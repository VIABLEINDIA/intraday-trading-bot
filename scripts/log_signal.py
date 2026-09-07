"""Append an advisory call to the signal log.

Logging has to be near-frictionless or it stops happening, and a log that stops
after a week answers nothing. One command, sensible defaults, no editing CSV by
hand:

    python scripts/log_signal.py --symbol RELIANCE --side LONG \\
        --entry 1305.50 --stop 1292 --target 1332

Date and time default to now. Record an exit later with --exit-price, or edit
the row directly.
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.utils.timezone import now_ist

FIELDS = ["date", "time", "symbol", "side", "instrument", "entry", "stop", "target",
          "exit_price", "exit_time", "exit_reason", "source", "notes"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", required=True, help="NSE symbol, e.g. RELIANCE")
    ap.add_argument("--side", required=True, choices=["LONG", "SHORT", "long", "short"])
    ap.add_argument("--entry", help="Advised entry; blank means price at call time")
    ap.add_argument("--stop", help="Advised stop loss")
    ap.add_argument("--target", help="Advised target")
    ap.add_argument("--instrument", default="EQ", choices=["EQ", "FUT", "OPT"])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--time", dest="clock", help="HH:MM IST (default: now)")
    ap.add_argument("--exit-price", help="Actual exit, if already closed")
    ap.add_argument("--exit-time", help="HH:MM of that exit")
    ap.add_argument("--exit-reason", help="TARGET, SL, MANUAL, ...")
    ap.add_argument("--source", default="univest")
    ap.add_argument("--notes", default="")
    ap.add_argument("--file", default="signals/univest_signals.csv")
    args = ap.parse_args()

    now = now_ist()
    row = {
        "date": args.date or now.strftime("%Y-%m-%d"),
        "time": args.clock or now.strftime("%H:%M"),
        "symbol": args.symbol.strip().upper(),
        "side": args.side.upper(),
        "instrument": args.instrument,
        "entry": args.entry or "",
        "stop": args.stop or "",
        "target": args.target or "",
        "exit_price": args.exit_price or "",
        "exit_time": args.exit_time or "",
        "exit_reason": args.exit_reason or "",
        "source": args.source,
        "notes": args.notes,
    }

    path = Path(args.file)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0

    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)

    levels = " ".join(
        f"{k}={row[k]}" for k in ("entry", "stop", "target") if row[k]
    ) or "no levels given"
    print(f"Logged {row['side']} {row['symbol']} @ {row['date']} {row['time']}  ({levels})")
    print(f"  -> {path}   (score with: python scripts/evaluate_signals.py)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
