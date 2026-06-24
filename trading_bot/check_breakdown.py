#!/usr/bin/env python3
"""check_breakdown.py — where are the losses coming from? Break paper-mode trades
down by strategy / side / pair / close-reason / SL-distance so we can see which
setups have any edge and which to cut.

    .venv/bin/python check_breakdown.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db

db.init_db()

trades = [t for t in db.get_trades() if t.get("paper_mode") == 1
          and (t.get("status") or "closed") == "closed"]


def grp(rows, keyfn, title):
    print(f"--- by {title} ---")
    buckets = {}
    for t in rows:
        k = keyfn(t)
        buckets.setdefault(k, []).append(float(t.get("net_pnl") or 0))
    for k, v in sorted(buckets.items(), key=lambda kv: sum(kv[1])):
        w = len([x for x in v if x > 0])
        aw = sum(x for x in v if x > 0) / max(w, 1)
        al = sum(x for x in v if x <= 0) / max(len(v) - w, 1)
        print(f"  {str(k):10} n={len(v):<3} net={sum(v):+6.2f} "
              f"win%={100*w/len(v):3.0f} avgW={aw:+.2f} avgL={al:+.2f}")


print("=" * 60)
print(f"PAPER TRADES: {len(trades)}")
net = [float(t.get("net_pnl") or 0) for t in trades]
if trades:
    w = len([x for x in net if x > 0])
    print(f"net={sum(net):+.2f}  win%={100*w/len(trades):.0f}  "
          f"avgW={sum(x for x in net if x>0)/max(w,1):+.2f}  "
          f"avgL={sum(x for x in net if x<=0)/max(len(trades)-w,1):+.2f}")
print("=" * 60)
if trades:
    grp(trades, lambda t: t.get("strategy"), "strategy")
    grp(trades, lambda t: t.get("side"), "side (BUY/SELL)")
    grp(trades, lambda t: t.get("close_reason"), "close reason")
    grp(trades, lambda t: t.get("pair"), "pair")
print("\nNOTE: 23+ trades is a small sample — treat as directional hints, not proof.")
