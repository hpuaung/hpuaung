"""Was the balance flat because the size was small, or because the edge was?

    .venv/bin/python why_flat.py           # real trades
    .venv/bin/python why_flat.py --paper   # paper trades

Risk per trade is a multiplier. It scales whatever expectancy the bot has --
including a negative one. So "we traded too small" is only a real explanation
if the trades themselves were profitable on average. This reads the actual
trades table and settles it, per strategy and overall, by showing what the
same trades would have come to at 2x and 4x the size.
"""
from __future__ import annotations

import sys

import database as db


def main() -> int:
    paper = 1 if "--paper" in sys.argv else 0
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT strategy, close_reason, net_pnl, entry_timestamp, exit_timestamp "
        "FROM trades WHERE paper_mode=? AND net_pnl IS NOT NULL "
        "ORDER BY id", (paper,)).fetchall()
    if not rows:
        print(f"No {'paper' if paper else 'real'} trades recorded.")
        return 1

    rows = [dict(r) for r in rows]
    span = _span_days(rows)
    print(f"{len(rows)} {'paper' if paper else 'real'} trades"
          + (f" over ~{span:.0f} days\n" if span else "\n"))

    print(f"{'strategy':14}{'n':>6}{'win%':>7}{'avg win':>10}{'avg loss':>10}"
          f"{'total':>11}{'per trade':>11}")
    print("-" * 69)
    by = {}
    for r in rows:
        by.setdefault(r["strategy"] or "(none)", []).append(r["net_pnl"])
    for name in sorted(by, key=lambda k: -len(by[k])):
        _line(name, by[name])
    print("-" * 69)
    allp = [r["net_pnl"] for r in rows]
    _line("ALL", allp)

    total = sum(allp)
    print(f"\nWhat the SAME trades would have come to at a bigger size:")
    for mult, label in ((1, "as traded"), (2, "2x size"), (4, "4x size")):
        print(f"  {label:12} {total * mult:>+10.2f}")

    print()
    if total > 0:
        print("Expectancy is POSITIVE, so size was holding the result back:")
        print("trading larger would have made more. Raising risk is the fix.")
    else:
        print("Expectancy is NEGATIVE. Size was NOT the problem -- trading")
        print("larger would have LOST more, faster. The small risk is what")
        print("kept the balance near its starting point instead of below it.")
        print("The edge had to be fixed first; risk only multiplies it.")

    print("\nBy close reason:")
    reasons = {}
    for r in rows:
        reasons.setdefault(r["close_reason"] or "(none)", []).append(r["net_pnl"])
    for name in sorted(reasons, key=lambda k: -len(reasons[k])):
        v = reasons[name]
        print(f"  {name:22}{len(v):>5}{sum(v):>+11.2f}")
    return 0


def _line(name: str, pnls: list) -> None:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    print(f"{name:14}{n:>6}{100 * len(wins) // n:>7}{aw:>10.2f}{al:>10.2f}"
          f"{sum(pnls):>+11.2f}{sum(pnls) / n:>+11.3f}")


def _span_days(rows: list) -> float:
    stamps = sorted(r["entry_timestamp"] or r["exit_timestamp"] or "" for r in rows)
    stamps = [s for s in stamps if s]
    if len(stamps) < 2:
        return 0.0
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return (datetime.strptime(stamps[-1][:19], fmt)
                - datetime.strptime(stamps[0][:19], fmt)).total_seconds() / 86400
    except ValueError:
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
