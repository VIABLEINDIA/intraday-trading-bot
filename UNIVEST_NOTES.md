# Running this bot on Univest

## The constraint

Univest publishes **no trading or market-data API**. Univest Stock Broking is a
SEBI-registered broker (INZ000317437) whose automation offering is *1-tap
execution* of its own research signals inside the app (TradeZapp) — not a
key-based REST API like Angel One, Zerodha or Dhan. There is no `api.univest`
endpoint, no developer portal and no SDK.

This has one hard consequence:

> **Nothing can place orders into a Univest account programmatically.**

So on Univest this repo can be a *signal generator* — it decides what to trade
and when — but a human places the order. That is a real workflow, and it is the
only one Univest supports today.

## What that changed in the code

The bot originally read **every** price from Angel One SmartAPI: LTP, candles,
the 9:15 Nifty gap, previous-day OHLC. Angel One was the data feed, not just the
executor, so even paper mode and the backtester needed SmartAPI credentials.
Without an Angel One account there was no way to run or validate anything.

`src/datafeed/` fixes that:

- `base.py` — `MarketDataFeed`, the market-data half of `AngelOneClient`.
- `yfinance_feed.py` — a Yahoo Finance implementation needing no credentials.
- `symbols.py` — `SBIN-EQ`/`3045` → `SBIN.NS`, `NIFTY`/`99926000` → `^NSEI`.

Two Yahoo quirks are handled explicitly:

- **No 3-minute interval.** 1-minute bars are resampled. NSE opens at 09:15,
  which is 555 minutes past midnight and divisible by 3, so buckets align to the
  open naturally and the 09:15–09:18 reference candle is exact.
- **8 days of 1-minute data per request.** Longer windows are fetched in 7-day
  chunks and stitched, which is what makes backtesting more than two days back
  possible at all.

## Backtesting without any broker account

```bash
pip install yfinance loguru
python scripts/backtest_yf.py --days 20                  # last 20 sessions
python scripts/backtest_yf.py --date 2026-09-04          # one session
python scripts/backtest_yf.py --days 20 --sweep          # target/stop grid
python scripts/backtest_yf.py --days 20 --universe 10    # faster smoke test
```

Yahoo retains roughly **30 days** of 1-minute history, so that is the backtest
horizon. Longer studies need a paid or broker feed.

The runner adds what the engine omits. `BacktestEngine` reports P&L in **points
per share** with no position sizing and no fees, so a 2-point move on a ₹268
stock and on a ₹2,329 stock score identically. The runner applies a fixed
notional per trade (`--capital`, `--alloc-pct`) and the repo's intraday cost
model (`--broker`) to produce comparable rupee figures.

## Result on 20 sessions (Aug–Sep 2026, Nifty 50, ₹1L, 25%/trade)

| | |
|---|---|
| Trades | 28 |
| Win rate | 50.0% |
| Gross P&L | ₹813.85 |
| Costs | ₹709.70 (87% of gross) |
| **Net P&L** | **₹104.15 (+0.10% on capital)** |
| Profit factor | 1.05 |

**Costs eat essentially the whole edge.** Round-trip charges are ~₹25 per trade
on a ₹25,000 notional — about **0.10%** — while the strategy's average gross
edge is roughly 0.03–0.17% per trade depending on parameters. It has to clear
0.10% before slippage just to break even, and these fills assume the exact
candle close with zero slippage and zero impact cost. Real fills will be worse.

The sweep shows the same thing from another angle: costs stay near ₹710 in every
one of the 18 parameter cells while gross swings from −₹92 to +₹1,166. The
result is decided by the cost line, not the parameters.

Two structural notes from the sweep:

- `stop_loss_percent` is nearly a no-op. It applies only when the reference
  candle range exceeds `large_candle_percent`; normally the stop is the
  reference candle low/high. SL 1.0% and 1.5% produced identical results.
- A 1% target against a ~1% stop at a 50% win rate is roughly zero expectancy
  **by construction**. Costs then push it negative.

## Caveats on that result

28 trades over 20 sessions is far too small to conclude anything about edge —
one trade is ~4% of the sample. What it *does* establish robustly is the cost
floor, which is arithmetic rather than a sample statistic. Do not tune
parameters on this window; the best sweep cell is curve-fitting.

## If you want live signals on Univest

The data feed makes a signal-only run possible, but the bot's live loop still
constructs an Angel One websocket for tick data (`src/core/bot.py`). Running
live on the Yahoo feed needs that replaced with a polling loop, plus a delivery
channel (console/Telegram) emitting symbol, side, entry, stop, target and
quantity for manual 1-tap entry in Univest.

Note also that Yahoo quotes are **not guaranteed real-time**. For a 3-minute
strategy that squares off intraday, a delayed feed is acceptable for research
and paper trading but not for live execution. A free real-time alternative is a
Dhan API account used purely as a data source, with execution still manual in
Univest.
