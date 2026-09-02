"""Are the missing winners still open, or did they never exist?

    .venv/bin/python trade_forensics.py

In a 1:3 system losers resolve fast -- the stop is 1R away -- while winners
have to travel 3R, so a short observation window records almost every loser
and only some of the winners. A run of stop-losses with no targets is what
that censoring looks like, and it is also what a broken strategy looks like.
The two are told apart by the open positions: if the winners are real they
are sitting in the book, unrealised and in profit.

This prints, per slot: how the closed trades resolved and how long each kind
took, then every open position with its current R, so the unresolved half of
the sample is visible instead of assumed.
"""
from __future__ import annotations

import statistics

import database as db
from utils import binance_client as bc

API = "real"      # public price endpoint, no key needed


def main() -> int:
    closed = [dict(r) for r in db.get_conn().execute(
        "SELECT strategy, close_reason, net_pnl, entry_timestamp, exit_timestamp "
        "FROM trades WHERE net_pnl IS NOT NULL ORDER BY id")]
    for slot in ("swing", "scalping"):
        rows = [r for r in closed if r["strategy"] == slot]
        print(f"\n=== {slot} — {len(rows)} closed ===")
        if rows:
            print(f"{'reason':12}{'n':>5}{'total':>10}{'median hold':>14}")
            groups = {}
            for r in rows:
                groups.setdefault(r["close_reason"] or "(none)", []).append(r)
            for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                holds = [h for h in (_hold_days(r) for r in g) if h is not None]
                med = f"{statistics.median(holds):.1f}d" if holds else "-"
                print(f"{name:12}{len(g):>5}{sum(x['net_pnl'] for x in g):>+10.2f}"
                      f"{med:>14}")
        _open_book(slot)

    print("\nHow to read this: if the open swing positions are mostly in profit\n"
          "and none has run long enough to reach 3R yet, the missing winners are\n"
          "still in flight and the closed sample is simply too young. If they are\n"
          "flat or negative and have been open past the backtest's median hold,\n"
          "the entries are not behaving like the backtest's and that is the\n"
          "problem to chase -- not the strategy choice.")
    return 0


def _open_book(slot: str) -> None:
    pos = db.get_open_positions(strategy=slot)
    if not pos:
        print("  no open positions")
        return
    print(f"\n  {len(pos)} open:")
    print(f"  {'pair':12}{'side':6}{'entry':>12}{'now':>12}{'R now':>8}"
          f"{'age':>7}{'to TP':>8}")
    for p in pos:
        entry = float(p.get("entry_price") or 0)
        sl = float(p.get("sl_price") or 0)
        tp = float(p.get("tp1") or p.get("tp1_price") or 0)
        try:
            now = float(bc.get_price(p["symbol"], API))
        except Exception:  # noqa: BLE001
            now = 0.0
        risk = abs(entry - sl)
        long = (p.get("side") or "").upper() in ("BUY", "LONG")
        r_now = ((now - entry) if long else (entry - now)) / risk if risk and now else 0.0
        to_tp = abs(tp - entry) / risk if risk and tp else 0.0
        age = _hold_days({"entry_timestamp": p.get("entry_timestamp"),
                          "exit_timestamp": None}, to_now=True)
        print(f"  {p['symbol']:12}{(p.get('side') or ''):6}{entry:>12.4f}{now:>12.4f}"
              f"{r_now:>+8.2f}{(f'{age:.1f}d' if age is not None else '-'):>7}"
              f"{to_tp:>8.1f}R")


def _hold_days(row, to_now: bool = False):
    from datetime import datetime, timezone
    fmt = "%Y-%m-%d %H:%M:%S"
    start = (row.get("entry_timestamp") or "")[:19]
    if not start:
        return None
    try:
        a = datetime.strptime(start, fmt)
        if to_now:
            b = datetime.now(timezone.utc).replace(tzinfo=None)
        else:
            end = (row.get("exit_timestamp") or "")[:19]
            if not end:
                return None
            b = datetime.strptime(end, fmt)
    except ValueError:
        return None
    return (b - a).total_seconds() / 86400


if __name__ == "__main__":
    raise SystemExit(main())
