#!/usr/bin/env python3
"""emastoch_parity.py — does the LIVE strategy fire exactly like the BACKTEST?

The walk-forward validated a set of rules, not a piece of code. If
strategies/emastoch.py does not reproduce those rules bar-for-bar, the PF 1.74
result says nothing about what the bot will actually do. This checks the two
implementations against each other on REAL market data:

  * user_strategy_test.signals(..., use_rsi=False, use_pa=False, W=5)  <- what
    was validated
  * strategies.emastoch.run(df[:i+1])                                  <- what
    the engine will call

It reports agreement, and any bar where they disagree, plus a geometry check
that the returned stop and target really are 1.5xATR and 1:3.

  cd /root/hpuaung/trading_bot && .venv/bin/python emastoch_parity.py
  .venv/bin/python emastoch_parity.py 6h 8       # timeframe, number of pairs
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
import user_strategy_test as U
from strategies import emastoch

db.init_db()

_a = sys.argv[1:]
TF = _a[0] if len(_a) > 0 else "6h"
NPAIRS = int(_a[1]) if len(_a) > 1 else 8
LIMIT = 1200
W = 5

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()][:NPAIRS]

print("=" * 78)
print(f"PARITY CHECK — live strategy vs validated backtest   tf={TF}  pairs={len(PAIRS)}")
print("=" * 78)

tot_match = tot_mismatch = 0
tot_bt = tot_live = 0
geom_checked = 0
geom_bad = 0
examples = []

for sym in PAIRS:
    try:
        raw = bc.get_ohlcv_deep(sym, TF, LIMIT, api_mode="real")
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: fetch error {str(e)[:40]}")
        continue
    if raw is None or len(raw) < 400:
        continue
    d = U.prepare(raw)
    buy, sell = U.signals(d, W, use_rsi=False, use_pa=False)
    n = len(d)
    m = mm = bt_n = live_n = 0
    # Walk the last 600 bars; each step hands the strategy only the data it
    # would have had live (no future bars).
    for i in range(max(300, n - 600), n):
        sub = ind.compute_indicators(raw.iloc[:i + 1].copy())
        r = emastoch.run(sub)
        live = r["signal"]
        bt = "BUY" if buy[i] else ("SELL" if sell[i] else "NONE")
        if bt != "NONE":
            bt_n += 1
        if live != "NONE":
            live_n += 1
            # geometry: stop is 1.5xATR away, target is 3x the stop distance
            e, sl, tp = r["entry"], r["sl"], r["tp1"]
            atr = float(sub["atr"].iloc[-1])
            geom_checked += 1
            if atr > 0:
                if (abs(abs(e - sl) / atr - 1.5) > 0.01
                        or abs(abs(tp - e) / abs(e - sl) - 3.0) > 0.01):
                    geom_bad += 1
        if live == bt:
            m += 1
        else:
            mm += 1
            if len(examples) < 10:
                examples.append((sym, i, bt, live))
    tot_match += m
    tot_mismatch += mm
    tot_bt += bt_n
    tot_live += live_n
    flag = "✅" if mm == 0 else "❌"
    print(f"  {sym:10} bars={m+mm:<5} backtest={bt_n:<4} live={live_n:<4} "
          f"mismatch={mm:<4} {flag}")

print("-" * 78)
tot = tot_match + tot_mismatch
if tot == 0:
    print("  no bars compared — check pairs/timeframe")
    raise SystemExit(1)
print(f"  bars compared     : {tot}")
print(f"  backtest signals  : {tot_bt}")
print(f"  live signals      : {tot_live}")
print(f"  agreement         : {100*tot_match/tot:.3f}%   mismatches: {tot_mismatch}")
if examples:
    print("  first mismatches (pair, bar, backtest, live):")
    for e in examples:
        print(f"    {e}")
print(f"  geometry checked  : {geom_checked} signals, bad: {geom_bad}"
      + ("  ✅ SL=1.5xATR and TP=3R" if geom_bad == 0 else "  ❌ WRONG GEOMETRY"))

print("\n" + "=" * 78)
if tot_mismatch == 0 and tot_live > 0 and geom_bad == 0:
    print("✅ PARITY CONFIRMED — the engine will trade exactly what was validated.")
elif tot_live == 0 and tot_bt == 0:
    print("⚠️  Neither fired on this sample. Try more pairs or a longer window;")
    print("   this strategy is selective (~5 entries/30d across the universe).")
else:
    print("❌ MISMATCH — the live strategy does NOT reproduce the backtest.")
    print("   Do not enable it until this is 0; the PF 1.74 would not apply.")
print("=" * 78)
