#!/usr/bin/env python3
"""swing_sweep.py — the exhaustive swing test we owe: ALL 4 strategies x MANY
swing timeframes x R:R {native, 1:2, 1:3}, in ONE run, reporting not just the
edge (expectancy / PF) but the ENTRY FREQUENCY (entries per 30 days across the
whole universe) — because the real pain is "swing gives no entries for weeks".

For each (timeframe, strategy, R:R) it aggregates trades over every selected
pair and prints n / win% / expR / PF / entries-per-30d. Then it ranks the
combos that BOTH have an edge (expR>0, PF>1.3, n>=30) AND trade often, so we can
pick a swing setup that actually fires without giving up the edge.

Reversion's scalping tuning (rsi_extreme / fixed_rr) is neutralised IN MEMORY
(no DB write) so we measure the BASE strategies uniformly; the rr= column
controls R:R cleanly instead.

Usage:
  cd /root/hpuaung/trading_bot && .venv/bin/python swing_sweep.py
  .venv/bin/python swing_sweep.py 4h,6h,8h,12h,1d,3d 1500
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()

# --- neutralise reversion scalping tuning in-process (no DB write) so the sweep
#     measures the BASE mean-reversion strategy, not the RSI<20 scalping gate ---
_orig_get_float = db.get_float
def _patched_get_float(key, default=0.0):
    if key in ("reversion_rsi_extreme", "reversion_fixed_rr"):
        return 0.0
    return _orig_get_float(key, default)
db.get_float = _patched_get_float

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in (_a[0] if len(_a) > 0 else
              "4h,6h,8h,12h,1d,3d").split(",") if x.strip()]
LIMIT = int(_a[1]) if len(_a) > 1 else 1500

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
STRATS = ["trend", "reversion", "breakout", "hybrid"]
RRS = [None, 2.0, 3.0]            # native, then forced 1:2 and 1:3
TF_HOURS = {"1m": 1/60, "5m": 5/60, "15m": .25, "30m": .5, "1h": 1, "2h": 2,
            "4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24, "2d": 48,
            "3d": 72, "1w": 168}


def _valid(res):
    if not res or res.get("signal", "NONE") not in ("BUY", "SELL"):
        return None
    e, sl, tp = float(res.get("entry", 0)), float(res.get("sl", 0)), float(res.get("tp1", 0))
    if e <= 0 or sl <= 0 or tp <= 0 or abs(e - sl) <= 0:
        return None
    if res["signal"] == "BUY" and not (sl < e < tp):
        return None
    if res["signal"] == "SELL" and not (tp < e < sl):
        return None
    return res


def run_pair(df):
    """One pass over the candles; every (strategy, rr) variant is an independent
    backtest sharing the same entry+SL, differing only in the TP."""
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    n = len(c)
    pos = {(s, rr): None for s in STRATS for rr in RRS}
    trades = {(s, rr): [] for s in STRATS for rr in RRS}
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
            r = _valid(sigs[s])
            for rr in RRS:
                key = (s, rr)
                p = pos[key]
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
                        trades[key].append((gross - fee) / p["risk"])
                        pos[key] = None
                        p = None
                if not p and r:
                    e = float(r["entry"]); sl = float(r["sl"]); tp = float(r["tp1"])
                    d = 1 if r["signal"] == "BUY" else -1
                    if rr is not None:
                        tp = e + d * rr * abs(e - sl)
                    pos[key] = {"i": i, "d": d, "entry": e, "sl": sl, "tp": tp,
                                "risk": abs(e - sl)}
    return trades


def stat(t, span_days):
    if not t:
        return None
    w = [x for x in t if x > 0]
    gW = sum(w); gL = -sum(x for x in t if x <= 0)
    pf = gW / gL if gL > 0 else 99.0
    exp = sum(t) / len(t)
    per30 = (len(t) / span_days * 30) if span_days > 0 else 0
    return dict(n=len(t), win=100 * len(w) // len(t), exp=exp, pf=pf, per30=per30)


winners = []   # collect edge+active combos for the final ranking

print("=" * 78)
print("SWING SWEEP — 4 strategies x timeframes x R:R{native,1:2,1:3}")
print(f"pairs={len(PAIRS)}  candles/pair<= {LIMIT}  (reversion tuning neutralised)")
print("=" * 78)

for tf in TIMEFRAMES:
    agg = {(s, rr): [] for s in STRATS for rr in RRS}
    max_candles = 0
    for sym in PAIRS:
        try:
            df = bc.get_ohlcv_deep(sym, tf, LIMIT, api_mode="real")
            if df is None or len(df) < WARMUP + 30:
                continue
            max_candles = max(max_candles, len(df))
            df = ind.compute_indicators(df)
            t = run_pair(df)
            for k in agg:
                agg[k] += t[k]
        except Exception:  # noqa: BLE001
            continue
    span_days = max_candles * TF_HOURS.get(tf, 1) / 24.0
    print(f"\n==== {tf}  (~{max_candles} candles/pair ≈ {span_days:.0f} days history) ====")
    print(f"{'strategy':10}{'R:R':>6}{'n':>6}{'win%':>6}{'expR':>8}{'PF':>7}"
          f"{'entries/30d':>13}")
    for s in STRATS:
        for rr in RRS:
            st = stat(agg[(s, rr)], span_days)
            rrlab = "nat" if rr is None else f"1:{rr:g}"
            if not st:
                print(f"{s:10}{rrlab:>6}{'0':>6}   no trades")
                continue
            edge = st["exp"] > 0 and st["pf"] > 1.3 and st["n"] >= 30
            flag = " EDGE" if edge else ""
            print(f"{s:10}{rrlab:>6}{st['n']:>6}{st['win']:>6}{st['exp']:>+8.3f}"
                  f"{st['pf']:>7.2f}{st['per30']:>13.2f}{flag}")
            if edge:
                winners.append((tf, s, rrlab, st))

print("\n" + "=" * 78)
print("EDGES WITH ACTIVITY  (expR>0, PF>1.3, n>=30) — ranked by entries/30d")
print("=" * 78)
if not winners:
    print("  none — no swing combo clears the edge bar with a real sample.")
else:
    print(f"{'timeframe':10}{'strategy':11}{'R:R':>6}{'PF':>7}{'expR':>8}"
          f"{'n':>6}{'entries/30d':>13}")
    for tf, s, rrlab, st in sorted(winners, key=lambda x: -x[3]["per30"]):
        print(f"{tf:10}{s:11}{rrlab:>6}{st['pf']:>7.2f}{st['exp']:>+8.3f}"
              f"{st['n']:>6}{st['per30']:>13.2f}")
print("\nMore entries/30d = more activity. Pick the highest-frequency EDGE you")
print("trust; breakout-1d is the proven one — see if a faster combo also holds.")
