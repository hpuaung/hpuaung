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

    _provenance()

    print("\nHow to read this: if the open swing positions are mostly in profit\n"
          "and none has run long enough to reach 3R yet, the missing winners are\n"
          "still in flight and the closed sample is simply too young. If they are\n"
          "flat or negative and have been open past the backtest's median hold,\n"
          "the entries are not behaving like the backtest's and that is the\n"
          "problem to chase -- not the strategy choice.")
    return 0


def _provenance() -> None:
    """Which timeframe and which strategy actually produced these trades.

    The sweep measured trend at 12h, where the median hold is 13.5 days. If
    the live stops were being hit in 1.4 days the trades may simply not have
    been taken on 12h -- these columns say so instead of leaving it to guesswork.
    """
    try:
        rows = db.get_conn().execute(
            "SELECT strategy, COALESCE(NULLIF(timeframe,''),'(not recorded)') tf, "
            "COALESCE(NULLIF(strategy_name,''),'(not recorded)') name, "
            "COUNT(*) n, SUM(net_pnl) total FROM trades "
            "GROUP BY strategy, tf, name ORDER BY n DESC").fetchall()
    except Exception as e:  # noqa: BLE001  (columns predate the migration)
        print(f"\nprovenance unavailable: {e}")
        return
    print(f"\n=== what actually produced these trades ===")
    print(f"{'slot':11}{'timeframe':16}{'strategy':18}{'n':>5}{'total':>10}")
    for r in rows:
        print(f"{(r['strategy'] or '?'):11}{r['tf']:16}{r['name']:18}"
              f"{r['n']:>5}{(r['total'] or 0):>+10.2f}")
    print(f"\nmtf_filter: swing={db.get_bool('swing_mtf_filter', True)}, "
          f"scalping={db.get_bool('scalping_mtf_filter', True)}"
          "   (the sweep ran with mtf=False)")
    _by_config()


def _by_config() -> None:
    """Results split by the settings each trade was taken under.

    This is what makes a settings change survivable: trades carry the config
    that produced them, so changing something opens a new bucket and leaves
    the earlier buckets intact and comparable, rather than forcing the whole
    measurement to start over.
    """
    try:
        rows = db.get_conn().execute(
            "SELECT COALESCE(NULLIF(config_id,''),'(before tracking)') cid, "
            "COALESCE(NULLIF(config_desc,''),'unlabelled') d, COUNT(*) n, "
            "SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) w, SUM(net_pnl) total "
            "FROM trades GROUP BY cid, d ORDER BY n DESC").fetchall()
    except Exception as e:  # noqa: BLE001  (column predates the migration)
        print(f"\nconfig breakdown unavailable: {e}")
        return
    print("\n=== results per configuration ===")
    print(f"{'config':10}{'settings':38}{'n':>5}{'win%':>7}{'total':>10}")
    for r in rows:
        n = r["n"] or 0
        win = f"{100 * (r['w'] or 0) // n}" if n else "-"
        print(f"{r['cid']:10}{r['d'][:37]:38}{n:>5}{win:>7}{(r['total'] or 0):>+10.2f}")
    cur_s = db.config_snapshot("swing")
    cur_c = db.config_snapshot("scalping")
    print(f"\nrunning now:  swing {cur_s[0]}  {cur_s[1]}")
    print(f"              scalping {cur_c[0]}  {cur_c[1]}")
    print("A settings change gives a new config id. Earlier ids keep their\n"
          "results, so the history is split, not thrown away.")


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
