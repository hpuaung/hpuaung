#!/usr/bin/env python3
"""check_real.py — diagnose the REAL Binance connection.

The dashboard 'Test/Save Live' can succeed (reads the account) while the engine
still shows 'Real Balance: error' — usually because the FUTURES wallet read needs
'Enable Futures' on the key, or the VPS IP isn't whitelisted, or the engine is
running an old client and just needs a restart. This prints the exact error so we
know which.

    cd /root/hpuaung/trading_bot && .venv/bin/python check_real.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc

db.init_db()

print("=" * 60)
print("REAL BINANCE CONNECTION DIAGNOSTIC")
print("=" * 60)
print("has real credentials :", bc.has_credentials("real"))
print("binance_conn (engine):", db.get_setting("binance_conn", ""))
print("binance_conn_msg     :", db.get_setting("binance_conn_msg", ""))
print("last_equity_live     :", db.get_setting("last_equity_live", ""))
print("scalping_mode        :", db.get_setting("scalping_mode", ""))
print("swing_mode           :", db.get_setting("swing_mode", ""))
print("-" * 60)

# Fresh client (same code path the engine uses) — does the FUTURES equity read?
bc.reset_clients()
try:
    ok, msg = bc.test_connection("real")
    print("test_connection(real):", ok, "|", msg)
except Exception as e:  # noqa: BLE001
    print("test_connection(real) EXCEPTION:", repr(e))

try:
    eq = bc.get_equity("real")
    print("get_equity(real)     :", eq)
except Exception as e:  # noqa: BLE001
    print("get_equity(real) ERROR ->", repr(e))

print("-" * 60)
print("recent error/connection events:")
for e in db.get_recent_events(20):
    k = (e.get("kind") or "").upper()
    if any(x in k for x in ("EQUITY", "ERROR", "CONN", "API", "ORDER")):
        print("  ", e.get("timestamp"), e.get("kind"), "-", (e.get("message") or "")[:66])

print("-" * 60)
print("READ: if get_equity errors with 'permission'/'Invalid Api-Key'/'-2015' ->")
print("  the key needs FUTURES enabled + the VPS IP (150.95.84.241) whitelisted.")
print("  If get_equity WORKS here but the dashboard said error -> just restart the")
print("  engine (systemctl restart futures-engine) to reload the client.")
