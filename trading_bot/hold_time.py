#!/usr/bin/env python3
"""hold_time.py — how LONG does the validated edge take per trade?

The forward test stalled because trades sit for weeks: after 7 days the best
open position was only +0.82R of the +3R it needs. Before waiting any longer,
measure the actual holding-time distribution of the SAME backtest that produced
PF 1.36-1.54, so the wait is a known number instead of a guess.

For the live configs (trend 6h and trend 12h, forced R:R 1:3) it reports:
  * how trades exit: TP / SL / still-open-at-cap
  * mean + median hold in days
  * what fraction resolve within 7 / 14 / 30 / 60 days
  * the practical answer: with N parallel slots, how many CLOSED trades to
    expect in a 14-day live window

  cd /root/hpuaung/trading_bot && .venv/bin/python hold_time.py
  .venv/bin/python hold_time.py 6h,12h 1500
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend

db.init_db()

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in (_a[0] if len(_a) > 0 else "6h,12h").split(",") if x.strip()]
LIMIT = int(_a[1]) if len(_a) > 1 else 1500

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
RR = 3.0
# Deliberately generous so the TRUE distribution shows; the live max_hold (30d)
# is applied afterwards as a reporting cut, not as a simulation limit.
MAXHOLD = 400
TF_HOURS = {"4h": 4, "6h": 6, "8h": 8, "12h": 12, "1d": 24}


def run_pair(df, tf_h):
    """One pass; returns list of (hold_days, exit_kind, r_multiple)."""
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    n = len(c)
    out = []
    pos = None
    for i in range(WARMUP, n):
        if pos:
            d = pos["d"]
            ex = kind = None
            if d > 0:
                if lo[i] <= pos["sl"]:
                    ex, kind = pos["sl"] * (1 - SLIP), "SL"
                elif hi[i] >= pos["tp"]:
                    ex, kind = pos["tp"], "TP"
            else:
                if hi[i] >= pos["sl"]:
                    ex, kind = pos["sl"] * (1 + SLIP), "SL"
                elif lo[i] <= pos["tp"]:
                    ex, kind = pos["tp"], "TP"
            if ex is None and i - pos["i"] >= MAXHOLD:
                ex, kind = c[i], "TIME"
            if ex is not None:
                held = (i - pos["i"]) * tf_h / 24.0
                gross = (ex - pos["entry"]) * d
                fee = (pos["entry"] + ex) * FEE
                out.append((held, kind, (gross - fee) / pos["risk"]))
                pos = None
        if not pos:
            sub = df.iloc[i - WARMUP:i + 1]
            if not ind.has_enough(sub):
                continue
            r = trend.run(sub, mtf=False)
            if not r or r.get("signal") not in ("BUY", "SELL"):
                continue
            e, sl = float(r.get("entry", 0)), float(r.get("sl", 0))
            if e <= 0 or sl <= 0 or abs(e - sl) <= 0:
                continue
            d = 1 if r["signal"] == "BUY" else -1
            if (d > 0 and sl >= e) or (d < 0 and sl <= e):
                continue
            pos = {"i": i, "d": d, "entry": e, "sl": sl,
                   "tp": e + d * RR * abs(e - sl), "risk": abs(e - sl)}
    return out


def pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = int(len(s) * q)
    return s[min(k, len(s) - 1)]


print("=" * 78)
print("HOLDING-TIME REALITY CHECK — trend @ R:R 1:3 (the live config)")
print(f"pairs={len(PAIRS)}  candles/pair<={LIMIT}")
print("=" * 78)

for tf in TIMEFRAMES:
    tf_h = TF_HOURS.get(tf, 6)
    trades = []
    print(f"\n[progress] {tf}: scanning {len(PAIRS)} pairs ...", flush=True)
    for pi, sym in enumerate(PAIRS, 1):
        try:
            df = bc.get_ohlcv_deep(sym, tf, LIMIT, api_mode="real")
            if df is None or len(df) < WARMUP + 50:
                continue
            trades += run_pair(ind.compute_indicators(df), tf_h)
            if pi % 10 == 0:
                print(f"[progress]   {pi}/{len(PAIRS)} done, {len(trades)} trades",
                      flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[progress]   {sym}: err {str(e)[:30]}", flush=True)
            continue

    if not trades:
        print(f"\n==== {tf}: no trades ====")
        continue

    holds = [h for h, _, _ in trades]
    kinds = {}
    for _, k, _ in trades:
        kinds[k] = kinds.get(k, 0) + 1
    rs = [r for _, _, r in trades]
    wins = [r for r in rs if r > 0]
    gW, gL = sum(wins), -sum(r for r in rs if r <= 0)

    print(f"\n==== {tf}  n={len(trades)}  PF={gW/gL if gL>0 else 99:.2f}  "
          f"expR={sum(rs)/len(rs):+.3f} ====")
    print("  exit type :", ", ".join(f"{k} {v} ({100*v//len(trades)}%)"
                                     for k, v in sorted(kinds.items())))
    print(f"  hold days : mean {sum(holds)/len(holds):.1f}   median {pct(holds,0.5):.1f}   "
          f"p25 {pct(holds,0.25):.1f}   p75 {pct(holds,0.75):.1f}   max {max(holds):.0f}")
    for cut in (7, 14, 30, 60):
        share = 100.0 * sum(1 for h in holds if h <= cut) / len(holds)
        print(f"    resolved within {cut:>2}d : {share:5.1f}%")

    # Practical: entries/30d across the universe, and expected CLOSES in 14 days.
    span_days = LIMIT * tf_h / 24.0
    per30 = len(trades) / span_days * 30 if span_days > 0 else 0
    res14 = sum(1 for h in holds if h <= 14) / len(holds)
    print(f"  live estimate: ~{per30:.0f} entries/30d across {len(PAIRS)} pairs "
          f"→ ~{per30*14/30:.0f} entries in 14d, of which ~{res14*100:.0f}% resolve "
          f"→ ~{per30*14/30*res14:.0f} CLOSED trades in a 14-day window")

print("\n" + "=" * 78)
print("HOW TO READ THIS")
print("=" * 78)
print("If most trades need >30 days, the live max_hold of 30d is CUTTING them")
print("short — the live bot then cannot reproduce the backtest PF, because the")
print("backtest let them run. And a forward test needs at least the median hold")
print("time before any verdict is possible.")
print("If 'CLOSED trades in a 14-day window' is under ~10, this configuration")
print("simply cannot be validated in two weeks, and a faster-resolving variant")
print("(tighter ATR stop, or a lower timeframe) has to be backtested instead.")
