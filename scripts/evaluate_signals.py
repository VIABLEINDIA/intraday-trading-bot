"""Score logged advisory calls after costs, slippage and market beta.

An advisory's published record is a hit rate on gross moves. Neither of the two
things that decide whether following it makes money appears in that number:
what it costs to trade, and how much of the move was the market rising anyway.
This scores a hand-kept log of calls on both.

Each signal is scored one of two ways (see signals/README.md):

* **Reconstructed** -- no exit recorded, so the day's 1-minute bars are replayed
  from the entry, taking whichever of stop or target is touched first and
  squaring off at 15:15 otherwise. Measures what the *advice* was worth.
* **Reported** -- an exit price was recorded, so that fill is scored directly.
  Needed for options, whose history this repo does not fetch, and useful when
  you want your actual execution measured rather than the idealised call.

Usage
-----
    python scripts/evaluate_signals.py
    python scripts/evaluate_signals.py --slippage-bps 3 --capital 100000
"""

import argparse
import csv
import statistics
import sys
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loguru import logger

from src.analysis.transaction_costs import TransactionCostCalculator
from src.datafeed import get_feed
from src.datafeed.instruments import resolve_token
from src.utils.timezone import IST

SQUARE_OFF = dt_time(15, 15)
NIFTY_TOKEN = "99926000"


def read_signals(path: Path) -> List[Dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("symbol") or "").strip()]
    return rows


def as_float(value: Optional[str]) -> Optional[float]:
    try:
        text = (value or "").strip()
        return float(text) if text else None
    except ValueError:
        return None


def bars_for(feed, symbol: str, token: str, date_str: str) -> List[Dict]:
    candles = feed.get_historical_data_for_date(
        symbol, token, date_str, interval="ONE_MINUTE", include_prev_day=False
    )
    return [c for c in (candles or []) if c["timestamp"][:10] == date_str]


def price_at(bars: List[Dict], when: datetime) -> Optional[float]:
    for c in bars:
        if datetime.fromisoformat(c["timestamp"]) >= when:
            return c["close"]
    return bars[-1]["close"] if bars else None


def replay(bars: List[Dict], start: datetime, side: str, entry: float,
           stop: Optional[float], target: Optional[float]) -> Dict:
    """Walk the session from the entry, taking stop or target, else squaring off.

    A bar that spans both levels is charged as the stop. Intrabar order is
    unknowable from OHLC, and assuming the good fill is how backtests flatter
    themselves.
    """
    forward = [c for c in bars if datetime.fromisoformat(c["timestamp"]) >= start]
    for c in forward:
        ts = datetime.fromisoformat(c["timestamp"])
        hit_stop = stop is not None and (
            c["low"] <= stop if side == "LONG" else c["high"] >= stop
        )
        hit_target = target is not None and (
            c["high"] >= target if side == "LONG" else c["low"] <= target
        )
        if hit_stop:
            return {"exit": stop, "reason": "STOP", "exit_time": ts}
        if hit_target:
            return {"exit": target, "reason": "TARGET", "exit_time": ts}
        if ts.time() >= SQUARE_OFF:
            return {"exit": c["close"], "reason": "SQUARE_OFF", "exit_time": ts}

    if forward:
        last = forward[-1]
        return {"exit": last["close"], "reason": "SESSION_END",
                "exit_time": datetime.fromisoformat(last["timestamp"])}
    return {"exit": entry, "reason": "NO_DATA", "exit_time": start}


def tstat(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    sd = statistics.stdev(xs)
    return (statistics.mean(xs) / (sd / len(xs) ** 0.5)) if sd else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="signals/univest_signals.csv")
    ap.add_argument("--feed", default="auto", choices=["auto", "dhan", "yfinance"])
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--alloc-pct", type=float, default=25.0)
    ap.add_argument("--broker", default="zerodha")
    ap.add_argument("--slippage-bps", type=float, default=0.0,
                    help="Basis points charged per leg, entry and exit")
    ap.add_argument("--verbose", action="store_true", help="Print every signal")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: no signal log at {path}. See signals/README.md", file=sys.stderr)
        return 1

    signals = read_signals(path)
    if not signals:
        print(f"No signals recorded in {path} yet.", file=sys.stderr)
        return 1

    feed = get_feed(args.feed)
    if not feed.login():
        print("ERROR: could not initialise the data feed", file=sys.stderr)
        return 1

    costs = TransactionCostCalculator(args.broker)
    scored: List[Dict] = []
    skipped: List[str] = []

    for sig in signals:
        symbol = sig["symbol"].strip().upper()
        date_str = (sig.get("date") or "").strip()
        side = (sig.get("side") or "LONG").strip().upper()
        instrument = (sig.get("instrument") or "EQ").strip().upper()

        reported_exit = as_float(sig.get("exit_price"))
        entry = as_float(sig.get("entry"))

        # Options are scored only from reported fills; this repo does not
        # fetch historical option prices.
        if instrument == "OPT" and (reported_exit is None or entry is None):
            skipped.append(f"{date_str} {symbol}: option needs entry and exit_price")
            continue

        token = resolve_token(symbol)
        bars: List[Dict] = []
        if instrument == "EQ":
            if not token:
                skipped.append(f"{date_str} {symbol}: symbol not found on NSE")
                continue
            bars = bars_for(feed, symbol, token, date_str)
            if not bars and (entry is None or reported_exit is None):
                skipped.append(f"{date_str} {symbol}: no market data for that date")
                continue

        try:
            hhmm = (sig.get("time") or "09:15").strip()
            start = datetime.strptime(f"{date_str} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        except ValueError:
            skipped.append(f"{date_str} {symbol}: unreadable date/time")
            continue

        if entry is None:
            entry = price_at(bars, start)
        if entry is None:
            skipped.append(f"{date_str} {symbol}: no entry price available")
            continue

        if reported_exit is not None:
            exit_price, reason = reported_exit, (sig.get("exit_reason") or "REPORTED")
            exit_time = start
            if (sig.get("exit_time") or "").strip():
                try:
                    exit_time = datetime.strptime(
                        f"{date_str} {sig['exit_time'].strip()}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=IST)
                except ValueError:
                    pass
        else:
            out = replay(bars, start, side,
                         entry, as_float(sig.get("stop")), as_float(sig.get("target")))
            exit_price, reason, exit_time = out["exit"], out["reason"], out["exit_time"]

        sign = 1.0 if side == "LONG" else -1.0
        points = sign * (exit_price - entry)
        if args.slippage_bps:
            points -= (args.slippage_bps / 10000.0) * (entry + exit_price)

        qty = int((args.capital * args.alloc_pct / 100.0) // entry) if entry > 0 else 0
        gross = points * qty
        if qty > 0:
            legs = (entry, exit_price) if side == "LONG" else (exit_price, entry)
            fees = costs.calculate_costs(qty, legs[0], legs[1])["total"]
        else:
            fees = 0.0

        ret_pct = (points / entry * 100) if entry else 0.0

        # Market leg over the same window, to separate advice from beta.
        adj_pct = None
        nifty_bars = bars_for(feed, "NIFTY", NIFTY_TOKEN, date_str)
        if nifty_bars:
            n_in, n_out = price_at(nifty_bars, start), price_at(nifty_bars, exit_time)
            if n_in and n_out:
                adj_pct = ret_pct - sign * (n_out - n_in) / n_in * 100

        scored.append({
            "date": date_str, "symbol": symbol, "side": side, "instrument": instrument,
            "entry": entry, "exit": exit_price, "reason": reason, "qty": qty,
            "ret_pct": ret_pct, "adj_pct": adj_pct,
            "gross": gross, "fees": fees, "net": gross - fees,
        })

    if not scored:
        print("No signals could be scored.", file=sys.stderr)
        for s in skipped:
            print(f"  skipped {s}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"\n{'date':<12}{'symbol':<12}{'side':<6}{'entry':>9}{'exit':>9}"
              f"{'  reason':<14}{'ret%':>8}{'net':>10}")
        print("-" * 82)
        for s in scored:
            print(f"{s['date']:<12}{s['symbol']:<12}{s['side']:<6}{s['entry']:>9.2f}"
                  f"{s['exit']:>9.2f}  {s['reason']:<12}{s['ret_pct']:>+7.2f}%"
                  f"{s['net']:>10.2f}")

    rets = [s["ret_pct"] for s in scored]
    adj = [s["adj_pct"] for s in scored if s["adj_pct"] is not None]
    nets = [s["net"] for s in scored]
    wins = [s for s in scored if s["net"] > 0]
    losses = [s for s in scored if s["net"] <= 0]
    loss_sum = abs(sum(s["net"] for s in losses))

    print(f"\n{'=' * 78}")
    print(f"  ADVISORY SIGNAL REVIEW   {len(scored)} signals"
          f" | Rs{args.capital:,.0f} @ {args.alloc_pct:.0f}%/trade"
          f" | {args.slippage_bps:g}bp slippage")
    print(f"{'=' * 78}")
    print(f"  Win rate           : {len(wins) / len(scored) * 100:.1f}%"
          f"  ({len(wins)}W / {len(losses)}L)")
    print(f"  Mean return        : {statistics.mean(rets):+.3f}% per signal")
    print(f"  Median return      : {statistics.median(rets):+.3f}%")
    print(f"  Gross P&L          : Rs{sum(s['gross'] for s in scored):>12,.2f}")
    print(f"  Costs              : Rs{sum(s['fees'] for s in scored):>12,.2f}")
    print(f"  NET P&L            : Rs{sum(nets):>12,.2f}"
          f"   ({sum(nets) / args.capital * 100:+.2f}% on capital)")
    if loss_sum:
        print(f"  Profit factor      : {sum(s['net'] for s in wins) / loss_sum:.2f}")
    print(f"  t-stat (raw)       : {tstat(rets):.2f}")
    if adj:
        print(f"  Market-adjusted    : {statistics.mean(adj):+.3f}% per signal"
              f"   t = {tstat(adj):.2f}")

    print()
    if len(scored) < 20:
        print(f"  {len(scored)} signals is too few to conclude anything. Keep logging;")
        print("  30 gives a first read on the sign, 100 starts to be meaningful.")
    elif abs(tstat(adj or rets)) < 2:
        print("  Market-adjusted |t| is below 2: this is not yet distinguishable")
        print("  from noise, whichever way the P&L happens to point.")
    else:
        print("  Market-adjusted |t| exceeds 2 - worth taking seriously, and worth")
        print("  re-checking as more signals accumulate.")

    if skipped:
        print(f"\n  Skipped {len(skipped)}:")
        for s in skipped[:10]:
            print(f"    {s}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
