#!/usr/bin/env python3
"""getset.py — read or write a setting from the CLI to test UI<->DB persistence.

  read :  .venv/bin/python getset.py daily_loss_limit_pct
  write:  .venv/bin/python getset.py daily_loss_limit_pct 17
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

if len(sys.argv) >= 3:
    db.save_setting(sys.argv[1], sys.argv[2])
    print(f"SET {sys.argv[1]} = {db.get_setting(sys.argv[1])}")
elif len(sys.argv) == 2:
    print(f"{sys.argv[1]} = {db.get_setting(sys.argv[1])}")
else:
    print("usage: getset.py <key> [value]")
