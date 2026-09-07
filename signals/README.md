# Advisory signal log

Univest has no API, so calls are logged by hand here and scored by
`scripts/evaluate_signals.py`. The point is to find out whether the advice you
act on is positive **after costs and market beta** — the two things a published
hit rate never accounts for.

## Recording a signal

Append a row to `univest_signals.csv`, either by editing it or with:

```bash
python scripts/log_signal.py --symbol RELIANCE --side LONG \
    --entry 1305.50 --stop 1292 --target 1332
```

That stamps today's date and the current time. Pass `--date`/`--time` to log
one after the fact.

## Columns

| column | required | meaning |
|---|---|---|
| `date` | yes | `YYYY-MM-DD` the call was given |
| `time` | yes | `HH:MM` the call was given (IST) |
| `symbol` | yes | NSE symbol, e.g. `RELIANCE`. Resolved to a security id automatically |
| `side` | yes | `LONG` or `SHORT` |
| `instrument` | no | `EQ` (default), `FUT`, or `OPT` |
| `entry` | no | Advised entry. Blank means "at the price when the call was given" |
| `stop` | no | Advised stop loss |
| `target` | no | Advised target |
| `exit_price` | no | Your actual exit, if you want the real fill scored |
| `exit_time` | no | `HH:MM` of that exit |
| `exit_reason` | no | Free text, e.g. `TARGET`, `SL`, `MANUAL` |
| `source` | no | Which advisory, default `univest` |
| `notes` | no | Anything you want to remember |

## Two ways a signal gets scored

**Reconstructed** — leave `exit_price` blank. The evaluator pulls the day's
1-minute bars, enters at the advised entry (or the price at the call time),
then walks forward checking whether stop or target hit first, squaring off at
15:15 if neither did. This is what the *advice* was worth, independent of how
you traded it.

**Reported** — fill in `exit_price`. The evaluator scores that fill directly.
Use this for options, whose historical prices this repo does not fetch, and any
time you want your actual execution measured rather than the idealised call.

Both paths then get the same treatment: position sizing, real Indian intraday
charges, optional slippage, and a market-adjusted significance test.

## How many do you need?

Thirty gives a first read on the sign. A hundred starts to be meaningful. Fewer
than about twenty tells you almost nothing — as this project has already
demonstrated, a 20-session sample of a losing strategy showed a *profit*.

`univest_signals.csv` is git-ignored: it is your trading record, not
repository content.
