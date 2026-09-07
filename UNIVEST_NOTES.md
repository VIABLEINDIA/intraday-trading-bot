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
- `yfinance_feed.py` — Yahoo Finance; no credentials, ~30 days of history.
- `dhan_feed.py` — DhanHQ v2; real-time, 5 years of intraday history.
- `bars.py` — resampling shared by both, so the reference candle is defined
  identically no matter which feed supplied the data.
- `symbols.py` — `SBIN-EQ`/`3045` → `SBIN.NS`, `NIFTY`/`99926000` → `^NSEI`.
- `get_feed("auto"|"dhan"|"yfinance")` picks one.

**No vendor serves a 3-minute candle** — Yahoo offers 1/2/5/15/30/60 and Dhan
1/5/15/25/60 — so both feeds fetch 1-minute bars and resample. NSE opens at
09:15, which is 555 minutes past midnight and divisible by 3, so buckets align
to the open and the 09:15–09:18 reference candle is exact. Intervals that do
not divide evenly are anchored per session instead.

Each vendor also caps a single request (Yahoo 8 days of 1m, Dhan 90 days), so
long windows are fetched in chunks and stitched. Without that, every backtest
date older than two sessions failed silently on Yahoo.

Dhan's `timestamp` epoch convention is ambiguous in the docs, and getting it
wrong shifts every bar by 5h30m — silently moving the 09:15–09:18 reference
candle. Rather than hardcode a guess, the feed infers it: NSE trades
09:15–15:30, so whichever interpretation lands the bars inside the session is
correct, and the verdict is cached.

Measured against the live API, Dhan ships **true UTC** epochs (100% vs 8%
in-session match) — the opposite of the commonly assumed IST wall-clock. Both
conventions are unit-tested to yield identical IST bars, so the feed is correct
either way if Dhan ever changes.

Cross-checked against Yahoo over 600 overlapping 3-minute bars, the two
independent feeds agree to a mean absolute close difference of **0.0069%**
(max 0.49%), with total volume within 0.14%.

## Backtesting without any broker account

```bash
pip install yfinance loguru
python scripts/backtest.py --days 20                  # last 20 sessions
python scripts/backtest.py --date 2026-09-04          # one session
python scripts/backtest.py --days 20 --sweep          # target/stop grid
python scripts/backtest.py --days 20 --universe 10    # faster smoke test
```

## Choosing a feed

`--feed auto` (the default) uses Dhan when `DHAN_CLIENT_ID` and
`DHAN_ACCESS_TOKEN` are set, else Yahoo.

| | Yahoo | Dhan |
|---|---|---|
| Account needed | no | yes |
| 1-minute history | ~30 days | **5 years** |
| Real-time | not guaranteed | yes |
| Per request | 8 days | 90 days |

Yahoo's ~30 days is enough to prove the pipeline works but far too short to
judge an edge. Use Dhan for anything conclusive:

```bash
export DHAN_CLIENT_ID=...      # your dhanClientId
export DHAN_ACCESS_TOKEN=...   # web.dhan.co -> DhanHQ Trading APIs
python scripts/backtest.py --days 250 --feed dhan --sweep
```

Dhan access tokens expire quickly (typically 24h). When one lapses every call
returns `DH-901` and the feed says so explicitly — regenerate and re-export.
Execution remains manual in Univest; Dhan is used purely as a data source.

Dhan's `securityId` for NSE equities is the NSE exchange token, the same number
this repo already stores as the Angel One token — verified identical for all 50
Nifty constituents — so no mapping table is needed for stocks. Indices differ
(Nifty 50 is `13`, segment `IDX_I`) and are mapped explicitly.

The runner adds what the engine omits. `BacktestEngine` reports P&L in **points
per share** with no position sizing and no fees, so a 2-point move on a ₹268
stock and on a ₹2,329 stock score identically. The runner applies a fixed
notional per trade (`--capital`, `--alloc-pct`) and the repo's intraday cost
model (`--broker`) to produce comparable rupee figures.

## Result: one year, 248 sessions, Dhan data

Nifty 50, ₹1L capital, 25% notional per trade, Zerodha intraday fees.

| | |
|---|---|
| Sessions | 248 |
| Trades | 279 |
| Win rate | 44.1% |
| **Gross P&L** | **−₹1,076.92** (negative *before* costs) |
| Costs | ₹7,141.60 |
| **Net P&L** | **−₹8,218.52 (−8.22% on capital)** |
| Profit factor | 0.72 |
| Profitable days | 75/248 (30%) |

**The strategy has no edge.** It loses money before a single rupee of
brokerage is paid, and costs then triple the loss. This is not a
cost-optimisation problem — there is nothing underneath the costs to rescue.

An earlier 20-session Yahoo sample showed +₹104 net and looked roughly
breakeven. That was noise: 28 trades is ~4% of a year, and one trade moved it
several percent. The lesson is that the short sample was not merely imprecise,
it pointed the wrong way.

### Every parameter combination loses

The full target × stop grid over the same 248 sessions:

| target% | 0.5 SL | 1.0 SL | 1.5 SL |
|---|---|---|---|
| 0.50 | −8,264 | −5,643 | −5,222 |
| 0.75 | −10,765 | −7,933 | −8,448 |
| 1.00 | −10,791 | **−8,219** | −9,414 |
| 1.50 | −9,467 | −5,586 | −6,247 |
| 2.00 | −7,490 | −5,365 | −6,822 |
| 3.00 | −6,956 | **−3,985** | −5,632 |

Profit factor ranges 0.57–0.87 and never reaches 1.0. Gross P&L is negative in
10 of 18 cells; the best gross (+₹3,158) still fails to cover ₹7,143 of costs.
Costs are near-constant at ~₹7,142 because trade count does not change — the
grid only moves exits, not entries.

The least-bad cell (3% target) is still −4% on capital for a year of daily
screen time. Do not read it as a configuration to adopt.

### Why it fails

- **Entry frequency is fixed.** 279 trades regardless of parameters, so costs
  are a fixed ~₹7,142 toll paid before any edge is earned.
- **The gap-fade premise does not hold.** Shorting the strongest gap-up stocks
  and buying the weakest gap-down ones is a mean-reversion bet, and a 3-minute
  breakout confirmation does not select the reverting subset. Win rate falls as
  the target widens (49.5% → 31.5%), which is what a directionless entry looks
  like.
- **`stop_loss_percent` barely matters.** It applies only when the reference
  candle range exceeds `large_candle_percent`; otherwise the stop is the
  reference candle low/high.

## Caveats on the caveats

248 sessions and 279 trades is a real sample, not a toy one, and the result is
consistent across all 18 parameter sets — so "no edge" is a robust conclusion,
not a sampling artefact. Two things still flatter these numbers: fills assume
the exact candle close with zero slippage and no impact cost, and there is no
survivorship correction for the Nifty 50 constituent list, which is today's
membership applied to the past year. Both biases push the true result *lower*.

## What to do next

Do not build execution or alerting on top of this strategy — that is machinery
on a negative expectancy. The infrastructure (feeds, backtester, cost model) is
sound and reusable; the strategy is what failed. Reasonable next steps:

1. **Test a different premise.** The current one fades gap extremes. Momentum
   continuation — going *with* the gap rather than against it — is the obvious
   opposite to try, and is a one-line change to the direction assignment in
   `_select_stocks`.
2. **Cut trade frequency.** Costs are a fixed ~₹7,142/year toll at 279 trades.
   Any viable strategy must either trade far less or earn far more per trade.
3. **Validate before building.** This whole exercise cost one afternoon and one
   Dhan token because the backtest came before the execution layer. Keep that
   order.

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
