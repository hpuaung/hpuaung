#!/usr/bin/env python3
"""trend_backtest.py — test a DIFFERENT, evidence-based entry: higher-timeframe
TREND-FOLLOWING (momentum), the one retail approach with real academic support
for a small edge. The current bot scalps 5m noise (~random); this rides 4h/1d
trends: low win rate but winners >> losers (let winners run, cut losers).

Entry: EMA21 > EMA50 > EMA200 (up-stack) -> long; down-stack -> short
       (only on the candle the stack first forms).
Exit:  the stack flips (close back through EMA50) OR a chandelier trailing stop
       (since-entry extreme -/+ atr_trail*ATR). No fixed TP — winners ride.
Risk:  initial SL = entry -/+ 2*ATR; results are in R (PnL / initial risk).

Usage: .venv/bin/python trend_backtest.py [pairs] [interval] [candles] [atr_trail]
  .venv/bin/python trend_backtest.py all 4h 1500
  .venv/bin/python trend_backtest.py all 1d 1000 3.0
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind

db.init_db()
_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
INTERVAL = _a[1] if len(_a) > 1 else "4h"
LIMIT = int(_a[2]) if len(_a) > 2 else 1500
TRAIL = float(_a[3]) if len(_a) > 3 else 3.0

if pairs_arg == "all":
    PAIRS = [p.strip() for p in db.get_setting(
        "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004
WARMUP = 210
SL_ATR = 2.0


def simulate(df):
    c = df["close"].astype(float).tolist()
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    e21 = df["ema21"].astype(float).tolist()
    e50 = df["ema50"].astype(float).tolist()
    e200 = df["ema200"].astype(float).tolist()
    atr = df["atr"].fillna(0).astype(float).tolist()
    n = len(c)
    trades = []
    pos = None

    def up(i):
        return e21[i] > e50[i] > e200[i]

    def dn(i):
        return e21[i] < e50[i] < e200[i]

    for i in range(WARMUP, n):
        if pos:
            d = pos["d"]
            if d > 0:
                pos["ext"] = max(pos["ext"], hi[i])
                trail = pos["ext"] - TRAIL * atr[i]
                exit_px = None
                if lo[i] <= trail:
                    exit_px = trail
                elif c[i] < e50[i]:
                    exit_px = c[i]
            else:
                pos["ext"] = min(pos["ext"], lo[i])
                trail = pos["ext"] + TRAIL * atr[i]
                exit_px = None
                if hi[i] >= trail:
                    exit_px = trail
                elif c[i] > e50[i]:
                    exit_px = c[i]
            if exit_px is not None:
                gross = (exit_px - pos["entry"]) * d
                fee = (pos["entry"] + exit_px) * FEE
                trades.append((gross - fee) / pos["risk"])
                pos = None
        if not pos and atr[i] > 0:
            if up(i) and not up(i - 1):
                pos = {"d": 1, "entry": c[i], "ext": hi[i], "risk": SL_ATR * atr[i]}
            elif dn(i) and not dn(i - 1):
                pos = {"d": -1, "entry": c[i], "ext": lo[i], "risk": SL_ATR * atr[i]}
    return trades


print("=" * 70)
print(f"TREND-FOLLOWING BACKTEST  interval={INTERVAL} candles={LIMIT} trail={TRAIL}xATR")
print("low win% is normal for trend-following; what matters is expectancy>0")
print("=" * 70)

allt = []
for sym in PAIRS:
    try:
        df = bc.get_ohlcv(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < WARMUP + 30:
            print(f"  {sym:9} not enough data")
            continue
        df = ind.compute_indicators(df)
        t = simulate(df)
        allt += t
        if t:
            w = [x for x in t if x > 0]
            aw = sum(w) / len(w) if w else 0
            al = sum(x for x in t if x <= 0) / max(len(t) - len(w), 1)
            print(f"  {sym:9} n={len(t):<3} win%={100*len(w)//len(t):3} "
                  f"avgWin={aw:+.2f}R avgLoss={al:+.2f}R totalR={sum(t):+.1f}")
        else:
            print(f"  {sym:9} no trades")
    except Exception as ex:  # noqa: BLE001
        print(f"  {sym:9} ERROR {ex}")

print("-" * 70)
if allt:
    w = [x for x in allt if x > 0]
    grossW = sum(w); grossL = -sum(x for x in allt if x <= 0)
    pf = grossW / grossL if grossL > 0 else float("inf")
    print(f"TOTAL  n={len(allt)}  win%={100*len(w)//len(allt)}  "
          f"expectancy={sum(allt)/len(allt):+.3f}R  totalR={sum(allt):+.1f}  PF={pf:.2f}")
    print("\n🟢 expectancy>0 and PF>1.3 -> trend-following has edge here.")
    print("🔴 expectancy<=0 -> even trend-following fails on these pairs/period.")
