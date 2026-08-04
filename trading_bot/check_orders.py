#!/usr/bin/env python3
"""check_orders.py — after a restart, is the engine opening SANE entries or a
correlated pile-on? Shows every open position with its live price + drift, groups
by engine/side to expose correlation, and prints the concurrency/mode config so
we can tell "valid live signals" apart from "over-concurrency / market-wide dip
fired 20 reversion longs at once".

  cd /root/hpuaung/trading_bot && .venv/bin/python check_orders.py
"""
import warnings
warnings.filterwarnings("ignore")
from collections import Counter

import database as db
from utils import binance_client as bc

db.init_db()

ps = db.get_open_positions()
print("=" * 72)
print(f"OPEN POSITIONS: {len(ps)}")
print("=" * 72)

if ps:
    by_eng = Counter(p["strategy"] for p in ps)
    by_side = Counter(p["side"] for p in ps)
    by_eng_side = Counter(f"{p['strategy']}/{p['side']}" for p in ps)
    print("by engine :", dict(by_eng))
    print("by side   :", dict(by_side))
    print("by eng+side:", dict(by_eng_side))
    # Correlation flag: many same-side same-engine at once = market-wide pile-on.
    worst = by_eng_side.most_common(1)[0] if by_eng_side else ("", 0)
    if worst[1] >= 5:
        print(f"⚠️  {worst[1]} positions are {worst[0]} — CORRELATED pile-on "
              "(one market move hits them all together).")
    print("-" * 72)
    print(f"{'engine':9}{'pair':11}{'side':5}{'entry':>12}{'live':>12}"
          f"{'drift%':>8}{'lev':>4}  {'opened(UTC)':<19}{'mode'}")
    total_drift = 0.0
    for p in ps:
        try:
            live = bc.get_price(p["symbol"], "real")   # mainnet public price
        except Exception:  # noqa: BLE001
            live = 0.0
        entry = float(p["entry_price"] or 0)
        drift = ((live - entry) / entry * 100.0) if (entry and live) else 0.0
        total_drift += drift if p["side"] == "BUY" else -drift
        print(f"{p['strategy']:9}{p['symbol']:11}{p['side']:5}{entry:>12.5g}"
              f"{live:>12.5g}{drift:>+8.2f}{int(p['leverage'] or 0):>4}  "
              f"{str(p['timestamp'] or '')[:19]:<19}{'paper' if p['paper_mode'] else 'REAL'}")
    print("-" * 72)
    # A big same-direction drift right after entry = entries chased a move / all
    # underwater together (the correlated-loss signature).
    print(f"net directional drift since entry (sum, + = in profit): {total_drift:+.2f}%")
else:
    print("none open.")

print("\n" + "=" * 72)
print("CONFIG (why entries can burst)")
print("=" * 72)
g = db.get_setting
gi = db.get_int
gb = db.get_bool
print(f"max_concurrent_trades = {gi('max_concurrent_trades', 5)}   "
      f"(auto_pilot={gb('auto_pilot', False)} global_auto_risk={gb('global_auto_risk', False)})")
print(f"open now = {len(ps)}  →  room for {max(0, gi('max_concurrent_trades',5)-len(ps))} more")
for s in ("swing", "scalping"):
    print(f"{s:9}: bot_on={gb(f'{s}_bot_on', False)!s:5} mode={g(f'{s}_mode','paper'):5} "
          f"tf={g(f'{s}_timeframe','?'):4} trend={gb(f'{s}_trend_on',False)!s:5} "
          f"breakout={gb(f'{s}_breakout_on',False)!s:5} reversion={gb(f'{s}_reversion_on',False)!s:5} "
          f"corr_filter={gb(f'{s}_corr_filter', False)}")
print(f"sl_cooldown_on = {gb('sl_cooldown_on', True)}   "
      f"emergency_stop = {gb('emergency_stop', False)}")
print(f"starting_balance = {db.get_float('starting_balance',0):.2f}   "
      f"paper_balance = {db.paper_balance():.2f}")

# Recent engine events — entries + any guard blocks, to see what the last scan did.
print("\n" + "=" * 72)
print("RECENT EVENTS (last 25)")
print("=" * 72)
try:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT timestamp, kind, message FROM events "
        "ORDER BY id DESC LIMIT 25").fetchall()
    for r in rows:
        print(f"{str(r['timestamp'])[:19]}  {str(r['kind']):14} {str(r['message'])[:60]}")
except Exception as e:  # noqa: BLE001
    print(f"(events table read failed: {e})")
