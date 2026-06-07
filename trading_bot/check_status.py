#!/usr/bin/env python3
"""check_status.py — open positions + recent closed trades + today PnL."""
import warnings
warnings.filterwarnings("ignore")

import database as db
from execution import risk_guard

print("=== OPEN POSITIONS ===")
pos = db.get_open_positions()
print("count:", len(pos))
for p in pos:
    print(f"  {p['symbol']} {p['strategy']} {p['side']} entry={p['entry_price']} "
          f"sl={p['sl_price']} paper={p['paper_mode']} ts={p['timestamp']}")

print("=== LAST 10 CLOSED TRADES ===")
for t in db.get_trades(limit=10):
    print(f"  {t.get('exit_timestamp','')} {t['pair']} {t['side']} "
          f"entry={t['entry_price']} exit={t['exit_price']} "
          f"net={round(t.get('net_pnl') or 0,2)} reason={t['close_reason']}")

print("total trades:", len(db.get_trades()))
print("today PnL  :", round(risk_guard.today_pnl(), 2))
