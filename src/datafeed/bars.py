"""Bar aggregation shared by every market data feed.

No vendor serves the 3-minute candle this strategy is built on — Yahoo offers
1/2/5/15/30/60-minute bars and Dhan offers 1/5/15/25/60 — so every feed fetches
a finer interval and resamples. Keeping that in one place means the reference
candle is defined identically no matter where the data came from.
"""

from typing import Dict, List

import pandas as pd

from src.utils.timezone import IST

_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


def to_ist(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure a bar frame's index is tz-aware in IST."""
    idx = df.index
    df.index = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate bars to ``rule``, anchored to each session's first bar.

    Anchoring per session matters for intervals that do not divide evenly into
    the 09:15 open (10-minute, say); without it pandas buckets from midnight and
    a bucket straddles the open. 3-minute happens to align anyway — 09:15 is 555
    minutes past midnight — but relying on that would silently break other
    intervals.
    """
    cols = {c: how for c, how in _AGG.items() if c in df.columns}

    out = []
    for _, day in df.groupby(df.index.date):
        origin = day.index[0]
        out.append(
            day.resample(rule, origin=origin, label="left", closed="left")
            .agg(cols)
            .dropna(subset=["Open"])
        )

    if not out:
        return df.iloc[0:0]
    return pd.concat(out).sort_index()


def to_candles(df: pd.DataFrame) -> List[Dict]:
    """Convert a bar frame to the candle dicts the bot and backtester expect."""
    candles = []
    for ts, row in df.iterrows():
        candles.append(
            {
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S%z").replace("+0530", "+05:30"),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]) if pd.notna(row["Volume"]) else 0,
            }
        )
    return candles
