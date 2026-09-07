"""Broker-independent market data feeds.

The bot was originally written against Angel One SmartAPI, which supplied both
execution *and* every piece of market data (LTP, candles, Nifty gap, previous
day OHLC). Univest offers no public trading/data API, so this package provides
a data source that needs no broker credentials at all.
"""

from src.datafeed.base import MarketDataFeed
from src.datafeed.yfinance_feed import YFinanceFeed
from src.datafeed.symbols import to_yf_ticker, is_index_token, NIFTY_TOKEN

__all__ = [
    "MarketDataFeed",
    "YFinanceFeed",
    "to_yf_ticker",
    "is_index_token",
    "NIFTY_TOKEN",
]
