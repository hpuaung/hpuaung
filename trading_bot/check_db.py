#!/usr/bin/env python3
"""check_db.py — diagnose why settings won't persist (DB write/integrity/disk)."""
import warnings
warnings.filterwarnings("ignore")

import os
import time
import database as db

db.init_db()
conn = db.get_conn()

print("DB path     :", db.DB_PATH)
print("DB exists   :", os.path.exists(db.DB_PATH))
print("DB size     :", os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0, "bytes")
print("DB writable :", os.access(db.DB_PATH, os.W_OK))
print("Dir writable:", os.access(os.path.dirname(db.DB_PATH), os.W_OK))

try:
    print("journal_mode:", conn.execute("PRAGMA journal_mode").fetchone()[0])
    print("integrity   :", conn.execute("PRAGMA integrity_check").fetchone()[0])
except Exception as e:  # noqa: BLE001
    print("PRAGMA error:", e)

# write/read round-trip
tv = str(time.time())
try:
    db.save_setting("__diag__", tv)
    got = db.get_setting("__diag__")
    print("WRITE/READ  :", "OK ✅" if got == tv else f"FAIL ❌ (got {got})")
except Exception as e:  # noqa: BLE001
    print("WRITE error :", e)

print("=== recent ERROR events ===")
shown = False
for e in db.get_recent_events(25):
    if "ERROR" in e["kind"]:
        shown = True
        print(" ", e["timestamp"][11:], e["kind"], "-", (e["message"] or "")[:60])
if not shown:
    print("  (none)")
