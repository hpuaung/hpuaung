#!/usr/bin/env python3
"""why_stuck.py — why has the balance not moved and why do no trades close?

Balance only changes when a trade CLOSES (realised PnL). If every slot is held
by a position whose TP/SL is far away, the account freezes: no closes, and once
max_concurrent is full, no new entries either.

For each open position this prints how far price still has to travel to reach
TP1 or SL (in % and in ATR-candles), how long it has been open, and how long
until the max-hold force-exit. Then it counts how many entries the concurrency
guard has blocked, so the freeze is visible as a number.

  cd /root/hpuaung/trading_bot && .venv/bin/python why_stuck.py
"""
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone

import database as db
from utils import binance_client as bc
from utils import indicators as ind

db.init_db()

TF_HOURS = {"1m": 1/60, "5m": 5/60, "15m": .25, "30m": .5, "1h": 1, "2h": 2,
            "4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24, "3d": 72}


def num(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def age_hours(ts):
    try:
        t = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return 0.0


ps = db.get_open_positions()
maxc = db.get_int("max_concurrent_trades", 5)

print("=" * 78)
print("WHY IS THE BALANCE STUCK?")
print("=" * 78)
print(f"open positions : {len(ps)} / max_concurrent {maxc}"
      + ("   ❌ FULL — no new entries possible" if len(ps) >= maxc else "   (room left)"))
print("Balance only moves when a trade CLOSES. Open positions float unrealised.")

if not ps:
    print("\nNo open positions.")
else:
    print("\n" + "-" * 78)
    print(f"{'pair':10}{'eng':9}{'side':5}{'age':>7}{'now R':>7}"
          f"{'→TP1':>8}{'→SL':>7}{'ATR/c':>7}{'~days to TP':>12}{'maxhold in':>11}")
    print("-" * 78)
    for p in ps:
        sym, strat, side = p["symbol"], p["strategy"], p["side"]
        d = 1 if side == "BUY" else -1
        entry, sl, tp1 = num(p["entry_price"]), num(p["sl_price"]), num(p["tp1"])
        tf = db.get_setting(f"{strat}_timeframe", "6h")
        try:
            live = bc.get_price(sym, "real")
        except Exception:  # noqa: BLE001
            live = 0.0
        # ATR per candle on the entry timeframe = how fast this pair actually moves
        atr_pct = 0.0
        try:
            df = ind.compute_indicators(bc.get_ohlcv(sym, tf, 100, api_mode="real"))
            atr_pct = float(df["atr"].iloc[-1]) / max(float(df["close"].iloc[-1]), 1e-9) * 100.0
        except Exception:  # noqa: BLE001
            pass
        risk = abs(entry - sl)
        now_r = ((live - entry) * d / risk) if (risk > 0 and live > 0) else 0.0
        to_tp = abs(tp1 - live) / live * 100.0 if live > 0 else 0.0
        to_sl = abs(sl - live) / live * 100.0 if live > 0 else 0.0
        # crude ETA: distance / (ATR per candle), in candles -> days
        eta = ""
        if atr_pct > 0 and to_tp > 0:
            candles = to_tp / atr_pct
            eta = f"{candles * TF_HOURS.get(tf, 6) / 24.0:.0f}d"
        ah = age_hours(p["timestamp"])
        maxd = (7 if db.auto_flag(f"{strat}_auto_maxhold", True)
                else db.get_int(f"{strat}_max_hold_days", 7))
        left = maxd - ah / 24.0
        print(f"{sym:10}{strat:9}{side:5}{ah/24.0:>6.1f}d{now_r:>+7.2f}"
              f"{to_tp:>7.1f}%{to_sl:>6.1f}%{atr_pct:>6.2f}%{eta:>12}{left:>10.1f}d")
    print("-" * 78)
    print("now R  = current unrealised R (needs +3.0 to hit TP1, -1.0 to hit SL)")
    print("~days to TP = distance / typical candle range — how long a TP realistically takes")

# How many entries did the concurrency guard actually refuse?
print("\n" + "=" * 78)
print("ENTRIES BLOCKED (last 500 events)")
print("=" * 78)
try:
    conn = db.get_conn()
    rows = conn.execute("SELECT kind, message FROM events ORDER BY id DESC LIMIT 500").fetchall()
    conc = sum(1 for r in rows if "Max concurrent" in str(r["message"]))
    corr = sum(1 for r in rows if "open SELL trades >=" in str(r["message"])
               or "open BUY trades >=" in str(r["message"]))
    opens = sum(1 for r in rows if str(r["kind"]) in ("PAPER_OPEN", "REAL_OPEN"))
    closes = sum(1 for r in rows if "CLOSE" in str(r["kind"]).upper())
    print(f"  blocked by max_concurrent : {conc}")
    print(f"  blocked by correlation cap: {corr}")
    print(f"  entries opened            : {opens}")
    print(f"  closes logged             : {closes}")
    if conc > 20:
        print("\n  ❌ The concurrency cap is refusing entries constantly — the bot is")
        print("     frozen behind slow positions, not scanning for nothing.")
except Exception as e:  # noqa: BLE001
    print(f"  (events read failed: {e})")

print("\n" + "=" * 78)
print("THE MECHANISM")
print("=" * 78)
print("trend.py puts the SL at EMA50. On 6h/12h in a running trend that can sit")
print("5-15% away, and the forced R:R 1:3 then places TP1 at 3x THAT — 15-45% away.")
print("Such a move takes weeks, so positions neither win nor lose; they just sit.")
print("With max_concurrent slots all held, no new entry can start either, so the")
print("balance stays flat at its starting value.")
print("\nThe backtest that produced PF 1.36-1.54 had NO concurrency cap: all ~38")
print("pairs traded independently (~20-38 entries/30d). Capping at 6 does not")
print("reproduce it — it samples a handful of trades and then stalls.")
