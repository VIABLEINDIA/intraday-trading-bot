"""The market-data interface the bot depends on.

``AngelOneClient`` mixes two responsibilities: market data and order execution.
Only the market-data half is required to run the strategy in paper mode, and
that half is what this interface captures. Any object satisfying it can drive
pre-market analysis, the live strategy loop and the backtester.

Candle dicts use the same shape Angel One's client already returns, so feeds are
drop-in replacements for each other::

    {"timestamp": "2026-09-07T09:15:00+05:30",
     "open": 100.0, "high": 101.0, "low": 99.5, "close": 100.75, "volume": 12345}
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class MarketDataFeed(ABC):
    """Read-only market data, independent of any broker's execution API."""

    #: Human-readable name used in logs and on the dashboard.
    name: str = "unknown"

    @property
    @abstractmethod
    def is_authenticated(self) -> bool:
        """Whether the feed is ready to serve data."""

    @abstractmethod
    def login(self) -> bool:
        """Prepare the feed. Feeds needing no credentials return True."""

    @abstractmethod
    def get_ltp(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[float]:
        """Last traded price, or None if unavailable."""

    @abstractmethod
    def get_quote(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[Dict]:
        """Snapshot with ltp/open/high/low/close/previousClose/volume."""

    @abstractmethod
    def get_historical_data(
        self,
        symbol: str,
        token: str,
        interval: str = "FIFTEEN_MINUTE",
        days: int = 5,
        exchange: str = "NSE",
    ) -> Optional[List[Dict]]:
        """Candles for the last ``days`` sessions, oldest first."""

    @abstractmethod
    def get_historical_data_for_date(
        self,
        symbol: str,
        token: str,
        date_str: str,
        interval: str = "THREE_MINUTE",
        exchange: str = "NSE",
        include_prev_day: bool = True,
    ) -> Optional[List[Dict]]:
        """Candles for a single session, optionally including the prior day."""

    @abstractmethod
    def get_previous_day_ohlc(
        self, symbol: str, token: str, exchange: str = "NSE"
    ) -> Optional[Dict]:
        """Previous session's OHLC, used for gaps and pivot levels."""

    def logout(self) -> bool:
        """Release any resources. Credential-free feeds have nothing to do."""
        return True
