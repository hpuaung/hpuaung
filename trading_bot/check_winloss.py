#!/usr/bin/env python3
"""check_winloss.py — split paper trades into winners vs losers and compare their
characteristics, so we can see WHY trades win and WHY they lose.

    .venv/bin/python check_winloss.py
"""
import warnings
warnings.filterwarnings("ignore")

from collections import Counter
import database as db

db.init_db()

trades = [t for t in db.get_trades() if t.get("paper_mode") == 1
          and (t.get("status") or "closed") == "closed"]
W = [t for t in trades if float(t.get("net_pnl") or 0) > 0]
L = [t for t in trades if float(t.get("net_pnl") or 0) <= 0]


def hold_h(t):
    try:
        return float(str(t.get("hold_duration") or "0h").rstrip("h"))
    except Exception:
        return 0.0


def avg(rows, fn):
    return sum(fn(t) for t in rows) / max(len(rows), 1)


def f(t, k):
    try:
        return float(t.get(k) or 0)
    except Exception:
        return 0.0


print("=" * 60)
print(f"PAPER TRADES: {len(trades)}   WINNERS: {len(W)}   LOSERS: {len(L)}")
if not trades:
    raise SystemExit
print("=" * 60)
print(f"{'metric':22} {'WINNERS':>12} {'LOSERS':>12}")
print(f"{'avg net pnl':22} {avg(W,lambda t:f(t,'net_pnl')):>12.2f} {avg(L,lambda t:f(t,'net_pnl')):>12.2f}")
print(f"{'avg hold (hours)':22} {avg(W,hold_h):>12.2f} {avg(L,hold_h):>12.2f}")
print(f"{'avg lgbm_score':22} {avg(W,lambda t:f(t,'lgbm_score')):>12.2f} {avg(L,lambda t:f(t,'lgbm_score')):>12.2f}")
print(f"{'avg news_score':22} {avg(W,lambda t:f(t,'news_score')):>12.2f} {avg(L,lambda t:f(t,'news_score')):>12.2f}")
print(f"{'avg leverage':22} {avg(W,lambda t:f(t,'leverage')):>12.1f} {avg(L,lambda t:f(t,'leverage')):>12.1f}")


def dist(rows, key):
    return Counter((t.get(key) or "?") for t in rows)


def show_winrate(key, title):
    print(f"--- win% by {title} ---")
    buckets = {}
    for t in trades:
        buckets.setdefault(t.get(key) or "?", []).append(float(t.get("net_pnl") or 0) > 0)
    for k, v in sorted(buckets.items(), key=lambda kv: -100*sum(kv[1])/len(kv[1])):
        print(f"  {str(k):10} win%={100*sum(v)/len(v):3.0f}  n={len(v)}")


print("=" * 60)
show_winrate("side", "side")
show_winrate("session", "session")
show_winrate("strategy", "strategy")

# hold-time buckets — do quick stop-outs dominate the losers?
print("--- losers by hold time ---")
hb = {"<0.2h (instant)": 0, "0.2-1h": 0, ">1h": 0}
for t in L:
    h = hold_h(t)
    hb["<0.2h (instant)" if h < 0.2 else ("0.2-1h" if h < 1 else ">1h")] += 1
for k, v in hb.items():
    print(f"  {k:18} {v}  ({100*v/max(len(L),1):.0f}% of losers)")
