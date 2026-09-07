"""NSE market data from the DhanHQ v2 API.

Why this feed exists alongside the Yahoo one
--------------------------------------------
Yahoo retains only ~30 days of 1-minute bars, which is far too short to say
anything about a strategy's edge. Dhan serves **5 years** of intraday history
(90 days per request) and is a real-time exchange feed rather than a delayed
web quote, so it is the feed to backtest and to run live signals on.

Execution still happens by hand in Univest, which publishes no API — this is a
data source only, and nothing here places an order.

Credentials
-----------
Read from the environment, never hardcoded::

    DHAN_CLIENT_ID     your dhanClientId
    DHAN_ACCESS_TOKEN  JWT from web.dhan.co -> DhanHQ Trading APIs

Dhan access tokens are short-lived. When one expires every call returns
``DH-901``; :meth:`login` reports that as an expired-token error rather than a
generic failure, because regenerating the token is the fix.

Security IDs
------------
For NSE equities Dhan's ``securityId`` **is** the NSE exchange token, which is
the same number this repo already stores as the Angel One token — all 50 Nifty
constituents match exactly — so stock tokens pass straight through. Only
indices differ and are mapped explicitly.
"""

import json
import os
import time as _time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from src.datafeed import bars
from src.datafeed.base import MarketDataFeed
from src.datafeed.symbols import NIFTY_TOKEN
from src.utils.timezone import IST

_BASE = "https://api.dhan.co/v2"
_INTRADAY_URL = f"{_BASE}/charts/intraday"
_HISTORICAL_URL = f"{_BASE}/charts/historical"
_LTP_URL = f"{_BASE}/marketfeed/ltp"

# Dhan serves only these intraday intervals, so anything else is resampled.
# Angel One interval name -> (Dhan interval minutes, optional resample rule)
_INTERVAL_MAP = {
    "ONE_MINUTE": (1, None),
    "THREE_MINUTE": (1, "3min"),
    "FIVE_MINUTE": (5, None),
    "TEN_MINUTE": (5, "10min"),
    "FIFTEEN_MINUTE": (15, None),
    "THIRTY_MINUTE": (15, "30min"),
    "ONE_HOUR": (60, None),
    "ONE_DAY": (None, None),  # served by the daily endpoint instead
}

# Angel One index tokens -> (Dhan securityId, exchangeSegment, instrument)
_INDEX_MAP = {
    NIFTY_TOKEN: ("13", "IDX_I", "INDEX"),
    "99926009": ("25", "IDX_I", "INDEX"),  # Bank Nifty
}

_MAX_DAYS_PER_REQUEST = 90
_SESSION_OPEN_MINUTE = 9 * 60 + 15
_SESSION_CLOSE_MINUTE = 15 * 60 + 30


class DhanAuthError(RuntimeError):
    """Raised when Dhan rejects the credentials (typically an expired token)."""


class DhanFeed(MarketDataFeed):
    """Market data from DhanHQ. Requires a Dhan account and a fresh token."""

    name = "dhan"

    def __init__(
        self,
        client_id: Optional[str] = None,
        access_token: Optional[str] = None,
        cache_ttl_seconds: int = 60,
        timeout: int = 60,
    ):
        self._client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self._token = access_token or os.environ.get("DHAN_ACCESS_TOKEN", "")
        self._cache: Dict[tuple, tuple] = {}
        self._cache_ttl = cache_ttl_seconds
        self._timeout = timeout
        self._ready = False
        # Resolved on the first successful fetch; see _epoch_to_ist.
        self._epoch_is_ist_wallclock: Optional[bool] = None

    # ------------------------------------------------------------------ setup

    @property
    def is_authenticated(self) -> bool:
        return self._ready

    def login(self) -> bool:
        """Validate credentials with one cheap request."""
        if not self._client_id or not self._token:
            logger.error(
                "Dhan credentials missing. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN."
            )
            return False

        probe_end = datetime.now(IST)
        probe_start = probe_end - timedelta(days=7)
        try:
            self._post(
                _INTRADAY_URL,
                {
                    "securityId": "13",
                    "exchangeSegment": "IDX_I",
                    "instrument": "INDEX",
                    "interval": "1",
                    "oi": False,
                    "fromDate": probe_start.strftime("%Y-%m-%d %H:%M:%S"),
                    "toDate": probe_end.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except DhanAuthError as exc:
            logger.error(f"Dhan authentication failed: {exc}")
            return False
        except Exception as exc:
            logger.error(f"Dhan probe request failed: {exc}")
            return False

        self._ready = True
        logger.info(f"Market data feed: DhanHQ (client {self._client_id})")
        return True

    # -------------------------------------------------------------- transport

    def _post(self, url: str, body: Dict) -> Dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "access-token": self._token,
                "client-id": self._client_id,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (401, 403) or "DH-901" in detail:
                raise DhanAuthError(
                    "token invalid or expired - regenerate it at "
                    "web.dhan.co -> DhanHQ Trading APIs"
                ) from exc
            raise RuntimeError(f"Dhan HTTP {exc.code}: {detail}") from exc

    # ------------------------------------------------------------- timestamps

    def _epoch_to_ist(self, epochs: List[int]) -> pd.DatetimeIndex:
        """Convert Dhan epochs to IST, detecting their convention once.

        Dhan documents ``timestamp`` as epoch seconds but ships values already
        shifted to IST wall-clock, so interpreting them as true UTC lands every
        bar 5h30m early. Rather than hardcode either reading, infer it from the
        data: NSE trades 09:15-15:30, so whichever interpretation puts the bars
        inside the session window is the right one. The verdict is cached.
        """
        naive = pd.to_datetime(pd.Series(epochs), unit="s")

        if self._epoch_is_ist_wallclock is None:
            as_ist = naive
            as_utc = naive + pd.Timedelta(hours=5, minutes=30)
            in_session = lambda s: (
                (s.dt.hour * 60 + s.dt.minute).between(
                    _SESSION_OPEN_MINUTE, _SESSION_CLOSE_MINUTE
                ).mean()
            )
            ist_score, utc_score = in_session(as_ist), in_session(as_utc)
            # Coerce: the comparison yields a numpy bool, which fails `is True`
            # identity checks and leaks a numpy type into the public attribute.
            self._epoch_is_ist_wallclock = bool(ist_score >= utc_score)
            logger.debug(
                f"Dhan epoch convention: "
                f"{'IST wall-clock' if self._epoch_is_ist_wallclock else 'true UTC'} "
                f"(in-session {ist_score:.0%} vs {utc_score:.0%})"
            )

        if not self._epoch_is_ist_wallclock:
            naive = naive + pd.Timedelta(hours=5, minutes=30)
        return pd.DatetimeIndex(naive).tz_localize(IST)

    def _to_frame(self, payload: Dict) -> Optional[pd.DataFrame]:
        """Turn Dhan's column-arrays response into a bar frame."""
        stamps = payload.get("timestamp") or []
        if not stamps:
            return None

        df = pd.DataFrame(
            {
                "Open": payload.get("open", []),
                "High": payload.get("high", []),
                "Low": payload.get("low", []),
                "Close": payload.get("close", []),
                "Volume": payload.get("volume", []),
            }
        )
        df.index = self._epoch_to_ist(stamps)
        return df.sort_index()

    # ----------------------------------------------------------- instrument id

    @staticmethod
    def _resolve(symbol: str, token: str) -> Tuple[str, str, str]:
        """Map a symbol/token to (securityId, exchangeSegment, instrument)."""
        tok = str(token).strip()
        if tok in _INDEX_MAP:
            return _INDEX_MAP[tok]
        if not tok:
            raise ValueError(f"{symbol}: a security id (token) is required for Dhan")
        return tok, "NSE_EQ", "EQUITY"

    # ------------------------------------------------------------- fetch core

    def _fetch_intraday(
        self, sec_id: str, segment: str, instrument: str, minutes: int, days: int
    ) -> Optional[pd.DataFrame]:
        """Fetch intraday bars, in 90-day requests, oldest chunk first."""
        end = datetime.now(IST).replace(tzinfo=None)
        start_limit = end - timedelta(days=days)
        frames = []

        cursor_end = end
        while cursor_end > start_limit:
            cursor_start = max(start_limit, cursor_end - timedelta(days=_MAX_DAYS_PER_REQUEST))
            try:
                payload = self._post(
                    _INTRADAY_URL,
                    {
                        "securityId": sec_id,
                        "exchangeSegment": segment,
                        "instrument": instrument,
                        "interval": str(minutes),
                        "oi": False,
                        "fromDate": cursor_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "toDate": cursor_end.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            except DhanAuthError:
                raise
            except Exception as exc:
                logger.debug(f"{sec_id}: chunk {cursor_start.date()}..{cursor_end.date()}: {exc}")
                payload = {}

            part = self._to_frame(payload)
            if part is not None and not part.empty:
                frames.append(part)
            cursor_end = cursor_start

        if not frames:
            return None
        df = pd.concat(frames)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def _fetch_daily(self, sec_id: str, segment: str, instrument: str,
                     days: int) -> Optional[pd.DataFrame]:
        end = datetime.now(IST).date() + timedelta(days=1)
        start = end - timedelta(days=max(days, 7))
        payload = self._post(
            _HISTORICAL_URL,
            {
                "securityId": sec_id,
                "exchangeSegment": segment,
                "instrument": instrument,
                "expiryCode": 0,
                "oi": False,
                "fromDate": start.isoformat(),
                "toDate": end.isoformat(),
            },
        )
        return self._to_frame(payload)

    # --------------------------------------------------------- MarketDataFeed

    def get_historical_data(
        self,
        symbol: str,
        token: str,
        interval: str = "FIFTEEN_MINUTE",
        days: int = 5,
        exchange: str = "NSE",
    ) -> Optional[List[Dict]]:
        if interval not in _INTERVAL_MAP:
            logger.error(f"Unsupported interval {interval!r} for the Dhan feed")
            return None
        if not self._ready and not self.login():
            return None

        minutes, rule = _INTERVAL_MAP[interval]
        sec_id, segment, instrument = self._resolve(symbol, token)

        key = (sec_id, interval, days)
        cached = self._cache.get(key)
        if cached and (_time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            if minutes is None:
                df = self._fetch_daily(sec_id, segment, instrument, days)
            else:
                df = self._fetch_intraday(sec_id, segment, instrument, minutes, max(days + 3, 2))
        except DhanAuthError as exc:
            logger.error(f"{symbol}: {exc}")
            self._ready = False
            return None
        except Exception as exc:
            logger.error(f"{symbol}: Dhan fetch failed: {exc}")
            return None

        if df is None or df.empty:
            return None

        if rule:
            df = bars.resample(df, rule)

        candles = bars.to_candles(df) or None
        if candles:
            self._cache[key] = (candles, _time.time())
        return candles

    def get_historical_data_for_date(
        self,
        symbol: str,
        token: str,
        date_str: str,
        interval: str = "THREE_MINUTE",
        exchange: str = "NSE",
        include_prev_day: bool = True,
    ) -> Optional[List[Dict]]:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Bad date {date_str!r}; expected YYYY-MM-DD")
            return None

        age_days = (datetime.now(IST).date() - target).days
        if age_days < 0:
            logger.error(f"{date_str} is in the future")
            return None

        candles = self.get_historical_data(
            symbol, token, interval=interval, days=age_days + 6, exchange=exchange
        )
        if not candles:
            return None

        selected = [c for c in candles if c["timestamp"][:10] <= date_str]
        if selected:
            dates = sorted({c["timestamp"][:10] for c in selected})
            wanted = set(dates[-2:]) if include_prev_day else {date_str}
            selected = [c for c in selected if c["timestamp"][:10] in wanted]

        if not selected or all(c["timestamp"][:10] != date_str for c in selected):
            logger.warning(f"{symbol}: no bars for {date_str} (holiday or delisted?)")
            return None
        return selected

    def get_previous_day_ohlc(
        self, symbol: str, token: str, exchange: str = "NSE"
    ) -> Optional[Dict]:
        candles = self.get_historical_data(symbol, token, interval="ONE_DAY", days=10)
        if not candles or len(candles) < 2:
            return None

        today = datetime.now(IST).date().isoformat()
        prior = [c for c in candles if c["timestamp"][:10] < today]
        candle = prior[-1] if prior else candles[-2]
        return {
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "open": candle["open"],
            "volume": candle["volume"],
            "date": candle["timestamp"],
        }

    def get_quote(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[Dict]:
        daily = self.get_historical_data(symbol, token, interval="ONE_DAY", days=10)
        if not daily:
            return None

        today = datetime.now(IST).date().isoformat()
        latest = daily[-1]
        is_today = latest["timestamp"][:10] == today

        if not is_today:
            return {
                "symbol": symbol, "token": token, "ltp": latest["close"],
                "open": None, "high": None, "low": None,
                "close": latest["close"], "previousClose": latest["close"],
                "volume": latest["volume"],
            }

        prev_close = daily[-2]["close"] if len(daily) >= 2 else latest["open"]
        ltp = self.get_ltp(symbol, token, exchange) or latest["close"]
        return {
            "symbol": symbol, "token": token, "ltp": ltp,
            "open": latest["open"], "high": latest["high"], "low": latest["low"],
            "close": ltp, "previousClose": prev_close, "volume": latest["volume"],
        }

    def get_ltp_batch(self, instruments: List[Dict]) -> Dict[str, float]:
        """Last traded price for many instruments in one request.

        The market feed endpoint accepts a whole segment's worth of security
        ids at once, so polling N symbols costs one call rather than N. Returns
        {token: price} keyed by the caller's token, skipping anything the
        response does not carry.
        """
        if not self._ready and not self.login():
            return {}

        by_segment: Dict[str, List[int]] = {}
        for item in instruments:
            token = str(item.get("token", "")).strip()
            if not token:
                continue
            sec_id, segment, _ = self._resolve(item.get("symbol", ""), token)
            by_segment.setdefault(segment, []).append(int(sec_id))

        if not by_segment:
            return {}

        try:
            payload = self._post(_LTP_URL, by_segment)
        except DhanAuthError as exc:
            logger.error(f"Batch LTP: {exc}")
            self._ready = False
            return {}

        prices: Dict[str, float] = {}
        data = payload.get("data", {})
        for segment, quotes in data.items():
            for sec_id, quote in (quotes or {}).items():
                price = (quote or {}).get("last_price")
                if price:
                    prices[str(sec_id)] = float(price)

        # Index tokens differ from Dhan security ids; map them back.
        for angel_token, (sec_id, _seg, _instr) in _INDEX_MAP.items():
            if sec_id in prices:
                prices.setdefault(angel_token, prices[sec_id])
        return prices

    def get_ltp(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[float]:
        """Real-time last traded price from the market feed endpoint."""
        if not self._ready and not self.login():
            return None

        sec_id, segment, _ = self._resolve(symbol, token)
        try:
            payload = self._post(_LTP_URL, {segment: [int(sec_id)]})
            quote = payload.get("data", {}).get(segment, {}).get(str(sec_id), {})
            price = quote.get("last_price")
            if price:
                return float(price)
        except DhanAuthError as exc:
            logger.error(f"{symbol}: {exc}")
            self._ready = False
            return None
        except Exception as exc:
            logger.debug(f"{symbol}: LTP endpoint failed ({exc}); falling back to 1m close")

        recent = self.get_historical_data(symbol, token, interval="ONE_MINUTE", days=2)
        return recent[-1]["close"] if recent else None
