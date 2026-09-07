# Intraday Trading Bot (NSE)

An automated intraday trading bot for Indian equities, with a research harness
for deciding whether a strategy is worth running at all.

It can run entirely **without a broker account**: market data comes from a
pluggable feed (Dhan or Yahoo Finance), execution is simulated, and the live
loop drives the same strategy code either way.

---

## Status: the bundled strategy does not work

The implemented strategy is a 3-minute opening-range breakout on Nifty 50 gap
extremes. Measured over 248 sessions (~1 year) of real NSE data, ₹1L capital,
25% notional per trade, Zerodha intraday charges:

| | |
|---|---|
| Trades | 279 |
| Win rate | 44.1% |
| Gross P&L | **−₹1,077** (negative before costs) |
| Costs | ₹7,142 |
| **Net P&L** | **−₹8,219 (−8.22% on capital)** |
| Profit factor | 0.72 |

All 18 target/stop combinations lose. Flipping the premise from fade to
momentum improves it materially — gross turns positive (+₹4,546), win rate
rises to 51.9% — but still nets negative, and break-even sits at **2.6 basis
points of slippage per leg**, below realistic NSE friction.

A signal-significance test settles it: at seven horizons from 30 minutes to 5
days, with no stops, targets or costs, **no horizon clears |t| > 2 once market
beta is removed**. The entry carries no measurable information.

Full workings, including the parameter grids and slippage curves, are in
[UNIVEST_NOTES.md](UNIVEST_NOTES.md).

**Treat this repo as a research harness with a worked negative example, not as
a money-making bot.** Paper trade anything you build here, and read the cost
and slippage sections before believing any backtest — including your own.

---

## Quick start

```bash
pip install -r requirements.txt

# Backtest with no account of any kind (Yahoo, ~30 days of history)
python scripts/backtest.py --days 20

# Deeper history via Dhan (5 years available, 90 days per request)
export DHAN_CLIENT_ID=...      # your dhanClientId
export DHAN_ACCESS_TOKEN=...   # web.dhan.co -> DhanHQ Trading APIs
python scripts/backtest.py --feed dhan --days 250 --sweep
```

Run the bot itself with `python run.py`, and open the dashboard at
`http://localhost:5000`.

---

## The implemented strategy

A 3-minute breakout on the opening range, sized by the Nifty's gap. The exact
rules are in [STRATEGY_RULES.md](STRATEGY_RULES.md); any deviation in code is a
bug.

```
~9:10  Rank Nifty 50 by pre-open gap: top 4 and bottom 4
 9:15  Classify the Nifty gap (>+0.2% UP, <-0.2% DOWN, else FLAT)
       Pick stocks and lock a direction for the day
 9:18  Reference candle (9:15-9:18) complete; record its high and low
 9:18+ Enter when a 3-minute candle CLOSES beyond that range
       Stop: opposite end of the reference candle (or 1% on wide candles)
       Target: 1% from entry
15:00  No new entries
15:15  Square off everything
```

`direction_mode` selects the premise: `fade` (short the strongest gap-ups —
the original rules) or `momentum` (trade with the gap).

---

## Market data

Two feeds implement one interface (`src/datafeed/base.py`), so the strategy,
backtester and live loop are indifferent to which is in use.

| | Yahoo | Dhan |
|---|---|---|
| Account needed | no | yes |
| 1-minute history | ~30 days | **5 years** |
| Real-time | not guaranteed | yes |
| Max per request | 8 days | 90 days |

Neither vendor serves a 3-minute candle, so both fetch 1-minute bars and
resample. NSE opens at 09:15 — 555 minutes past midnight, divisible by 3 — so
buckets align to the open and the reference candle is exact.

The two feeds were cross-checked over 600 overlapping bars: mean absolute close
difference **0.0069%**, total volume within 0.14%.

Select one in `config/settings.json`:

```json
{
  "trading_mode": "paper",
  "data_source": "dhan",      // "angel" | "dhan" | "yfinance"
  "data_poll_seconds": 3
}
```

Angel One pushes ticks over a websocket; the other feeds are polled on a timer
and emit the same callbacks, so the live loop cannot tell them apart. Live
trading requires `data_source: "angel"` — the others are data-only and refuse
to place orders rather than faking a fill.

---

## Research tools

```bash
# Single run, or a target/stop grid, on either premise
python scripts/backtest.py --feed dhan --days 250 --direction momentum
python scripts/backtest.py --feed dhan --days 250 --direction both
python scripts/backtest.py --feed dhan --days 250 --sweep

# Charge slippage per leg -- usually the number that decides everything
python scripts/backtest.py --feed dhan --days 250 --slippage-bps 3

# Does the entry predict anything at all, at any horizon?
python scripts/signal_horizon.py --cache-dir .cache --direction momentum
```

`--cache-dir` stores downloaded candles so repeat runs and sweeps are instant
rather than re-fetching ~150 requests.

Two things the raw `BacktestEngine` does not do, which the runner adds: it
reports P&L in **points per share** with no position sizing, and it applies **no
fees**. A 2-point move on a ₹268 stock and on a ₹2,329 stock score identically
until you fix both.

### Scoring an advisory you follow

Univest and similar services publish a hit rate on gross moves, which omits the
two things that decide whether following them pays: costs, and how much of the
move was the market. Log calls by hand and score them properly:

```bash
python scripts/log_signal.py --symbol RELIANCE --side LONG \
    --entry 1305.50 --stop 1292 --target 1332
python scripts/evaluate_signals.py --feed dhan --slippage-bps 3
```

See [signals/README.md](signals/README.md). The report refuses to conclude
anything below 20 signals.

---

## Risk management

Configured in `config/settings.json`, enforced in `src/strategy/risk_manager.py`:

| setting | default | meaning |
|---|---|---|
| `max_daily_loss_percent` | 2.0 | halt trading for the day |
| `max_trades_per_day` | 2 | overtrading limit |
| `max_position_size_percent` | 25 | cap per position |
| `square_off_time` | 15:15 | no overnight exposure |

Transaction costs (`src/analysis/transaction_costs.py`) model brokerage, STT,
exchange charges, GST, SEBI fees and stamp duty for Zerodha, Angel One and
Upstox. Note that intraday STT is 0.025% on the sell side only, while delivery
pays 0.1% on **both** — holding longer to amortise costs does not work the way
it first appears.

---

## Architecture

```
src/
├── datafeed/          # Broker-independent market data
│   ├── base.py            # MarketDataFeed interface
│   ├── dhan_feed.py       # DhanHQ v2 (real-time, 5y history)
│   ├── yfinance_feed.py   # Yahoo (no account, ~30d)
│   ├── bars.py            # Resampling shared by both feeds
│   ├── polling.py         # Websocket-shaped tick source
│   ├── broker_adapter.py  # Feed dressed as the broker client
│   ├── instruments.py     # NSE symbol -> security id
│   └── symbols.py         # Angel/Yahoo symbol translation
├── strategy/
│   ├── three_minute_strategy.py
│   ├── risk_manager.py
│   └── strategy_registry.py
├── analysis/          # Pre-market, pickers, indicators, costs
├── broker/            # Angel One client, websocket, paper trader
├── executor/          # Order manager, position tracker
├── core/              # Bot orchestration, config, scheduler
└── api/               # Flask server for the dashboard

scripts/
├── backtest.py            # Backtest, sweeps, slippage
├── signal_horizon.py      # Signal significance test
├── log_signal.py          # Record an advisory call
└── evaluate_signals.py    # Score recorded calls
```

Angel One is an **optional** dependency, in the requirements as well as the
code. `requirements.txt` covers backtesting, research, paper trading, the
dashboard and both credential-free feeds; the SmartApi SDK and its helpers live
in `requirements-angel.txt` and are only needed for live trading:

```bash
pip install -r requirements.txt -r requirements-angel.txt
```

The client imports SmartApi at module level, so it is imported lazily and a
missing SDK is reported only when `data_source` is `angel`.

---

## Configuration

`.env` (only for Angel One live trading):

```
ANGEL_CLIENT_ID / ANGEL_PASSWORD / ANGEL_TOTP_SECRET
ANGEL_TRADING_API_KEY / ANGEL_HISTORICAL_API_KEY / ANGEL_MARKET_API_KEY
```

`DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` for the Dhan feed. Dhan tokens are
short-lived; when one expires every call returns `DH-901` and the feed says so
explicitly.

---

## Notes

Earlier versions of this README documented a VWAP+RSI strategy and an OHL
strategy, with a claimed 70% win rate. Both strategies were removed from the
code in `4ff4638`, and that win rate was never reproducible from any stated
sample or cost basis. Both have been dropped from this document rather than
left to mislead.

## Disclaimer

For education and research. Trading carries risk of loss; intraday leverage
magnifies it. Nothing here is investment advice. The bundled strategy loses
money on a year of historical data — do not run it with real capital.
