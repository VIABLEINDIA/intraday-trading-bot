"""Symbol translation between the bot's Angel One conventions and Yahoo Finance.

The bot identifies instruments by an Angel One trading symbol (``SBIN-EQ``) plus
a numeric ``token`` (``3045``). Yahoo Finance uses suffixed tickers instead
(``SBIN.NS``) and has no concept of a token, so tokens are only used here to
recognise indices.
"""

from typing import Optional

# Angel One instrument token for the Nifty 50 index.
NIFTY_TOKEN = "99926000"

# Indices have no ``.NS`` ticker on Yahoo; they use dedicated caret symbols.
_INDEX_TICKERS = {
    NIFTY_TOKEN: "^NSEI",
    "99926009": "^NSEBANK",  # Bank Nifty
}

_INDEX_NAMES = {
    "NIFTY": "^NSEI",
    "NIFTY 50": "^NSEI",
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NIFTY BANK": "^NSEBANK",
}

# A handful of NSE symbols whose Yahoo ticker is not simply ``<symbol>.NS``.
_TICKER_OVERRIDES = {
    "M&M": "M&M.NS",
    "MM": "M&M.NS",
}


def is_index_token(token: Optional[str], symbol: Optional[str] = None) -> bool:
    """True when the symbol/token pair refers to an index rather than a stock."""
    if token and str(token).strip() in _INDEX_TICKERS:
        return True
    if symbol and _normalise(symbol) in _INDEX_NAMES:
        return True
    return False


def _normalise(symbol: str) -> str:
    """Strip Angel One's series suffix and normalise casing/whitespace."""
    s = (symbol or "").strip().upper()
    for suffix in ("-EQ", "-BE", "-BZ", "-SM", "-ST"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    return s.strip()


def to_yf_ticker(symbol: str, token: Optional[str] = None) -> str:
    """Convert an Angel One symbol/token to a Yahoo Finance ticker.

    >>> to_yf_ticker("SBIN-EQ", "3045")
    'SBIN.NS'
    >>> to_yf_ticker("NIFTY", "99926000")
    '^NSEI'
    """
    tok = str(token).strip() if token else ""
    if tok in _INDEX_TICKERS:
        return _INDEX_TICKERS[tok]

    name = _normalise(symbol)
    if name in _INDEX_NAMES:
        return _INDEX_NAMES[name]
    if name in _TICKER_OVERRIDES:
        return _TICKER_OVERRIDES[name]
    if not name:
        raise ValueError(f"Cannot map empty symbol (token={token!r}) to a Yahoo ticker")
    return f"{name}.NS"
