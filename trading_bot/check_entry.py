#!/usr/bin/env python3
"""check_entry.py — why isn't a given pair entering? Replays the engine's checks.

Usage:  .venv/bin/python check_entry.py [SYMBOL] [scalping|swing]
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from execution import risk_guard
from utils import binance_client as bc
import engine

sym = sys.argv[1] if len(sys.argv) > 1 else "DOGEUSDT"
strat = sys.argv[2] if len(sys.argv) > 2 else "swing"

print(f"=== {sym} {strat} ===")
print("scalping_bot_on:", db.get_bool("scalping_bot_on"),
      "| swing_bot_on:", db.get_bool("swing_bot_on"),
      "| emergency_stop:", db.get_bool("emergency_stop"))
print("paper_mode:", db.get_bool("paper_trading_mode"),
      "| news_on:", db.get_bool(f"{strat}_news_on"))

existing = [p for p in db.get_open_positions(strategy=strat) if p["symbol"] == sym]
print("already open on this pair:", len(existing))

api_mode = "real" if db.get_setting(f"{strat}_api_mode") == "real" else "test"
equity = bc.get_equity(api_mode) if bc.has_credentials(api_mode) else db.get_float("last_equity", 0)
starting = db.get_float("starting_balance", equity or 1)
health = risk_guard.health_ratio(equity, starting)
mult = risk_guard.health_multiplier(health)
print(f"equity={equity:.2f} starting={starting:.2f} health={health:.1f}% mult={mult}")

e, cf, tr = engine._gather_indicator_frames(sym, strat, api_mode)
sig, trig, score = engine.aggregate_signal(sym, strat, e, cf, tr, 0, 0, 0)
print("SIGNAL:", sig["signal"], "| triggered:", trig, "| score:", round(score, 2))

if sig["signal"] != "NONE":
    print("--- GUARDS (False = blocking) ---")
    print(" health     :", risk_guard.apply_health_guard(equity, starting, strat)[:2])
    print(" daily_loss :", risk_guard.apply_daily_loss_guard(equity))
    print(" blackout   :", risk_guard.apply_blackout_guard())
    print(" correlation:", risk_guard.apply_correlation_guard(sig["signal"], strat))
    print(" session    :", risk_guard.apply_session_filter(strat))
    print(" concurrency:", risk_guard.apply_concurrency_guard())
    wr, n = db.winrate(strat, sym)
    print(f" winrate    : {wr}% over {n} (skips if n>=15 and <40%)")

print("--- recent blocks/errors ---")
shown = False
for ev in db.get_recent_events(40):
    if ev["kind"] in ("GUARD_BLOCK", "ORDER_BLOCKED", "WINRATE_SKIP",
                      "PAIR_ERROR", "DATA_ERROR", "ORDER_ERROR", "EQUITY_ERROR"):
        shown = True
        print(" ", ev["timestamp"][11:], ev["kind"], "-", (ev["message"] or "")[:60])
if not shown:
    print("  (none)")
