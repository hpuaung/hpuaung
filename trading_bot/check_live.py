#!/usr/bin/env python3
"""check_live.py — is the live engine actually running (fresh) right now?"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone
import database as db

print("now UTC:", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
print("=== last 10 EVENTS (newest first) ===")
for e in db.get_recent_events(10):
    print(" ", e["timestamp"], e["kind"], "-", (e["message"] or "")[:45])
print("=== last 10 SIGNALS (newest first) ===")
for s in db.get_recent_signals(10):
    print(" ", s["timestamp"], s["pair"], s["strategies"], "->", s["action"])
