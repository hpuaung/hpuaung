#!/usr/bin/env python3
"""reversion_test.py — reversion was the only scalping strategy with a pulse
(breakeven at R:R 1:3). This tries to push it OVER the edge bar by taking only
HIGH-QUALITY reversion setups (RSI extreme / price beyond a Bollinger band /
volume), exiting at a fixed R:R. If a filter clears PF>1.3 with enough trades,
we found a scalping config; if not, reversion scalping tops out at breakeven.

  .venv/bin/python reversion_test.py [pairs] [intervals] [candles] [rr]
  .venv/bin/python reversion_test.py all 15m,30m 6000 3
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import reversion

db.init_db()
_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
INTERVALS = [x.strip() for x in (_a[1] if len(_a) > 1 else "15m,30m").split(",") if x.strip()]
LIMIT = int(_a[2]) if len(_a) > 2 else 6000
RR = float(_a[3]) if len(_a) > 3 else 3.0

DEF = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,LTCUSDT"
if pairs_arg == "all":
    PAIRS = [p.strip() for p in DEF.split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 120

# (label, rsi_th, need_bb, vol_th)  — BUY needs rsi<=rsi_th (SELL rsi>=100-rsi_th)
CONFIGS = [
    ("baseline", None, False, 0),
    ("rsi<30", 30, False, 0),
    ("rsi<25", 25, False, 0),
    ("rsi<20", 20, False, 0),
    ("bb-break", None, True, 0),
    ("rsi<25+bb", 25, True, 0),
    ("rsi<25+bb+vol1.3", 25, True, 1.3),
]


def precompute(df):
    n = len(df)
    close = df["close"].astype(float).tolist()
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    rsi = df["rsi"].fillna(50).astype(float).tolist() if "rsi" in df else [50]*n
    bbl = df["bb_lower"].astype(float).tolist() if "bb_lower" in df else [0]*n
    bbu = df["bb_upper"].astype(float).tolist() if "bb_upper" in df else [1e9]*n
    vol = df["volume"].astype(float).tolist()
    vma = df["vol_ma"].fillna(0).astype(float).tolist() if "vol_ma" in df else [0]*n
    sig = [None]*n
    for i in range(WARMUP, n):
        sub = df.iloc[:i+1]
        if not ind.has_enough(sub):
            continue
        r = reversion.run(sub, mtf=False)
        s = r.get("signal", "NONE")
        if s in ("BUY", "SELL"):
            e = float(r.get("entry", 0)); sl = float(r.get("sl", 0))
            if e > 0 and sl > 0 and abs(e-sl) > 0 and (
                    (s == "BUY" and sl < e) or (s == "SELL" and sl > e)):
                sig[i] = (s, e, sl)
    return dict(n=n, c=close, hi=hi, lo=lo, rsi=rsi, bbl=bbl, bbu=bbu,
                vol=vol, vma=vma, sig=sig)


def passes(cfg, d, i):
    s = d["sig"][i][0]
    _, rsi_th, need_bb, vol_th = cfg
    if rsi_th is not None:
        if s == "BUY" and d["rsi"][i] > rsi_th:
            return False
        if s == "SELL" and d["rsi"][i] < 100 - rsi_th:
            return False
    if need_bb:
        if s == "BUY" and d["c"][i] > d["bbl"][i]:
            return False
        if s == "SELL" and d["c"][i] < d["bbu"][i]:
            return False
    if vol_th:
        vb = d["vma"][i] or 1e-9
        if d["vol"][i] / vb < vol_th:
            return False
    return True


def simulate(cfg, d):
    trades = []
    pos = None
    for i in range(WARMUP, d["n"]):
        if pos:
            dd = pos["d"]; ex = None
            if dd > 0:
                if d["lo"][i] <= pos["sl"]:
                    ex = pos["sl"]*(1-SLIP)
                elif d["hi"][i] >= pos["tp"]:
                    ex = pos["tp"]
            else:
                if d["hi"][i] >= pos["sl"]:
                    ex = pos["sl"]*(1+SLIP)
                elif d["lo"][i] <= pos["tp"]:
                    ex = pos["tp"]
            if ex is None and i - pos["i"] >= MAXHOLD:
                ex = d["c"][i]
            if ex is not None:
                gross = (ex - pos["entry"])*dd
                fee = (pos["entry"]+ex)*FEE
                frac = (pos["i"]-WARMUP)/max(1, d["n"]-WARMUP)
                trades.append((frac, (gross-fee)/pos["risk"]))
                pos = None
        if not pos and d["sig"][i] and passes(cfg, d, i):
            s, e, sl = d["sig"][i]
            dd = 1 if s == "BUY" else -1
            tp = e + dd*RR*abs(e-sl)
            pos = {"i": i, "d": dd, "entry": e, "sl": sl, "tp": tp, "risk": abs(e-sl)}
    return trades


def line(label, t):
    rs = [r for _, r in t]
    if not rs:
        print(f"  {label:18} no trades")
        return
    w = [x for x in rs if x > 0]
    gW = sum(w); gL = -sum(x for x in rs if x <= 0)
    pf = gW/gL if gL > 0 else 99
    exp = sum(rs)/len(rs)
    flag = " 🟢" if (exp > 0 and pf > 1.3) else ""
    print(f"  {label:18} n={len(rs):<5} win%={100*len(w)//len(rs):<3} "
          f"expR={exp:+.3f} PF={pf:.2f}{flag}")


print("="*64)
print(f"REVERSION QUALITY-FILTER TEST  R:R 1:{RR:g}  timeframes={','.join(INTERVALS)}")
print("can a quality filter push reversion scalping over PF 1.3?")
print("="*64)
for iv in INTERVALS:
    data = {}
    for sym in PAIRS:
        try:
            df = bc.get_ohlcv_deep(sym, iv, LIMIT, api_mode="real")
            if df is None or len(df) < WARMUP+60:
                continue
            data[sym] = precompute(ind.compute_indicators(df))
        except Exception:  # noqa: BLE001
            continue
    print(f"\n==== interval={iv} ====")
    for cfg in CONFIGS:
        allt = []
        for sym in data:
            allt += simulate(cfg, data[sym])
        line(cfg[0], allt)
    # walk-forward for the winning config (rsi<20) — is it robust across eras?
    win_cfg = ("rsi<20", 20, False, 0)
    wf = []
    for sym in data:
        wf += simulate(win_cfg, data[sym])
    print(f"  -- walk-forward rsi<20 (oldest->newest) --")
    for k in range(3):
        seg = [(f, r) for (f, r) in wf if k/3 <= f < (k+1)/3]
        line(f"   seg {k+1}/3", seg)
print("-"*64)
print("🟢 = expR>0 AND PF>1.3. If none clear it, reversion scalping tops out at")
print("breakeven and no filter creates a real edge.")
