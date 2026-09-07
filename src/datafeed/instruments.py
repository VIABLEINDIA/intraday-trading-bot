"""Resolve NSE trading symbols to the security id the data feeds need.

The bot ships tokens for the Nifty 50 only, but advisory calls land on any
listed name, so a symbol typed by hand has to be resolvable to an id. Dhan
publishes a full scrip master; it is downloaded once and cached, then searched
in memory.

Conveniently the same number works for both feeds in use: for NSE equities
Dhan's ``SECURITY_ID`` is the exchange token, which is what the Angel One code
paths already call ``token``.
"""

import csv
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from loguru import logger

SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
DEFAULT_CACHE = Path("data") / "scrip_master.csv"
MAX_AGE_DAYS = 7

# Indices are not in the equity segment and carry their own ids.
_INDEX_TOKENS = {
    "NIFTY": "99926000",
    "NIFTY50": "99926000",
    "NIFTY 50": "99926000",
    "BANKNIFTY": "99926009",
    "NIFTY BANK": "99926009",
}

_cache: Optional[Dict[str, str]] = None


def _download(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading Dhan scrip master (~34 MB, once a week)...")
    try:
        urllib.request.urlretrieve(SCRIP_MASTER_URL, path)
        return True
    except Exception as exc:
        logger.error(f"Could not download scrip master: {exc}")
        return False


def _needs_refresh(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return True
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return age_days > MAX_AGE_DAYS


def load_symbol_map(cache_path: Path = DEFAULT_CACHE) -> Dict[str, str]:
    """Map of NSE trading symbol -> security id, for cash-segment equities."""
    global _cache
    if _cache is not None:
        return _cache

    if _needs_refresh(cache_path) and not _download(cache_path):
        if not cache_path.exists():
            _cache = {}
            return _cache

    mapping: Dict[str, str] = {}
    try:
        with cache_path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("EXCH_ID") != "NSE" or row.get("SEGMENT") != "E":
                    continue
                if row.get("SERIES") not in ("EQ", "BE"):
                    continue
                symbol = (row.get("UNDERLYING_SYMBOL") or "").strip().upper()
                sec_id = (row.get("SECURITY_ID") or "").strip()
                if symbol and sec_id:
                    mapping.setdefault(symbol, sec_id)
    except Exception as exc:
        logger.error(f"Could not read scrip master: {exc}")

    logger.debug(f"Loaded {len(mapping)} NSE equity symbols")
    _cache = mapping
    return mapping


def normalise(symbol: str) -> str:
    """Strip the series suffix the bot's own symbols carry (``SBIN-EQ``)."""
    s = (symbol or "").strip().upper()
    for suffix in ("-EQ", "-BE", "-BZ", "-SM", "-ST"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def resolve_token(symbol: str, cache_path: Path = DEFAULT_CACHE) -> Optional[str]:
    """Security id for a symbol, or None if it is not a recognised NSE name."""
    name = normalise(symbol)
    if name in _INDEX_TOKENS:
        return _INDEX_TOKENS[name]
    return load_symbol_map(cache_path).get(name)
