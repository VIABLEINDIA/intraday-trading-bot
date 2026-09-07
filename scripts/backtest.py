"""Backtest the 3-minute breakout strategy against a pluggable data feed.

The existing backtester is driven from the dashboard and sources its candles
from Angel One, which means it cannot run without SmartAPI credentials. This
runner feeds the same ``BacktestEngine`` from any ``MarketDataFeed``: Dhan for
depth (5 years of intraday history) or Yahoo for convenience (no account, ~30
days).

Each Nifty 50 stock is downloaded once and sliced per session locally, rather
than re-fetched per date — both vendors cap how much can be pulled
per request and chunking per date would multiply the calls.

Usage
-----
    python scripts/backtest.py --days 10
    python scripts/backtest.py --date 2026-09-04 --feed dhan
    python scripts/backtest.py --days 60 --feed dhan --sweep
    python scripts/backtest.py --days 10 --universe 20      # faster smoke test
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loguru import logger

from src.analysis.transaction_costs import TransactionCostCalculator
from src.backtest.backtest_engine import BacktestEngine
from src.datafeed import MarketDataFeed, get_feed

NIFTY_SYMBOL = "NIFTY"
NIFTY_TOKEN = "99926000"


def load_stock_list(limit: int | None) -> List[Dict]:
    """Read the Nifty 50 constituents the bot already tracks."""
    path = ROOT / "config" / "nifty50.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stocks = data.get("stocks", [])
    cleaned = [
        {"symbol": s["symbol"], "token": str(s.get("token", "")), "name": s.get("name", "")}
        for s in stocks
        if s.get("symbol")
    ]
    return cleaned[:limit] if limit else cleaned


def session_dates(candles: List[Dict]) -> List[str]:
    """Distinct session dates present in a candle list, oldest first."""
    return sorted({c["timestamp"][:10] for c in candles})


def slice_session(candles: List[Dict], date_str: str) -> List[Dict]:
    """Return the target session plus the one before it.

    The engine infers the test date from the newest candle, so the slice must
    end on ``date_str``. The preceding session supplies the previous close used
    for gap classification.
    """
    dates = [d for d in session_dates(candles) if d <= date_str]
    if len(dates) < 2 or dates[-1] != date_str:
        return []
    keep = {dates[-2], dates[-1]}
    return [c for c in candles if c["timestamp"][:10] in keep]


def fetch_all(feed: MarketDataFeed, stocks: List[Dict], lookback_days: int) -> Dict[str, List[Dict]]:
    """Download 3-minute candles once per symbol."""
    out: Dict[str, List[Dict]] = {}
    total = len(stocks)

    for i, stock in enumerate(stocks, 1):
        symbol, token = stock["symbol"], stock["token"]
        candles = feed.get_historical_data(
            symbol, token, interval="THREE_MINUTE", days=lookback_days
        )
        if candles:
            out[symbol] = candles
            print(f"  [{i:>2}/{total}] {symbol:<16} {len(candles):>5} bars", flush=True)
        else:
            print(f"  [{i:>2}/{total}] {symbol:<16} NO DATA", flush=True)

    return out


def size_and_cost(trade: Dict, capital: float, alloc_pct: float,
                  costs: TransactionCostCalculator) -> Dict:
    """Attach quantity, gross and net rupee P&L to a raw engine trade.

    The engine reports P&L in points per share with no sizing and no fees, so a
    2-point gain on a 268-rupee stock and on a 2300-rupee stock look identical.
    Converting to rupees on a fixed notional per trade, then subtracting real
    intraday charges, is what makes days comparable.
    """
    entry = float(trade.get("entry_price", 0) or 0)
    exit_price = float(trade.get("exit_price", 0) or 0)
    direction = trade.get("direction", "LONG")

    qty = int((capital * alloc_pct / 100.0) // entry) if entry > 0 else 0
    points = float(trade.get("pnl", 0) or 0)
    gross = points * qty

    if qty <= 0:
        fees = 0.0
    elif direction == "LONG":
        fees = costs.calculate_costs(qty, entry, exit_price)["total"]
    else:
        # Short intraday: the sell leg happens first, but charges depend on the
        # two turnovers, not their order.
        fees = costs.calculate_costs(qty, exit_price, entry)["total"]

    enriched = dict(trade)
    enriched.update(
        {
            "quantity": qty,
            "points": points,
            "gross_rupees": gross,
            "fees": fees,
            "net_rupees": gross - fees,
            "return_pct": (points / entry * 100) if entry else 0.0,
        }
    )
    return enriched


def print_day(result, date_str: str, trades: List[Dict]) -> None:
    gap = result.nifty_gap or {}
    status = gap.get("gap_status", "?")
    pct = gap.get("gap_percent", 0.0)

    print(f"\n{'=' * 78}")
    print(f"  {date_str}   Nifty {status} ({pct:+.2f}%)")
    print(f"{'=' * 78}")

    if not gap.get("valid"):
        print(f"  skipped: {gap.get('reason')}")
        return

    for s in result.selected_stocks or []:
        print(f"    selected {s['symbol']:<16} {s['direction']}")

    if not trades:
        for n in result.no_entry_stocks or []:
            print(f"    no entry {n['symbol']:<16} {n.get('reason', '')}")
        return

    for d in trades:
        print(
            f"    {d.get('symbol', ''):<15} {d.get('direction', ''):<5} "
            f"qty {d['quantity']:>4}  entry {d.get('entry_price', 0):>8.2f} "
            f"exit {d.get('exit_price', 0):>8.2f}  {d.get('exit_reason', ''):<14} "
            f"{d['return_pct']:>+6.2f}%  net {d['net_rupees']:>9.2f}"
        )

    print(f"    -- day net: {sum(t['net_rupees'] for t in trades):>9.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="Single session to test (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=10, help="Test the last N available sessions")
    ap.add_argument("--universe", type=int, help="Only use the first N Nifty 50 stocks")
    ap.add_argument("--lookback", type=int, default=28,
                    help="Calendar days of history to download (Yahoo caps 1m at ~30)")
    ap.add_argument("--capital", type=float, default=100000.0, help="Account capital in rupees")
    ap.add_argument("--alloc-pct", type=float, default=25.0,
                    help="Percent of capital deployed per trade (MIS notional)")
    ap.add_argument("--broker", default="zerodha",
                    help="Fee schedule to apply: zerodha, angel_one, upstox")
    ap.add_argument("--sweep", action="store_true",
                    help="Grid the target/stop parameters instead of a single run")
    ap.add_argument("--feed", default="auto", choices=["auto", "dhan", "yfinance"],
                    help="Data source. dhan needs DHAN_* env vars but reaches back 5 years; "
                         "yfinance needs no account but only ~30 days")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    config = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
    stocks = load_stock_list(args.universe)
    feed = get_feed(args.feed, cache_ttl_seconds=3600)

    if not feed.login():
        print(f"ERROR: could not initialise the {args.feed} feed", file=sys.stderr)
        return 1
    print(f"Feed: {feed.name}")

    print(f"Downloading Nifty index + {len(stocks)} stocks ({args.lookback}d of 3-min bars)...")
    nifty = feed.get_historical_data(
        NIFTY_SYMBOL, NIFTY_TOKEN, interval="THREE_MINUTE", days=args.lookback
    )
    if not nifty:
        print("ERROR: could not fetch Nifty index data", file=sys.stderr)
        return 1
    print(f"  Nifty: {len(nifty)} bars over {len(session_dates(nifty))} sessions")

    all_candles = fetch_all(feed, stocks, args.lookback)
    if not all_candles:
        print("ERROR: no stock data fetched", file=sys.stderr)
        return 1

    # Only sessions where the index itself has data are testable.
    available = session_dates(nifty)
    if args.date:
        if args.date not in available:
            print(f"ERROR: {args.date} not available. Have: {available[0]}..{available[-1]}",
                  file=sys.stderr)
            return 1
        targets = [args.date]
    else:
        targets = available[1:][-args.days:]  # skip the oldest: it has no prior session

    costs = TransactionCostCalculator(args.broker)

    def evaluate(target_pct: float, sl_pct: float, verbose: bool):
        """Replay every target session with one parameter set."""
        cfg = json.loads(json.dumps(config))  # deep copy; engine reads nested params
        params = cfg.setdefault("strategies", {}).setdefault("three_minute", {}).setdefault(
            "params", {}
        )
        params["target_percent"] = target_pct
        params["stop_loss_percent"] = sl_pct
        engine = BacktestEngine(cfg)

        trades: List[Dict] = []
        per_day: List[float] = []
        n_sessions = 0

        for date_str in targets:
            nifty_slice = slice_session(nifty, date_str)
            if not nifty_slice:
                continue
            stock_slices = {
                sym: sliced
                for sym, candles in all_candles.items()
                if (sliced := slice_session(candles, date_str))
            }
            if not stock_slices:
                continue

            result = engine.run(date_str, nifty_slice, stock_slices, stocks)
            raw = [t.to_dict() if hasattr(t, "to_dict") else t for t in (result.trades or [])]
            day_trades = [size_and_cost(t, args.capital, args.alloc_pct, costs) for t in raw]

            if verbose:
                print_day(result, date_str, day_trades)
            n_sessions += 1
            trades.extend(day_trades)
            per_day.append(sum(t["net_rupees"] for t in day_trades))

        return trades, per_day, n_sessions

    if args.sweep:
        print(f"\n{'=' * 78}")
        print(f"  PARAMETER SWEEP over {len(targets)} sessions"
              f"   (capital Rs{args.capital:,.0f}, {args.alloc_pct:.0f}%/trade)")
        print(f"{'=' * 78}")
        print(f"  {'target%':>8} {'sl%':>6} {'trades':>7} {'win%':>7} "
              f"{'gross':>11} {'costs':>10} {'NET':>11} {'PF':>6}")
        print(f"  {'-' * 74}")

        for tgt in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
            for sl in (0.5, 1.0, 1.5):
                tr, _, _ = evaluate(tgt, sl, verbose=False)
                if not tr:
                    continue
                net = sum(t["net_rupees"] for t in tr)
                gross = sum(t["gross_rupees"] for t in tr)
                fees = sum(t["fees"] for t in tr)
                w = [t for t in tr if t["net_rupees"] > 0]
                lo = abs(sum(t["net_rupees"] for t in tr if t["net_rupees"] <= 0))
                pf = (sum(t["net_rupees"] for t in w) / lo) if lo else float("inf")
                print(f"  {tgt:>8.2f} {sl:>6.2f} {len(tr):>7} {len(w) / len(tr) * 100:>6.1f}% "
                      f"{gross:>11,.2f} {fees:>10,.2f} {net:>11,.2f} {pf:>6.2f}")

        print()
        print("  Read this as a robustness check, not an optimisation target:")
        print("  picking the best cell of a sweep this small is curve-fitting.")
        return 0

    all_trades, daily_net, sessions = evaluate(
        float(config.get("strategies", {}).get("three_minute", {})
              .get("params", {}).get("target_percent", 1.0)),
        float(config.get("strategies", {}).get("three_minute", {})
              .get("params", {}).get("stop_loss_percent", 1.0)),
        verbose=True,
    )

    net_total = sum(t["net_rupees"] for t in all_trades)
    gross_total = sum(t["gross_rupees"] for t in all_trades)
    fees_total = sum(t["fees"] for t in all_trades)
    wins = [t for t in all_trades if t["net_rupees"] > 0]
    losses = [t for t in all_trades if t["net_rupees"] <= 0]
    n = len(all_trades)

    print(f"\n{'=' * 78}")
    print(f"  AGGREGATE   capital Rs{args.capital:,.0f} | {args.alloc_pct:.0f}% per trade"
          f" | {costs.fees['name']} fees")
    print(f"{'=' * 78}")
    print(f"  Sessions tested : {sessions}")
    print(f"  Trades          : {n}")

    if not n:
        print("  No trades triggered in this window.")
    else:
        win_sum = sum(t["net_rupees"] for t in wins)
        loss_sum = abs(sum(t["net_rupees"] for t in losses))
        print(f"  Win rate        : {len(wins) / n * 100:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Gross P&L       : Rs{gross_total:>12,.2f}")
        print(f"  Costs           : Rs{fees_total:>12,.2f}"
              f"   ({fees_total / abs(gross_total) * 100:.1f}% of gross)"
              if gross_total else f"  Costs           : Rs{fees_total:>12,.2f}")
        print(f"  NET P&L         : Rs{net_total:>12,.2f}"
              f"   ({net_total / args.capital * 100:+.2f}% on capital)")
        print(f"  Avg per trade   : Rs{net_total / n:>12,.2f}")
        if loss_sum:
            print(f"  Profit factor   : {win_sum / loss_sum:.2f}")
        if daily_net:
            best, worst = max(daily_net), min(daily_net)
            green = sum(1 for d in daily_net if d > 0)
            print(f"  Best / worst day: Rs{best:,.2f} / Rs{worst:,.2f}")
            print(f"  Profitable days : {green}/{len(daily_net)}")

    print()
    print("  CAVEATS: fills assume the exact candle close with zero slippage and")
    print("  no impact cost; Yahoo bars are unadjusted and may differ from")
    print(f"  exchange data. {sessions} sessions is far too small to infer an edge.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
