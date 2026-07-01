#!/usr/bin/env python3
"""oos_backtest.py — TRUE out-of-sample test of the pair-selection method.

The remaining honest caveat is that the 20 pairs were picked by looking at the
WHOLE history (in-sample). This tool removes that bias:

  1. Split every pair's 1d history at a cut point (default 55% in / 45% out).
  2. IN-SAMPLE (older part): run breakout, rank pairs, SELECT the 🟢 ones
     (PF>1.3, expR>0, enough trades) — using ONLY the older data.
  3. OUT-OF-SAMPLE (newer part): trade ONLY those selected pairs and see if the
     edge survives on data the selection never saw.

If the OOS selected-basket is still 🟢 (and beats trading everything), then
"pick pairs by past breakout performance" genuinely generalises forward — the
strongest evidence short of live trading.

Usage:  .venv/bin/python oos_backtest.py [cut_frac] [candles]
        .venv/bin/python oos_backtest.py 0.55 3000
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import breakout

db.init_db()
_a = sys.argv[1:]
CUT = float(_a[0]) if len(_a) > 0 else 0.55
LIMIT = int(_a[1]) if len(_a) > 1 else 3000
INTERVAL = "1d"
IS_MIN_N = 6          # min in-sample trades to trust a pair's selection
IS_MIN_PF = 1.3       # in-sample bar to be "selected"

# Broad universe (same 38 liquid perps we tested), so selection has real choice.
UNIVERSE = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
            "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,UNIUSDT,ETCUSDT,XLMUSDT,"
            "BCHUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,FILUSDT,INJUSDT,SUIUSDT,"
            "AAVEUSDT,ALGOUSDT,ICPUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,"
            "MANAUSDT,AXSUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT,DYDXUSDT")
PAIRS = [p.strip() for p in UNIVERSE.split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200


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
    """Return list of (frac, R) for every breakout trade (frac = entry position)."""
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    n = len(c)
    span = max(1, n - WARMUP)
    out = []
    pos = None
    for i in range(WARMUP, n):
        if pos:
            d = pos["d"]; ex = None
            if d > 0:
                if lo[i] <= pos["sl"]:
                    ex = pos["sl"] * (1 - SLIP)
                elif hi[i] >= pos["tp"]:
                    ex = pos["tp"]
            else:
                if hi[i] >= pos["sl"]:
                    ex = pos["sl"] * (1 + SLIP)
                elif lo[i] <= pos["tp"]:
                    ex = pos["tp"]
            if ex is None and i - pos["i"] >= MAXHOLD:
                ex = c[i]
            if ex is not None:
                gross = (ex - pos["entry"]) * d
                fee = (pos["entry"] + ex) * FEE
                out.append(((pos["i"] - WARMUP) / span, (gross - fee) / pos["risk"]))
                pos = None
        if not pos:
            sub = df.iloc[:i + 1]
            if not ind.has_enough(sub):
                continue
            r = _valid(breakout.run(sub, mtf=False))
            if r:
                e = float(r["entry"]); sl = float(r["sl"]); tp = float(r["tp1"])
                d = 1 if r["signal"] == "BUY" else -1
                pos = {"i": i, "d": d, "entry": e, "sl": sl, "tp": tp, "risk": abs(e - sl)}
    return out


def _stats(rs):
    if not rs:
        return None
    w = [x for x in rs if x > 0]
    gW = sum(w); gL = -sum(x for x in rs if x <= 0)
    pf = gW / gL if gL > 0 else float("inf")
    return len(rs), 100 * len(w) // len(rs), sum(rs) / len(rs), pf


def _fmt(label, rs):
    s = _stats(rs)
    if not s:
        return f"{label:22} no trades"
    n, wr, exp, pf = s
    flag = " 🟢" if (exp > 0 and pf > 1.3) else " 🔴"
    return f"{label:22} n={n:<4} win%={wr:<3} expR={exp:+.3f} PF={pf:.2f}{flag}"


print("=" * 64)
print(f"OUT-OF-SAMPLE PAIR SELECTION  breakout 1d  cut={CUT:.0%} in / "
      f"{1-CUT:.0%} out")
print("select pairs on OLD data, test on NEW data the selection never saw")
print("=" * 64)

is_trades = {}   # pair -> in-sample R list
oos_trades = {}  # pair -> out-of-sample R list
for sym in PAIRS:
    try:
        df = bc.get_ohlcv_deep(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < WARMUP + 80:
            continue
        df = ind.compute_indicators(df)
        t = run_pair(df)
        is_trades[sym] = [r for f, r in t if f < CUT]
        oos_trades[sym] = [r for f, r in t if f >= CUT]
    except Exception:  # noqa: BLE001
        continue

# --- select on in-sample only ---
selected = []
for sym in PAIRS:
    s = _stats(is_trades.get(sym, []))
    if s and s[0] >= IS_MIN_N and s[2] > 0 and s[3] > IS_MIN_PF:
        selected.append(sym)

print(f"\nIN-SAMPLE selected {len(selected)} pairs (🟢 on OLD data):")
print("  " + (", ".join(selected) if selected else "none"))

sel_oos = [r for s in selected for r in oos_trades.get(s, [])]
all_oos = [r for s in PAIRS for r in oos_trades.get(s, [])]
drop_oos = [r for s in PAIRS if s not in selected for r in oos_trades.get(s, [])]

print("\nOUT-OF-SAMPLE results (data the selection never saw):")
print("  " + _fmt("selected basket", sel_oos))
print("  " + _fmt("all-universe basket", all_oos))
print("  " + _fmt("dropped pairs", drop_oos))

print("-" * 64)
ss = _stats(sel_oos)
if ss and ss[2] > 0 and ss[3] > 1.3:
    print("VERDICT 🟢 : selecting pairs by PAST breakout edge STILL WINS on unseen")
    print("future data — the method generalises, not just curve-fitting.")
else:
    print("VERDICT 🔴 : the in-sample pair edge did NOT carry to new data — pair")
    print("selection is overfit; trust the broad basket, not the hand-picked one.")
