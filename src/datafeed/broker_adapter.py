"""Present a market data feed with the surface the bot expects of a broker.

``Bot`` was written against ``AngelOneClient``, which mixes market data with
account and order operations. Swapping in a data-only feed therefore needs a
shim: this adapter forwards the data calls to a :class:`MarketDataFeed` and
answers the account calls with paper-mode values.

Order placement deliberately fails loudly. Univest publishes no API, and the
whole point of this path is research and paper trading — silently pretending an
order was placed would be the worst possible behaviour, so anything that would
send a real order raises instead.
"""

from typing import Dict, List, Optional

from loguru import logger

from src.datafeed.base import MarketDataFeed


class BrokerlessAdapter:
    """A MarketDataFeed dressed as the broker client the bot expects."""

    def __init__(self, feed: MarketDataFeed, paper_balance: float = 100000.0):
        self.feed = feed
        self.paper_balance = float(paper_balance)
        self._authenticated = False

        # The live loop reads these off the client to build an Angel One
        # websocket. They stay empty because this path polls instead; the bot
        # checks the feed type before touching them.
        self.market_api_key = ""
        self.client_id = ""
        self.feed_token = ""
        self.auth_token = ""

    # ------------------------------------------------------------- identity

    @property
    def name(self) -> str:
        return getattr(self.feed, "name", "unknown")

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def login(self) -> bool:
        self._authenticated = bool(self.feed.login())
        return self._authenticated

    def logout(self) -> bool:
        self._authenticated = False
        return self.feed.logout()

    # -------------------------------------------------------------- account

    def get_profile(self) -> Optional[Dict]:
        return {"name": f"paper ({self.name} data)", "clientcode": "PAPER"}

    def get_available_balance(self) -> float:
        return self.paper_balance

    def get_funds(self) -> Optional[Dict]:
        return {
            "availablecash": self.paper_balance,
            "net": self.paper_balance,
            "utiliseddebits": 0.0,
        }

    def get_positions(self) -> Optional[List[Dict]]:
        # Positions live in the PaperTrader on this path.
        return []

    # ----------------------------------------------------------- market data

    def get_ltp(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[float]:
        return self.feed.get_ltp(symbol, token, exchange)

    def get_ltp_batch(self, instruments: List[Dict]) -> Dict[str, float]:
        batch = getattr(self.feed, "get_ltp_batch", None)
        return batch(instruments) if callable(batch) else {}

    def get_quote(self, symbol: str, token: str, exchange: str = "NSE") -> Optional[Dict]:
        return self.feed.get_quote(symbol, token, exchange)

    def get_historical_data(self, symbol: str, token: str,
                            interval: str = "FIFTEEN_MINUTE", days: int = 5,
                            exchange: str = "NSE") -> Optional[List[Dict]]:
        return self.feed.get_historical_data(symbol, token, interval, days, exchange)

    def get_historical_data_for_date(self, symbol: str, token: str, date_str: str,
                                     interval: str = "THREE_MINUTE",
                                     exchange: str = "NSE",
                                     include_prev_day: bool = True) -> Optional[List[Dict]]:
        return self.feed.get_historical_data_for_date(
            symbol, token, date_str, interval, exchange, include_prev_day
        )

    def get_previous_day_ohlc(self, symbol: str, token: str,
                              exchange: str = "NSE") -> Optional[Dict]:
        return self.feed.get_previous_day_ohlc(symbol, token, exchange)

    # --------------------------------------------------------------- orders

    def place_order(self, *args, **kwargs):
        raise NotImplementedError(
            f"The {self.name} feed is market data only and cannot place orders. "
            "Run in paper mode, or execute manually in your broker's app."
        )

    def cancel_order(self, *args, **kwargs) -> bool:
        raise NotImplementedError(f"The {self.name} feed cannot cancel orders.")

    def get_order_book(self) -> Optional[List[Dict]]:
        logger.debug("Order book requested on a data-only feed; returning empty")
        return []
