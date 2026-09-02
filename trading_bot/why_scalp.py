#!/usr/bin/env python3
"""why_scalp.py — attribute SCALPING trades to the sub-strategy that fired them
(Trend / Reversion / Breakout) by joining each trade to the nearest matching
entry in the signals log, then report win/loss + net by strategy, by pair, and
the root-cause metrics. Reads the live DB on the VPS.

  .venv/bin/python why_scalp.py            # scalping (default)
  .venv/bin/python why_scalp.py swing      # swing (read-only; does not change it)
"""
import sys
import warnings
warnings.filterwarnings("ignore")

from collections import defaultdict
import database as db

db.init_db()
ENGINE = sys.argv[1] if len(sys.argv) > 1 else "scalping"

trades = [t for t in db.get_trades(paper_mode=1)
          if t.get("strategy") == ENGINE and (t.get("status") or "closed") == "closed"]
sigs = db.get_recent_signals(8000)


def _num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def match_strategy(tr):
    """Find the signal that most likely fired this trade: same pair, same side,
    timestamp at/just before the entry."""
    et = tr.get("entry_timestamp") or tr.get("timestamp") or ""
    side = tr.get("side")
    best = None
    for s in sigs:
        if s.get("pair") != tr.get("pair"):
            continue
        if s.get("action") not in (side, "BUY", "SELL"):
            continue
        if s.get("action") != side:
            continue
        st = s.get("timestamp") or ""
        if st <= et:
            if best is None or st > best[0]:
                best = (st, s.get("strategies") or "?")
    return best[1] if best else "unknown"


def report(label, rs):
    if not rs:
        print(f"  {label:22} no trades")
        return
    n = len(rs); w = [x for x in rs if _num(x["net_pnl"]) > 0]
    net = sum(_num(x["net_pnl"]) for x in rs)
    gW = sum(_num(x["net_pnl"]) for x in w)
    gL = -sum(_num(x["net_pnl"]) for x in rs if _num(x["net_pnl"]) <= 0)
    pf = gW / gL if gL > 0 else 99.0
    flag = "🟢" if net > 0 else "🔴"
    print(f"  {label:22} net=${net:+7.2f} n={n:<3} win%={100*len(w)//n:<3} PF={pf:.2f} {flag}")


print("=" * 62)
print(f"{ENGINE.upper()} — attribution by sub-strategy (from signals log)")
print(f"trades={len(trades)}  signals in log={len(sigs)}")
print("=" * 62)

by_strat = defaultdict(list)
for t in trades:
    by_strat[match_strategy(t)].append(t)

print("BY TRIGGERING STRATEGY:")
for k in sorted(by_strat, key=lambda x: sum(_num(t["net_pnl"]) for t in by_strat[x])):
    report(k, by_strat[k])

print("\nBY PAIR:")
by_pair = defaultdict(list)
for t in trades:
    by_pair[t["pair"]].append(t)
for k in sorted(by_pair, key=lambda x: sum(_num(t["net_pnl"]) for t in by_pair[x])):
    report(k, by_pair[k])

print("\nROOT CAUSE:")
report("ALL", trades)
wins = [_num(t["net_pnl"]) for t in trades if _num(t["net_pnl"]) > 0]
loss = [_num(t["net_pnl"]) for t in trades if _num(t["net_pnl"]) <= 0]
aw = sum(wins) / len(wins) if wins else 0
al = sum(loss) / len(loss) if loss else 0
fees = sum(_num(t["fees_paid"]) for t in trades)
net = sum(_num(t["net_pnl"]) for t in trades)
print(f"  avg win=${aw:+.2f}  avg loss=${al:+.2f}  (win must be >= |loss| to profit at 50%)")
print(f"  total fees=${fees:.2f}  vs net=${net:.2f}"
      + (f"  -> fees {100*fees/abs(net):.0f}% of the damage" if net < 0 else ""))
sl = [t for t in trades if t.get("close_reason") == "SL"]
print(f"  SL trades: {len(sl)}  net=${sum(_num(t['net_pnl']) for t in sl):.2f}")
