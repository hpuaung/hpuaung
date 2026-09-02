#!/usr/bin/env python3
"""walkforward.py — out-of-sample validation for the swing_sweep winners before
we trust any of them live. For each candidate (strategy, timeframe, R:R) it
splits every pair's history into 3 chronological segments (oldest→newest) and
reports the edge in EACH, then a verdict:

  ROBUST   — all 3 segments positive AND the NEWEST still has PF>=1.2
  FRAGILE  — the newest segment decays (this is what killed reversion scalping)
  FAIL     — loses overall / no edge

The newest segment matters most: it is the closest thing to "what happens next".
An edge that only lived in the old era is overfit and must not be deployed.

Windowed slices (O(n)) + progress logging; safe to run backgrounded to a log.

Usage:
  cd /root/hpuaung/trading_bot && .venv/bin/python walkforward.py
  .venv/bin/python walkforward.py 1500        # candles per pair
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()

# neutralise reversion scalping tuning in-process (no DB write)
_orig_get_float = db.get_float
def _patched_get_float(key, default=0.0):
    if key in ("reversion_rsi_extreme", "reversion_fixed_rr"):
        return 0.0
    return _orig_get_float(key, default)
db.get_float = _patched_get_float

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
SEGMENTS = 3

# top swing_sweep edges, ranked by activity — validate each out-of-sample
CANDIDATES = [
    ("breakout", "4h", 3.0),
    ("breakout", "6h", 3.0),
    ("trend",    "6h", 3.0),
    ("breakout", "8h", 3.0),
    ("trend",    "8h", 3.0),
    ("trend",    "12h", 3.0),
    ("breakout", "1d", 3.0),
    ("breakout", "1d", None),   # proven baseline (native TP)
]

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
TF_HOURS = {"4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24, "3d": 72}
RUNNERS = {"trend": trend, "reversion": reversion, "breakout": breakout}


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


def run_pair(df, strat_mod, rr):
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
            sub = df.iloc[i - WARMUP:i + 1]      # window, not full history (O(n))
            if not ind.has_enough(sub):
                continue
            r = _valid(strat_mod.run(sub, mtf=False))
            if r:
                e = float(r["entry"]); sl = float(r["sl"]); tp = float(r["tp1"])
                d = 1 if r["signal"] == "BUY" else -1
                if rr is not None:
                    tp = e + d * rr * abs(e - sl)
                pos = {"i": i, "d": d, "entry": e, "sl": sl, "tp": tp, "risk": abs(e - sl)}
    return out


def _stats(rs):
    if not rs:
        return None
    w = [x for x in rs if x > 0]
    gW = sum(w); gL = -sum(x for x in rs if x <= 0)
    pf = gW / gL if gL > 0 else 99.0
    return len(rs), 100 * len(w) // len(rs), sum(rs) / len(rs), pf


def _fmt(label, s):
    if not s:
        return f"  {label:16}   no trades"
    n, wr, exp, pf = s
    return f"  {label:16}{n:>5}{wr:>6}{exp:>+9.3f}{pf:>7.2f}"


print("=" * 68)
print(f"WALK-FORWARD VALIDATION of swing_sweep winners  ({SEGMENTS} segments)")
print(f"pairs={len(PAIRS)}  candles<= {LIMIT}   newest segment must hold")
print("=" * 68)

summary = []
for strat, tf, rr in CANDIDATES:
    rrlab = "nat" if rr is None else f"1:{rr:g}"
    tag = f"{strat} {tf} {rrlab}"
    print(f"\n[progress] validating {tag} ...", flush=True)
    seg = {k: [] for k in range(SEGMENTS)}
    max_candles = 0
    mod = RUNNERS[strat]
    for pi, sym in enumerate(PAIRS, 1):
        try:
            df = bc.get_ohlcv_deep(sym, tf, LIMIT, api_mode="real")
            if df is None or len(df) < WARMUP + 60:
                continue
            max_candles = max(max_candles, len(df))
            df = ind.compute_indicators(df)
            for frac, r in run_pair(df, mod, rr):
                k = min(SEGMENTS - 1, int(frac * SEGMENTS))
                seg[k].append(r)
        except Exception:  # noqa: BLE001
            continue
    print(f"\n=== {tag} ===")
    print(f"  {'period':16}{'n':>5}{'win%':>6}{'expR':>9}{'PF':>7}")
    segstats = [_stats(seg[k]) for k in range(SEGMENTS)]
    for k in range(SEGMENTS):
        newest = " <- NEWEST" if k == SEGMENTS - 1 else ""
        print(_fmt(f"segment {k+1}/{SEGMENTS}", segstats[k]) + newest)
    allr = [r for k in range(SEGMENTS) for r in seg[k]]
    ov = _stats(allr)
    print(_fmt("OVERALL", ov))

    # verdict
    span_days = max_candles * TF_HOURS.get(tf, 24) / 24.0
    per30 = (len(allr) / span_days * 30) if span_days > 0 else 0
    all_pos = all(s and s[2] > 0 for s in segstats)
    new = segstats[-1]
    if not ov or ov[2] <= 0:
        verdict = "FAIL (no overall edge)"
    elif all_pos and new and new[3] >= 1.2:
        verdict = "ROBUST"
    elif not new or new[2] <= 0 or new[3] < 1.0:
        verdict = "FRAGILE (newest decays)"
    else:
        verdict = "MIXED (weak newest)"
    print(f"  VERDICT: {verdict}   |  ~{per30:.1f} entries/30d")
    summary.append((tag, verdict, ov, per30))

print("\n" + "=" * 68)
print("SUMMARY — deploy only ROBUST, prefer higher entries/30d")
print("=" * 68)
print(f"{'candidate':22}{'verdict':24}{'PF':>6}{'expR':>8}{'ent/30d':>9}")
order = {"ROBUST": 0, "MIXED (weak newest)": 1, "FRAGILE (newest decays)": 2}
for tag, verdict, ov, per30 in sorted(
        summary, key=lambda x: (order.get(x[1], 3), -x[3])):
    pf = f"{ov[3]:.2f}" if ov else "-"
    exp = f"{ov[2]:+.3f}" if ov else "-"
    print(f"{tag:22}{verdict:24}{pf:>6}{exp:>8}{per30:>9.1f}")
print("\nROBUST + high entries/30d = the swing upgrade. FRAGILE = do NOT deploy.")
