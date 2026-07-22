#!/usr/bin/env python3
"""autopsy.py — full post-mortem of the paper trades on the dashboard.

Answers three questions in ONE paste:
  1. WHY is it losing when win/loss counts are close?  (realized R:R)
  2. WHICH factor bleeds money?  (strategy / pair / side / close-reason to BAN)
  3. WHICH factor makes money?   (what to keep and replicate)

Plus a config-era check: are these trades from the OLD messy debugging period,
or from the current proven config? If they're contaminated, PF 0.41 is
meaningless and the honest move is to reset paper and measure clean.

    cd /root/hpuaung/trading_bot && .venv/bin/python autopsy.py
"""
import warnings
warnings.filterwarnings("ignore")

from collections import defaultdict
import database as db

db.init_db()


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def date_of(t):
    s = t.get("entry_timestamp") or t.get("timestamp") or ""
    return s[:10] if s else "?"


# ---- load ----
allt = db.get_trades()
closed = [t for t in allt if (t.get("status") or "closed") == "closed"]
paper = [t for t in closed if t.get("paper_mode") == 1]
real = [t for t in closed if t.get("paper_mode") != 1]

print("=" * 66)
print("TRADE AUTOPSY")
print("=" * 66)
print(f"all trades in DB: {len(allt)}   closed: {len(closed)}   "
      f"paper_mode=1: {len(paper)}   other: {len(real)}")
print("(dashboard 'Paper only' should match paper_mode=1 count)")

T = paper if paper else closed          # analyse the paper set (fallback: all)
if not T:
    raise SystemExit("no trades to analyse")


def block(rows, title, keyfn, sort_by_net=True, limit=None):
    print(f"\n--- by {title} ---")
    b = defaultdict(list)
    for t in rows:
        b[keyfn(t)].append(num(t.get("net_pnl")))
    items = sorted(b.items(), key=lambda kv: sum(kv[1]) if sort_by_net else str(kv[0]))
    if limit:
        items = items[:limit]
    for k, v in items:
        w = [x for x in v if x > 0]
        loss = [x for x in v if x <= 0]
        aw = sum(w) / max(len(w), 1)
        al = sum(loss) / max(len(loss), 1)
        gW = sum(w); gL = -sum(loss)
        pf = gW / gL if gL > 0 else 99.0
        flag = " BLEED" if sum(v) < 0 else " green"
        print(f"  {str(k):11} n={len(v):<3} net={sum(v):+7.2f} "
              f"win%={100*len(w)//len(v):>3} avgW={aw:+5.2f} avgL={al:+5.2f} "
              f"PF={pf:4.2f}{flag}")


def overall(rows, label):
    net = [num(t.get("net_pnl")) for t in rows]
    w = [x for x in net if x > 0]
    loss = [x for x in net if x <= 0]
    aw = sum(w) / max(len(w), 1)
    al = sum(loss) / max(len(loss), 1)
    gW = sum(w); gL = -sum(loss)
    pf = gW / gL if gL > 0 else 99.0
    rr = aw / abs(al) if al else 0
    print(f"\n{label}: n={len(rows)} net={sum(net):+.2f} win%={100*len(w)//max(len(rows),1)} "
          f"PF={pf:.2f}")
    print(f"  avg WIN = ${aw:+.2f}   avg LOSS = ${al:+.2f}   "
          f"realized R:R = {rr:.2f} : 1")
    print(f"  -> to break even at win%={100*len(w)//max(len(rows),1)}, "
          f"need avgWin >= ${abs(al)*len(loss)/max(len(w),1):.2f}. "
          f"you have ${aw:.2f}.")


overall(T, "OVERALL (paper)")

# THE key diagnosis: are wins small exits and losses full stops?
block(T, "close reason (R:R KILLER lives here)", lambda t: t.get("close_reason") or "?")
block(T, "strategy (swing vs scalping)", lambda t: t.get("strategy") or "?")
block(T, "side", lambda t: t.get("side") or "?")

# config-era check
print("\n--- by ENTRY DATE (config-era contamination check) ---")
bydate = defaultdict(list)
for t in T:
    bydate[date_of(t)].append(num(t.get("net_pnl")))
for k in sorted(bydate):
    v = bydate[k]
    w = len([x for x in v if x > 0])
    print(f"  {k}  n={len(v):<3} net={sum(v):+7.2f} win%={100*w//len(v):>3}")

# pairs: worst bleeders first (ban candidates), then best (keep)
block(T, "pair — WORST first (ban candidates)", lambda t: t.get("pair") or "?")

# strategy x close_reason cross
print("\n--- strategy x close_reason (how each strategy exits) ---")
cross = defaultdict(list)
for t in T:
    cross[(t.get("strategy") or "?", t.get("close_reason") or "?")].append(num(t.get("net_pnl")))
for (st, cr), v in sorted(cross.items(), key=lambda kv: sum(kv[1])):
    w = len([x for x in v if x > 0])
    print(f"  {st:9} {cr:10} n={len(v):<3} net={sum(v):+7.2f} win%={100*w//len(v):>3}")

print("\n" + "=" * 66)
print("READ THIS:")
print(" - If wins exit via BE/TIMEOUT/TP1(partial) small and losses via SL big,")
print("   the R:R 1:3 is being cut short by an exit mechanism, not realized.")
print(" - If most trades predate the current config, PF 0.41 is stale -> reset.")
print(" - Net-negative pairs/strategies = ban candidates. Net-positive = keep.")
