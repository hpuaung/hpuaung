#!/usr/bin/env python3
"""robust_backtest.py — WALK-FORWARD / out-of-sample robustness check for the
deployed breakout strategy. It splits each pair's history into N chronological
segments (oldest → newest) and reports the edge in EACH segment, so we can see
whether breakout holds up across DIFFERENT market eras (robust) or only made
money in one lucky period (overfit / fragile).

Uses the bot's real breakout.run() and its own SL/TP1 exits — same as
strategy_backtest.py, just bucketed by time.

Usage (on the VPS, in trading_bot/):
  .venv/bin/python robust_backtest.py all 1d 3000 3      # pairs tf candles segments
  .venv/bin/python robust_backtest.py all 1d 3000 4 detail
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
pairs_arg = _a[0] if len(_a) > 0 else "all"
INTERVAL = _a[1] if len(_a) > 1 else "1d"
LIMIT = int(_a[2]) if len(_a) > 2 else 3000
SEGMENTS = int(_a[3]) if len(_a) > 3 else 3
DETAIL = "detail" in _a

# The deployed 20-pair basket (fallback to selected_pairs).
BASKET = ("BTCUSDT,SOLUSDT,XRPUSDT,AVAXUSDT,LTCUSDT,DOTUSDT,ETCUSDT,XLMUSDT,"
          "NEARUSDT,FILUSDT,AAVEUSDT,ALGOUSDT,VETUSDT,HBARUSDT,GRTUSDT,"
          "SANDUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT")
if pairs_arg == "all":
    PAIRS = [p.strip() for p in BASKET.split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

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
    """Return list of (frac, R): frac = entry position in this pair's timeline."""
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


def _line(label, rs):
    s = _stats(rs)
    if not s:
        print(f"{label:16}   no trades")
        return
    n, wr, exp, pf = s
    edge = " 🟢" if (exp > 0 and pf > 1.3) else (" 🔴" if exp <= 0 else "")
    print(f"{label:16}{n:>5}{wr:>6}{exp:>+9.3f}{pf:>7.2f}{edge}")


seg_all = {k: [] for k in range(SEGMENTS)}
per_pair = {}
print("=" * 64)
print(f"WALK-FORWARD ROBUSTNESS  breakout {INTERVAL}  {SEGMENTS} segments "
      f"(oldest→newest)")
print("edge must hold across segments to be robust (not one lucky era)")
print("=" * 64)
for sym in PAIRS:
    try:
        df = bc.get_ohlcv_deep(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < WARMUP + 60:
            continue
        df = ind.compute_indicators(df)
        trades = run_pair(df)
        buckets = {k: [] for k in range(SEGMENTS)}
        for frac, r in trades:
            k = min(SEGMENTS - 1, int(frac * SEGMENTS))
            seg_all[k].append(r)
            buckets[k].append(r)
        per_pair[sym] = buckets
    except Exception:  # noqa: BLE001
        continue

print(f"\n{'period':16}{'n':>5}{'win%':>6}{'expR':>9}{'PF':>7}")
for k in range(SEGMENTS):
    _line(f"segment {k+1}/{SEGMENTS}", seg_all[k])
allr = [r for k in range(SEGMENTS) for r in seg_all[k]]
print("-" * 44)
_line("OVERALL", allr)

if DETAIL:
    print("\nper-pair per-segment expR (🟢 exp>0&PF>1.3):")
    for sym in PAIRS:
        if sym not in per_pair:
            continue
        cells = []
        for k in range(SEGMENTS):
            s = _stats(per_pair[sym][k])
            cells.append(f"{s[2]:+.2f}" if s else "  -  ")
        print(f"  {sym:10} " + " | ".join(cells))

print("-" * 64)
print("READ: if every segment is 🟢 (or at least positive), the edge is robust")
print("across eras. If only one segment carries it, the edge is fragile/overfit.")
