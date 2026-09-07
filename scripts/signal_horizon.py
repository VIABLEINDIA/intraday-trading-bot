"""Does the entry signal predict anything, at any horizon?

Every test so far asked how much money the strategy extracts. This asks the
prior question: is there information in the entry at all?

Method: take the exact same entries the backtester takes (same gap
classification, same ranking, same break-and-close confirmation -- the engine's
own functions are reused so the entries are identical), then measure forward
returns at several horizons with **no stop, no target and no costs**. If the
signal carries information, a cost-free horizon has to show it. If none does,
no amount of cost engineering or parameter tuning will rescue the strategy.

Returns are signed by trade direction, so a positive number means the signal was
right regardless of whether it was long or short.

The market-adjusted column matters most. In a rising year a long-biased signal
earns a positive raw return from market beta alone, which says nothing about the
signal. Subtracting the Nifty's move over the identical window strips that out.

Usage
-----
    python scripts/signal_horizon.py --cache-dir <dir> --direction momentum
"""

import argparse
import json
import pickle
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loguru import logger

from src.backtest.backtest_engine import BacktestEngine

NIFTY_KEY = "__NIFTY__"

# (label, kind, amount) -- "min" walks forward inside the session,
# "eod" takes the session's last bar, "day" takes a later session's last bar.
HORIZONS = [
    ("+30 min", "min", 30),
    ("+60 min", "min", 60),
    ("+120 min", "min", 120),
    ("session close", "eod", 0),
    ("+1 day", "day", 1),
    ("+3 days", "day", 3),
    ("+5 days", "day", 5),
]


def index_by_session(candles: List[Dict]) -> Dict[str, List[Dict]]:
    grouped: Dict[str, List[Dict]] = {}
    for c in candles:
        grouped.setdefault(c["timestamp"][:10], []).append(c)
    for bars in grouped.values():
        bars.sort(key=lambda c: c["timestamp"])
    return grouped


def price_at_or_after(bars: List[Dict], when: datetime) -> Optional[float]:
    """Close of the first bar at or after ``when``; None if the session ends first."""
    for c in bars:
        if datetime.fromisoformat(c["timestamp"]) >= when:
            return c["close"]
    return None


def forward_price(sessions: Dict[str, List[Dict]], ordered: List[str],
                  date_str: str, entry_dt: datetime, kind: str, amount: int) -> Optional[float]:
    """Price at one horizon after the entry, or None if it runs off the data."""
    if kind == "min":
        return price_at_or_after(sessions[date_str], entry_dt + timedelta(minutes=amount))
    if kind == "eod":
        return sessions[date_str][-1]["close"]
    if kind == "day":
        try:
            pos = ordered.index(date_str)
        except ValueError:
            return None
        target = pos + amount
        if target >= len(ordered):
            return None
        return sessions[ordered[target]][-1]["close"]
    return None


def summarise(name: str, raw: List[float], adj: List[float]) -> str:
    """One row: sample size, central tendency, hit rate and significance."""
    if not raw:
        return f"  {name:<15} {'no observations':>50}"

    mean = statistics.mean(raw)
    med = statistics.median(raw)
    win = sum(1 for r in raw if r > 0) / len(raw) * 100
    amean = statistics.mean(adj)

    # t = mean / standard error. |t| > ~2 is the usual "not obviously noise" bar.
    def tstat(xs: List[float]) -> float:
        if len(xs) < 2:
            return 0.0
        sd = statistics.stdev(xs)
        return (statistics.mean(xs) / (sd / len(xs) ** 0.5)) if sd else 0.0

    return (f"  {name:<15} {len(raw):>6} {mean:>+9.3f}% {med:>+9.3f}% {win:>7.1f}% "
            f"{tstat(raw):>7.2f} {amean:>+11.3f}% {tstat(adj):>7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", required=True, help="Directory holding the cached candles")
    ap.add_argument("--cache-file", default="dhan_365d_50sym.pkl")
    ap.add_argument("--direction", default="momentum", choices=["fade", "momentum"])
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    path = Path(args.cache_dir) / args.cache_file
    if not path.exists():
        print(f"ERROR: no cache at {path}", file=sys.stderr)
        return 1
    with path.open("rb") as fh:
        payload = pickle.load(fh)

    config = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    config.setdefault("strategies", {}).setdefault("three_minute", {}).setdefault(
        "params", {})["direction_mode"] = args.direction
    engine = BacktestEngine(config)

    stocks = [{"symbol": s, "token": ""} for s in payload["stocks"]]
    sessions = {sym: index_by_session(c) for sym, c in payload["stocks"].items()}
    sessions[NIFTY_KEY] = index_by_session(payload["nifty"])
    ordered = {sym: sorted(idx) for sym, idx in sessions.items()}
    dates = ordered[NIFTY_KEY]

    # horizon label -> (signed returns, market-adjusted signed returns)
    results: Dict[str, List[List[float]]] = {h[0]: [[], []] for h in HORIZONS}
    entries = 0

    for i, date_str in enumerate(dates):
        if i == 0:
            continue
        prev = dates[i - 1]

        nifty_slice = sessions[NIFTY_KEY][prev] + sessions[NIFTY_KEY][date_str]
        gap = engine._classify_nifty_gap(nifty_slice)
        if not gap.get("valid"):
            continue

        stock_slices = {}
        for sym, idx in sessions.items():
            if sym == NIFTY_KEY or date_str not in idx or prev not in idx:
                continue
            stock_slices[sym] = idx[prev] + idx[date_str]
        if not stock_slices:
            continue

        ranked = engine._rank_stocks_by_gap(stock_slices, stocks)
        for pick in engine._select_stocks(ranked, gap):
            sym, direction = pick["symbol"], pick["direction"]
            bars = sessions[sym].get(date_str) or []
            if len(bars) < 2:
                continue

            ref = bars[0]
            entry = None
            for candle in bars[1:]:
                if engine._get_time_from_timestamp(candle["timestamp"]) >= __import__(
                        "datetime").time(15, 0):
                    break
                if engine._check_candle_breakout(candle, direction, ref["high"], ref["low"]):
                    entry = candle
                    break
            if entry is None:
                continue

            entries += 1
            entry_px = entry["close"]
            entry_dt = datetime.fromisoformat(entry["timestamp"])
            sign = 1.0 if direction == "LONG" else -1.0

            # The index leg of the same window, to net out beta.
            nifty_entry = price_at_or_after(sessions[NIFTY_KEY][date_str], entry_dt)

            for label, kind, amount in HORIZONS:
                px = forward_price(sessions[sym], ordered[sym], date_str,
                                   entry_dt, kind, amount)
                if px is None or entry_px <= 0:
                    continue
                ret = sign * (px - entry_px) / entry_px * 100
                results[label][0].append(ret)

                nx = forward_price(sessions[NIFTY_KEY], ordered[NIFTY_KEY], date_str,
                                   entry_dt, kind, amount)
                if nx is not None and nifty_entry:
                    mkt = sign * (nx - nifty_entry) / nifty_entry * 100
                    results[label][1].append(ret - mkt)

    print(f"\n{'=' * 92}")
    print(f"  SIGNAL HORIZON TEST ({args.direction}) -- {entries} entries over "
          f"{len(dates) - 1} sessions")
    print("  No stops, no targets, no costs. Returns signed by trade direction.")
    print(f"{'=' * 92}")
    print(f"  {'horizon':<15} {'n':>6} {'mean':>10} {'median':>10} {'win%':>8} "
          f"{'t':>7} {'mkt-adj mean':>12} {'t':>7}")
    print(f"  {'-' * 88}")
    for label, _, _ in HORIZONS:
        raw, adj = results[label]
        print(summarise(label, raw, adj))

    print()
    print("  mkt-adj subtracts the Nifty's move over the identical window, so it")
    print("  strips out market beta. |t| > 2 is the usual bar for 'not just noise'.")
    print("  A signal with no edge shows means near zero and |t| below 2 everywhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
