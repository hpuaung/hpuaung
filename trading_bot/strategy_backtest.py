#!/usr/bin/env python3
"""strategy_backtest.py — backtest the bot's FOUR strategies SEPARATELY so we can
see which (if any) has an edge on its own: Trend Following, Mean Reversion,
Breakout, and AI Hybrid. Runs each strategy's real run() over historical candles
and exits at that strategy's OWN sl / tp1 (as it is designed), then reports
win% / avg win-R / avg loss-R / expectancy / profit factor per strategy.

Usage:
  .venv/bin/python strategy_backtest.py [pairs] [interval] [candles]
  .venv/bin/python strategy_backtest.py all 1h 1500
  .venv/bin/python strategy_backtest.py all 4h 1500
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()
_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
INTERVAL = _a[1] if len(_a) > 1 else "1h"
LIMIT = int(_a[2]) if len(_a) > 2 else 1500

if pairs_arg == "all":
    PAIRS = [p.strip() for p in db.get_setting(
        "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
STRATS = ["trend", "reversion", "breakout", "hybrid"]


def _valid(res):
    if not res or res.get("signal", "NONE") not in ("BUY", "SELL"):
        return None
    e, sl, tp = float(res.get("entry", 0)), float(res.get("sl", 0)), float(res.get("tp1", 0))
    if e <= 0 or sl <= 0 or tp <= 0 or abs(e - sl) <= 0:
        return None
    # sanity: sl/tp on correct sides
    if res["signal"] == "BUY" and not (sl < e < tp):
        return None
    if res["signal"] == "SELL" and not (tp < e < sl):
        return None
    return res


def run_pair(df):
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    n = len(c)
    pos = {s: None for s in STRATS}
    trades = {s: [] for s in STRATS}
    for i in range(WARMUP, n):
        sub = df.iloc[:i + 1]
        if not ind.has_enough(sub):
            continue
        tr = trend.run(sub, mtf=False)
        rv = reversion.run(sub, mtf=False)
        bk = breakout.run(sub, mtf=False)
        hy = ai_hybrid.run(sub, tr, rv, bk, ai_threshold=0.60, use_model=False)
        sigs = {"trend": tr, "reversion": rv, "breakout": bk, "hybrid": hy}
        for s in STRATS:
            p = pos[s]
            if p:
                d = p["d"]; ex = None
                if d > 0:
                    if lo[i] <= p["sl"]:
                        ex = p["sl"] * (1 - SLIP)
                    elif hi[i] >= p["tp"]:
                        ex = p["tp"]
                else:
                    if hi[i] >= p["sl"]:
                        ex = p["sl"] * (1 + SLIP)
                    elif lo[i] <= p["tp"]:
                        ex = p["tp"]
                if ex is None and i - p["i"] >= MAXHOLD:
                    ex = c[i]
                if ex is not None:
                    gross = (ex - p["entry"]) * d
                    fee = (p["entry"] + ex) * FEE
                    trades[s].append((gross - fee) / p["risk"])
                    pos[s] = None
            if not pos[s]:
                r = _valid(sigs[s])
                if r:
                    e = float(r["entry"]); sl = float(r["sl"]); tp = float(r["tp1"])
                    d = 1 if r["signal"] == "BUY" else -1
                    pos[s] = {"i": i, "d": d, "entry": e, "sl": sl, "tp": tp,
                              "risk": abs(e - sl)}
    return trades


print("=" * 68)
print(f"STRATEGY BACKTEST (each strategy alone)  interval={INTERVAL} candles={LIMIT}")
print("exits use each strategy's own SL / TP1.  model OFF in hybrid.")
print("=" * 68)

agg = {s: [] for s in STRATS}
for sym in PAIRS:
    try:
        df = bc.get_ohlcv(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < WARMUP + 30:
            print(f"  {sym}: not enough data")
            continue
        df = ind.compute_indicators(df)
        t = run_pair(df)
        for s in STRATS:
            agg[s] += t[s]
        print(f"  {sym} done "
              + " ".join(f"{s[:4]}={len(t[s])}" for s in STRATS))
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: ERROR {e}")

print("-" * 68)
print(f"{'strategy':11}{'n':>5}{'win%':>6}{'avgWinR':>9}{'avgLossR':>10}{'expR':>8}{'PF':>7}")
for s in STRATS:
    t = agg[s]
    if not t:
        print(f"{s:11}{'0':>5}   no trades")
        continue
    w = [x for x in t if x > 0]
    aw = sum(w) / len(w) if w else 0
    al = sum(x for x in t if x <= 0) / max(len(t) - len(w), 1)
    gW = sum(w); gL = -sum(x for x in t if x <= 0)
    pf = gW / gL if gL > 0 else float("inf")
    print(f"{s:11}{len(t):>5}{100*len(w)//len(t):>6}{aw:>+9.2f}{al:>+10.2f}"
          f"{sum(t)/len(t):>+8.3f}{pf:>7.2f}")
print("-" * 68)
print("🟢 expectancy(expR) > 0 AND PF > 1.3  -> that strategy has edge here.")
print("🔴 all expR <= 0 -> none of the four beat costs on these pairs/period.")
