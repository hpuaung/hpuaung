#!/usr/bin/env python3
"""live.py — what every open position is winning or losing RIGHT NOW.

The Paper Balance only changes when a trade CLOSES, so while trades are open it
looks frozen. This shows the live picture instead: each position's current price
move, its floating profit/loss in dollars, and the account EQUITY (balance +
floating), which does move minute to minute.

It also prints what a win and a loss are actually WORTH at the current position
sizing — the honest answer to "why does the balance never leave $100".

  cd /root/hpuaung/trading_bot && .venv/bin/python live.py
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone

import database as db
from utils import binance_client as bc

db.init_db()


def num(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def age_days(ts):
    try:
        t = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return 0.0


ps = db.get_open_positions()
balance = db.paper_balance()
start = db.get_float("starting_balance", 100.0)

print("=" * 80)
print("LIVE POSITIONS — what you are winning / losing right now")
print("=" * 80)

total_pnl = 0.0
rows = []
for p in ps:
    sym, side = p["symbol"], p["side"]
    d = 1.0 if side == "BUY" else -1.0
    entry, qty = num(p["entry_price"]), num(p["entry_qty"])
    sl, tp1 = num(p["sl_price"]), num(p["tp1"])
    lev = num(p["leverage"]) or 1
    try:
        live = bc.get_price(sym, "real")
    except Exception:  # noqa: BLE001
        live = 0.0
    if live <= 0 or entry <= 0:
        continue
    move_pct = (live - entry) / entry * 100.0 * d      # price move in our favour
    pnl = (live - entry) * qty * d                      # dollars
    pnl_pct = move_pct * lev                            # leveraged % on margin
    risk = abs(entry - sl)
    r_now = ((live - entry) * d / risk) if risk > 0 else 0.0
    total_pnl += pnl
    rows.append((sym, p["strategy"], side, entry, live, move_pct, pnl, pnl_pct,
                 r_now, age_days(p["timestamp"])))

if rows:
    print(f"{'pair':10}{'eng':9}{'side':5}{'entry':>11}{'now':>11}"
          f"{'move%':>8}{'P&L $':>9}{'P&L %':>8}{'R':>7}{'age':>7}")
    print("-" * 80)
    for r in sorted(rows, key=lambda x: -x[6]):
        mark = "🟢" if r[6] > 0 else ("🔴" if r[6] < 0 else "⚪")
        print(f"{r[0]:10}{r[1]:9}{r[2]:5}{r[3]:>11.5g}{r[4]:>11.5g}"
              f"{r[5]:>+8.2f}{r[6]:>+9.2f}{r[7]:>+8.2f}{r[8]:>+7.2f}{r[9]:>6.1f}d {mark}")
    print("-" * 80)
else:
    print("No open positions.")

equity = balance + total_pnl
print(f"\n  Balance (realised, moves only on CLOSE) : ${balance:,.2f}")
print(f"  Open P&L (floating, moves every second) : ${total_pnl:+,.2f}")
print(f"  EQUITY  (balance + open P&L)            : ${equity:,.2f}"
      f"   [{equity-start:+,.2f} vs start ${start:,.0f}]")

# --- the honest sizing math ------------------------------------------------
print("\n" + "=" * 80)
print("WHAT A TRADE IS WORTH AT THIS SIZE")
print("=" * 80)
risks = []
for p in ps:
    entry, qty, sl = num(p["entry_price"]), num(p["entry_qty"]), num(p["sl_price"])
    if entry > 0 and qty > 0 and sl > 0:
        risks.append(abs(entry - sl) * qty)      # dollars at risk = 1R
if risks:
    avg_r = sum(risks) / len(risks)
    print(f"  1R (one full stop-loss) ≈ ${avg_r:.2f}")
    print(f"  a WIN at R:R 1:3        ≈ ${avg_r*3:+.2f}")
    print(f"  a LOSS                  ≈ ${-avg_r:+.2f}")
    print(f"\n  So the balance can only move in ~${avg_r:.2f}-${avg_r*3:.2f} steps.")
    print(f"  Going ${start:,.0f} -> ${start*2:,.0f} needs roughly "
          f"{int(start/(avg_r*0.5)) if avg_r > 0 else 0} net-winning trades.")
    print("  That is the real reason the balance sits near its start: the edge is")
    print("  thin AND the position size is 1% of a small account. It is not broken —")
    print("  it is small and slow by design.")
else:
    print("  (no open positions to measure sizing from)")

print("\n  Raise the size and every number above scales with it — and so does the")
print("  drawdown. Do NOT raise it until the edge is confirmed on closed trades.")
