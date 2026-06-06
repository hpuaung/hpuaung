#!/usr/bin/env python3
"""check_engine.py — what has the live engine actually been logging/doing?"""
import warnings
warnings.filterwarnings("ignore")

import database as db
c = db.get_conn()

print("=== signal actions the ENGINE logged ===")
for row in c.execute("SELECT action, COUNT(*) n FROM signals GROUP BY action").fetchall():
    print(" ", row["action"], row["n"])

print("=== recent NON-NONE engine signals ===")
rows = c.execute("SELECT timestamp,pair,strategies,action FROM signals "
                 "WHERE action != 'NONE' ORDER BY id DESC LIMIT 15").fetchall()
if rows:
    for r in rows:
        print(" ", r["timestamp"][11:], r["pair"], r["strategies"], "->", r["action"])
else:
    print("  (engine has NOT logged any non-NONE signal)")

print("open positions:", db.count_open_positions())
print("total trades  :", len(db.get_trades()))

print("=== recent ORDER/OPEN events ===")
shown = False
for ev in db.get_recent_events(60):
    if any(k in ev["kind"] for k in ("ORDER", "OPEN", "WINRATE", "GUARD")):
        shown = True
        print(" ", ev["timestamp"][11:], ev["kind"], "-", (ev["message"] or "")[:55])
if not shown:
    print("  (none)")
