"""Broker-independent market data feeds.

The bot was originally written against Angel One SmartAPI, which supplied both
execution *and* every piece of market data (LTP, candles, Nifty gap, previous
day OHLC). Univest offers no public trading/data API, so this package provides
a data source that needs no broker credentials at all.
"""

from src.datafeed.base import MarketDataFeed
from src.datafeed.dhan_feed import DhanAuthError, DhanFeed
from src.datafeed.yfinance_feed import YFinanceFeed
from src.datafeed.symbols import to_yf_ticker, is_index_token, NIFTY_TOKEN

__all__ = [
    "MarketDataFeed",
    "YFinanceFeed",
    "DhanFeed",
    "DhanAuthError",
    "get_feed",
    "to_yf_ticker",
    "is_index_token",
    "NIFTY_TOKEN",
]


def get_feed(name: str = "auto", **kwargs) -> MarketDataFeed:
    """Build a feed by name.

    ``auto`` prefers Dhan when credentials are present — it is real-time and
    carries 5 years of intraday history — and falls back to Yahoo, which needs
    no account but retains only ~30 days and is not guaranteed real-time.
    """
    import os

    name = (name or "auto").lower()
    if name == "auto":
        has_dhan = os.environ.get("DHAN_CLIENT_ID") and os.environ.get("DHAN_ACCESS_TOKEN")
        name = "dhan" if has_dhan else "yfinance"

    if name == "dhan":
        return DhanFeed(**kwargs)
    if name in ("yfinance", "yahoo", "yf"):
        return YFinanceFeed(**kwargs)
    raise ValueError(f"Unknown feed {name!r}; expected 'dhan', 'yfinance' or 'auto'")
