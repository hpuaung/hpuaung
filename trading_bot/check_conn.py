#!/usr/bin/env python3
"""check_conn.py — verify Binance testnet/live credentials + connection."""
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc

for mode, kk, sk in (("test", "binance_testnet_api", "binance_testnet_secret"),
                     ("real", "binance_live_api", "binance_live_secret")):
    key = db.get_setting(kk, "")
    sec = db.get_setting(sk, "")
    print(f"{mode}: key_set={bool(key)} (len={len(key)})  secret_set={bool(sec)} (len={len(sec)})  "
          f"has_creds={bc.has_credentials(mode)}")
    if bc.has_credentials(mode):
        bc.reset_clients()
        print("   conn:", bc.test_connection(mode))

print("scalping_api_mode:", db.get_setting("scalping_api_mode"))
print("swing_api_mode   :", db.get_setting("swing_api_mode"))
print("last_equity_test :", db.get_setting("last_equity_test"))
print("last_equity_live :", db.get_setting("last_equity_live"))
print("starting_balance :", db.get_setting("starting_balance"))
print("binance_conn     :", db.get_setting("binance_conn"))
