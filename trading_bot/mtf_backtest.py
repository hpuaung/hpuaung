#!/usr/bin/env python3
"""mtf_backtest.py — backtest the bot's MULTI-TIMEFRAME structure exactly as the
live engine runs it: a signal on the ENTRY timeframe, gated by a CONFIRM frame and
an HTF-TREND frame (strategy.run(..., mtf=True)). This is what single-timeframe
strategy_backtest.py cannot measure — whether the confirm+trend filter rescues a
lower-timeframe entry that loses on its own.

Alignment is leak-free: for each entry candle we only expose the higher-TF candles
that have already CLOSED by the time the entry candle closes (searchsorted on close
times). Exits use each strategy's own SL / TP1, same as strategy_backtest.py.

Usage:
  .venv/bin/python mtf_backtest.py <pairs> <entry> <confirm> <trend> <candles> [detail] [only=breakout]
  # swing  : .venv/bin/python mtf_backtest.py all 1h 4h 1d 4000
  # scalp  : .venv/bin/python mtf_backtest.py all 3m 15m 1h 8000
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import numpy as np

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()
_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
ENTRY_TF = _a[1] if len(_a) > 1 else "1h"
CONFIRM_TF = _a[2] if len(_a) > 2 else "4h"
TREND_TF = _a[3] if len(_a) > 3 else "1d"
LIMIT = int(_a[4]) if len(_a) > 4 else 4000
DETAIL = "detail" in _a
ONLY = None
for _x in _a:
    if _x.startswith("only="):
        ONLY = _x.split("=", 1)[1]

if pairs_arg == "all":
    PAIRS = [p.strip() for p in db.get_setting(
        "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
MIN_HTF = 60          # min closed higher-TF rows before we trust ema21/ema50/rsi
STRATS = ["trend", "reversion", "breakout", "hybrid"]


def _ms(df):
    return (df["open_time"].astype("int64") // 10**6).to_numpy()


def _len_ms(open_ms):
    if len(open_ms) < 3:
        return 0
    return int(np.median(np.diff(open_ms)))


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


def run_pair(dfe, dfc, dft):
    hi = dfe["high"].astype(float).tolist()
    lo = dfe["low"].astype(float).tolist()
    c = dfe["close"].astype(float).tolist()
    n = len(c)

    e_open = _ms(dfe); e_close = e_open + _len_ms(e_open)
    c_close = _ms(dfc) + _len_ms(_ms(dfc))
    t_close = _ms(dft) + _len_ms(_ms(dft))

    active = [ONLY] if ONLY else STRATS
    need_tr = ("trend" in active) or ("hybrid" in active)
    need_rv = ("reversion" in active) or ("hybrid" in active)
    need_bk = ("breakout" in active) or ("hybrid" in active)
    pos = {s: None for s in active}
    trades = {s: [] for s in active}

    for i in range(WARMUP, n):
        # ---- exits first (independent of higher TFs) ----
        for s in active:
            p = pos[s]
            if not p:
                continue
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

        # ---- entries: only higher-TF candles closed by this entry-candle close ----
        if all(pos[s] for s in active):
            continue
        jc = int(np.searchsorted(c_close, e_close[i], side="right")) - 1
        jt = int(np.searchsorted(t_close, e_close[i], side="right")) - 1
        if jc < MIN_HTF or jt < MIN_HTF:
            continue
        sub = dfe.iloc[:i + 1]
        if not ind.has_enough(sub):
            continue
        subc = dfc.iloc[:jc + 1]
        subt = dft.iloc[:jt + 1]

        tr = trend.run(sub, subc, subt, mtf=True) if need_tr else None
        rv = reversion.run(sub, subc, subt, mtf=True) if need_rv else None
        bk = breakout.run(sub, subc, subt, mtf=True) if need_bk else None
        hy = (ai_hybrid.run(sub, tr, rv, bk, ai_threshold=0.60, use_model=False)
              if "hybrid" in active else None)
        sigs = {"trend": tr, "reversion": rv, "breakout": bk, "hybrid": hy}

        for s in active:
            if pos[s]:
                continue
            r = _valid(sigs[s])
            if r:
                e = float(r["entry"]); sl = float(r["sl"]); tp = float(r["tp1"])
                d = 1 if r["signal"] == "BUY" else -1
                pos[s] = {"i": i, "d": d, "entry": e, "sl": sl, "tp": tp,
                          "risk": abs(e - sl)}
    return trades


def _line(label, t):
    if not t:
        print(f"{label:12}{'0':>6}   no trades")
        return
    w = [x for x in t if x > 0]
    aw = sum(w) / len(w) if w else 0
    al = sum(x for x in t if x <= 0) / max(len(t) - len(w), 1)
    gW = sum(w); gL = -sum(x for x in t if x <= 0)
    pf = gW / gL if gL > 0 else float("inf")
    edge = " 🟢" if (sum(t) / len(t) > 0 and pf > 1.3) else ""
    print(f"{label:12}{len(t):>6}{100*len(w)//len(t):>6}{aw:>+9.2f}{al:>+10.2f}"
          f"{sum(t)/len(t):>+8.3f}{pf:>7.2f}{edge}")


strats = [ONLY] if ONLY else STRATS
agg = {s: [] for s in strats}
per_pair = {s: {} for s in strats}
got = 0
print("=" * 72)
print(f"MTF BACKTEST  entry={ENTRY_TF} confirm={CONFIRM_TF} trend={TREND_TF} "
      f"candles={LIMIT}")
print("strategy.run(mtf=True); leak-free HTF alignment; own SL/TP1 exits")
print("=" * 72)
for sym in PAIRS:
    try:
        dfe = bc.get_ohlcv_deep(sym, ENTRY_TF, LIMIT, api_mode="real")
        dfc = bc.get_ohlcv_deep(sym, CONFIRM_TF, max(400, LIMIT // 3), api_mode="real")
        dft = bc.get_ohlcv_deep(sym, TREND_TF, max(400, LIMIT // 12), api_mode="real")
        if dfe is None or len(dfe) < WARMUP + 30 or dfc is None or dft is None:
            continue
        got = max(got, len(dfe))
        dfe = ind.compute_indicators(dfe)
        dfc = ind.compute_indicators(dfc)
        dft = ind.compute_indicators(dft)
        t = run_pair(dfe, dfc, dft)
        for s in strats:
            agg[s] += t[s]
            per_pair[s][sym] = t[s]
    except Exception as ex:  # noqa: BLE001
        print(f"  {sym:9} ERROR {ex}")
        continue

print(f"\n==== entry {ENTRY_TF} / confirm {CONFIRM_TF} / trend {TREND_TF}  "
      f"candles/pair≈{got} ====")
print(f"{'strategy':12}{'n':>6}{'win%':>6}{'avgWinR':>9}{'avgLossR':>10}{'expR':>8}{'PF':>7}")
for s in strats:
    _line(s, agg[s])
    if DETAIL:
        for sym in PAIRS:
            if per_pair[s].get(sym):
                _line("  " + sym, per_pair[s][sym])
print("-" * 72)
print("🟢 = expectancy > 0 AND PF > 1.3 (real edge with the MTF filter applied).")
