#!/usr/bin/env python3
"""reset_paper.py — force a TRUE clean slate for paper measurement.

clear_paper_trades() only deletes rows with paper_mode=1, but some closed trades
were recorded with paper_mode=0 (a recording-default bug), so the old scalping /
mixed history survived and kept polluting check_perf. This deployment is 100%
paper (no real keys, no real fills), so it is safe to wipe ALL trades + open
positions and start the breakout-1d measurement from zero.

Usage:  .venv/bin/python reset_paper.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db

db.init_db()
conn = db.get_conn()
before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
by_mode = conn.execute(
    "SELECT paper_mode, COUNT(*) FROM trades GROUP BY paper_mode").fetchall()
conn.execute("DELETE FROM trades")
conn.execute("DELETE FROM active_positions")
conn.commit()
after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

print(f"trades before = {before}  (by paper_mode: {by_mode})")
print(f"trades after  = {after}")
print("CLEAN SLATE ✅ — from here, check_perf shows only new breakout-1d trades"
      if after == 0 else "⚠️ STILL NOT CLEAN — two DB files? investigate.")
