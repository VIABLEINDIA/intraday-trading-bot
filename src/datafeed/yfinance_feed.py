"""Credential-free NSE market data via Yahoo Finance.

Why this exists
---------------
Univest publishes no trading or market-data API, so a bot that follows Univest
signals still needs prices from somewhere. Yahoo covers NSE equities and the
Nifty index without an account, which is enough to run pre-market analysis,
paper trading and backtests.

Two Yahoo limitations shape this module:

1. **There is no 3-minute interval.** The strategy's reference candle is
   3-minute, so 1-minute bars are fetched and resampled. NSE opens at 09:15,
   which is 555 minutes past midnight and exactly divisible by 3, so 3-minute
   buckets align to the session open naturally. Intervals that do not divide
   evenly (10-minute) are resampled per-day from the session's first bar.

2. **Intraday history is short.** Yahoo serves roughly 30 days of 1-minute data
   and 60 days of 5/15-minute data. Backtests reaching further back need a
   different source; ``get_historical_data_for_date`` says so explicitly rather
   than returning silently empty results.

Quotes are also **not guaranteed real-time** — treat this feed as fit for
research, paper trading and signal generation, not low-latency live execution.
"""

import time as _time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from src.datafeed import bars
from src.datafeed.base import MarketDataFeed
from src.datafeed.symbols import to_yf_ticker
from src.utils.timezone import IST

# Angel One interval name -> (Yahoo interval to request, optional resample rule)
_INTERVAL_MAP = {
    "ONE_MINUTE": ("1m", None),
    "THREE_MINUTE": ("1m", "3min"),
    "FIVE_MINUTE": ("5m", None),
    "TEN_MINUTE": ("5m", "10min"),
    "FIFTEEN_MINUTE": ("15m", None),
    "THIRTY_MINUTE": ("30m", None),
    "ONE_HOUR": ("60m", None),
    "ONE_DAY": ("1d", None),
}

# Yahoo's maximum lookback per intraday interval, in calendar days.
_MAX_LOOKBACK_DAYS = {"1m": 30, "5m": 60, "15m": 60, "30m": 60, "60m": 730}

# Yahoo also caps how much can be pulled in a *single* request. 1-minute bars
# are limited to 8 days per call, so longer 1m windows are fetched in chunks
# and stitched together.
_MAX_DAYS_PER_REQUEST = {"1m": 7}


class YFinanceFeed(MarketDataFeed):
    """Market data from Yahoo Finance. Needs no credentials."""

    name = "yfinance"

    def __init__(self, cache_ttl_seconds: int = 60):
        self._cache: Dict[tuple, tuple] = {}
        self._cache_ttl = cache_ttl_seconds
        self._yf = None

    # ------------------------------------------------------------------ setup

    @property
    def is_authenticated(self) -> bool:
        """Always ready once yfinance imports; there is nothing to log in to."""
        return self._yf is not None

    def login(self) -> bool:
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance is not installed. Run: pip install yfinance")
            return False
        self._yf = yf
        logger.info("Market data feed: Yahoo Finance (no credentials required)")
        return True

    # ------------------------------------------------------------- fetch core

    def _fetch(self, ticker: str, yf_interval: str, days: int) -> Optional[pd.DataFrame]:
        """Fetch raw bars for a ticker, with a short TTL cache."""
        if self._yf is None and not self.login():
            return None

        max_days = _MAX_LOOKBACK_DAYS.get(yf_interval)
        if max_days and days > max_days:
            logger.debug(
                f"{ticker}: {days}d of {yf_interval} exceeds Yahoo's {max_days}d limit; clamping"
            )
            days = max_days

        key = (ticker, yf_interval, days)
        cached = self._cache.get(key)
        if cached and (_time.time() - cached[1]) < self._cache_ttl:
            return cached[0]

        chunk = _MAX_DAYS_PER_REQUEST.get(yf_interval)
        try:
            if chunk and days > chunk:
                df = self._fetch_chunked(ticker, yf_interval, days, chunk)
            else:
                df = self._yf.Ticker(ticker).history(
                    period=f"{days}d", interval=yf_interval, auto_adjust=False
                )
        except Exception as exc:
            logger.error(f"Yahoo fetch failed for {ticker} ({yf_interval}): {exc}")
            return None

        if df is None or df.empty:
            logger.debug(f"{ticker}: Yahoo returned no {yf_interval} data")
            return None

        df = bars.to_ist(df)
        self._cache[key] = (df, _time.time())
        return df

    def _fetch_chunked(
        self, ticker: str, yf_interval: str, days: int, chunk_days: int
    ) -> Optional[pd.DataFrame]:
        """Pull a long intraday window as several short requests, stitched.

        Yahoo rejects 1-minute requests spanning more than 8 days, so walk
        backwards from today in ``chunk_days`` windows and concatenate. Empty
        chunks (holidays, or gaps beyond Yahoo's retention) are skipped rather
        than aborting the whole fetch.
        """
        end = datetime.now(IST).date() + timedelta(days=1)
        start_limit = end - timedelta(days=days)
        frames = []

        while end > start_limit:
            start = max(start_limit, end - timedelta(days=chunk_days))
            try:
                part = self._yf.Ticker(ticker).history(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval=yf_interval,
                    auto_adjust=False,
                )
            except Exception as exc:
                logger.debug(f"{ticker}: chunk {start}..{end} failed: {exc}")
                part = None

            if part is not None and not part.empty:
                frames.append(part)
            end = start

        if not frames:
            return None

        df = pd.concat(frames)
        # Chunk boundaries can overlap by a bar; keep one row per timestamp.
        return df[~df.index.duplicated(keep="last")].sort_index()

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
            logger.error(f"Unsupported interval {interval!r} for the Yahoo feed")
            return None

        yf_interval, rule = _INTERVAL_MAP[interval]
        ticker = to_yf_ticker(symbol, token)

        # Pad the window so weekends and holidays still yield `days` sessions.
        df = self._fetch(ticker, yf_interval, max(days + 3, 2))
        if df is None or df.empty:
            return None

        if rule:
            df = bars.resample(df, rule)

        return bars.to_candles(df) or None

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

        yf_interval, _ = _INTERVAL_MAP.get(interval, ("1m", None))
        age_days = (datetime.now(IST).date() - target).days
        limit = _MAX_LOOKBACK_DAYS.get(yf_interval)

        if age_days < 0:
            logger.error(f"{date_str} is in the future")
            return None
        if limit and age_days > limit:
            logger.error(
                f"{date_str} is {age_days} days old; Yahoo serves at most {limit} days "
                f"of {yf_interval} bars. Use a broker feed for older backtests."
            )
            return None

        # Fetch through today, then trim to the requested session (+ the prior one).
        candles = self.get_historical_data(
            symbol, token, interval=interval, days=age_days + 6, exchange=exchange
        )
        if not candles:
            return None

        selected = [
            c for c in candles
            if datetime.fromisoformat(c["timestamp"]).date() <= target
        ]

        if selected:
            dates = sorted({datetime.fromisoformat(c["timestamp"]).date() for c in selected})
            wanted = set(dates[-2:]) if include_prev_day else {target}
            selected = [
                c for c in selected
                if datetime.fromisoformat(c["timestamp"]).date() in wanted
            ]

        if not selected or all(
            datetime.fromisoformat(c["timestamp"]).date() != target for c in selected
        ):
            logger.warning(f"{symbol}: no bars for {date_str} (holiday or delisted?)")
            return None
        return selected

    def get_previous_day_ohlc(
        self, symbol: str, token: str, exchange: str = "NSE"
    ) -> Optional[Dict]:
        candles = self.get_historical_data(
            symbol, token, interval="ONE_DAY", days=7, exchange=exchange
        )
        if not candles or len(candles) < 2:
            return None

        today = datetime.now(IST).date()
        # The final candle is today's session once the market has opened, so the
        # "previous day" is the latest completed session strictly before today.
        prior = [c for c in candles if datetime.fromisoformat(c["timestamp"]).date() < today]
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
        """Latest snapshot built from daily bars, refined with an intraday LTP."""
        daily = self.get_historical_data(symbol, token, interval="ONE_DAY", days=7)
        if not daily:
            return None

        today = datetime.now(IST).date()
        latest = daily[-1]
        is_today = datetime.fromisoformat(latest["timestamp"]).date() == today

        if not is_today:
            # Market has not opened yet: today's open/high/low do not exist.
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

    def get_ltp(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[float]:
        """Most recent 1-minute close. Not guaranteed to be tick-current."""
        ticker = to_yf_ticker(symbol, token)
        df = self._fetch(ticker, "1m", 2)
        if df is None or df.empty:
            daily = self.get_historical_data(symbol, token, interval="ONE_DAY", days=5)
            return daily[-1]["close"] if daily else None
        return float(df["Close"].iloc[-1])
