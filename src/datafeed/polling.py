"""A tick stream built by polling, for feeds with no websocket.

The bot's live loop was written around ``AngelWebSocket``: it assigns an
``on_price_update`` callback, subscribes tokens, and calls ``connect()``. That
is the only contract it depends on, so anything offering the same four methods
can drive it.

This class polls a :class:`MarketDataFeed` on a timer and emits the same
``(symbol, price_data)`` callbacks a websocket would. Ticks arrive at the poll
interval rather than on every trade, which is fine for a strategy that decides
on 3-minute candle closes but is not suitable for anything latency-sensitive.

Day open/high/low are tracked from observed prices rather than requested per
tick, keeping each poll to one request per symbol — or a single batched request
when the feed supports it.
"""

import threading
import time
from datetime import time as dt_time
from typing import Callable, Dict, List, Optional

from loguru import logger

from src.datafeed.base import MarketDataFeed
from src.utils.timezone import now_ist

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)


class PollingPriceFeed:
    """Websocket-shaped tick source backed by periodic polling."""

    def __init__(self, feed: MarketDataFeed, interval_seconds: float = 3.0,
                 only_market_hours: bool = True):
        self.feed = feed
        self.interval = max(0.5, float(interval_seconds))
        self.only_market_hours = only_market_hours

        self.on_price_update: Optional[Callable[[str, Dict], None]] = None
        self.subscribed_tokens: List[Dict] = []
        self.symbol_map: Dict[str, str] = {}
        self.is_connected = False

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._price_cache: Dict[str, Dict] = {}
        # Running session extremes, so each tick carries day OHLC like a
        # websocket tick does without paying for a quote call every poll.
        self._day: Dict[str, Dict] = {}
        self._session_date: Optional[str] = None

    # ------------------------------------------------ websocket-shaped API

    def subscribe(self, tokens: List[Dict], symbol_map: Dict[str, str] = None) -> bool:
        """Register the instruments to poll. Mirrors AngelWebSocket.subscribe."""
        self.subscribed_tokens = tokens or []
        self.symbol_map = symbol_map or {
            str(t.get("token")): t.get("symbol", "") for t in self.subscribed_tokens
        }
        logger.info(f"Polling feed: tracking {len(self.subscribed_tokens)} instruments "
                    f"every {self.interval:g}s")
        return True

    def connect(self) -> bool:
        """Start the polling thread."""
        if self.is_connected:
            return True
        if not self.subscribed_tokens:
            logger.warning("Polling feed: connect() called with nothing subscribed")

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="polling-feed", daemon=True)
        self._thread.start()
        self.is_connected = True
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval + 2)
        self.is_connected = False
        logger.info("Polling feed stopped")

    def unsubscribe(self, tokens: List[Dict]) -> bool:
        drop = {str(t.get("token")) for t in tokens or []}
        self.subscribed_tokens = [
            t for t in self.subscribed_tokens if str(t.get("token")) not in drop
        ]
        return True

    def get_cached_price(self, token: str) -> Optional[Dict]:
        with self._lock:
            cached = self._price_cache.get(str(token))
            return cached.copy() if cached else None

    # ------------------------------------------------------------ internals

    def _market_is_open(self) -> bool:
        if not self.only_market_hours:
            return True
        now = now_ist()
        if now.weekday() >= 5:  # Saturday/Sunday
            return False
        return MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self._market_is_open():
                    self._poll_once()
            except Exception as exc:  # never let one bad poll kill the thread
                logger.error(f"Polling feed error: {exc}")
            self._stop.wait(self.interval)

    def _fetch_prices(self) -> Dict[str, float]:
        """Latest price per token, batched when the feed supports it."""
        batch = getattr(self.feed, "get_ltp_batch", None)
        if callable(batch):
            try:
                return batch(self.subscribed_tokens)
            except Exception as exc:
                logger.debug(f"Batch LTP failed ({exc}); falling back to per-symbol")

        prices: Dict[str, float] = {}
        for item in self.subscribed_tokens:
            token = str(item.get("token"))
            symbol = item.get("symbol") or self.symbol_map.get(token, "")
            price = self.feed.get_ltp(symbol, token)
            if price:
                prices[token] = float(price)
        return prices

    def _poll_once(self) -> None:
        today = now_ist().date().isoformat()
        if today != self._session_date:
            # New session: forget yesterday's extremes.
            self._session_date = today
            self._day.clear()

        for token, ltp in self._fetch_prices().items():
            if ltp <= 0:
                continue
            symbol = self.symbol_map.get(token) or token

            day = self._day.get(token)
            if day is None:
                day = {"open": ltp, "high": ltp, "low": ltp}
                self._day[token] = day
            else:
                day["high"] = max(day["high"], ltp)
                day["low"] = min(day["low"], ltp)

            price_data = {
                "token": token,
                "ltp": ltp,
                "open": day["open"],
                "high": day["high"],
                "low": day["low"],
                "close": ltp,
                "volume": 0,  # polling gives no reliable cumulative volume
            }

            with self._lock:
                self._price_cache[token] = price_data.copy()

            if self.on_price_update and symbol and symbol != token:
                try:
                    self.on_price_update(symbol, price_data)
                except Exception as exc:
                    logger.error(f"Error in price update callback for {symbol}: {exc}")
