#!/usr/bin/env python3
"""grand_sweep.py — every strategy x timeframe x R:R, ranked the way a trader
should rank them: by R PER MONTH among combos that survive a walk-forward.

WHY NOT RANK BY PF
  PF says how good each trade is. It says nothing about how many trades you get
  or how long your capital is tied up. The live result made the difference
  concrete: trend-12h at 1:3 has a fine backtest PF, yet after 18 days the open
  positions sat at +0.85R of the +3R they needed and 15 of 15 closes were stops.
  A PF of 1.74 on 5 trades a month grows the account slower than a PF of 1.35 on
  25, and the second is far easier to live with because it wins more often.

  So every combo is scored on:
    PF · win% · expR · entries/30d · MEDIAN HOLD DAYS · R PER MONTH
  and only combos that hold up across 3 eras (newest included) are ranked.

WHY R:R IS SWEPT WIDE (1:1 ... 1:3)
  Forcing 1:3 buys a bigger winner at the cost of win rate. At ~30% wins the
  break-even for 1:3 is 25% — almost no margin. Lower targets resolve faster and
  win more often; whether that is better is an empirical question, so it is
  measured instead of assumed.

Each strategy keeps ITS OWN stop (trend = EMA50, breakout = broken level,
reversion = 1%, emastoch = 1.5xATR) — that is what the live engine does; only
the target is set by the R:R multiple.

  cd /root/hpuaung/trading_bot && .venv/bin/python grand_sweep.py
  .venv/bin/python grand_sweep.py 1h,4h,6h,12h 1500
"""
import sys
import statistics
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, emastoch

db.init_db()

# Neutralise the live reversion tuning so the BASE strategy is measured, exactly
# as every earlier sweep did (otherwise the live RSI<20 gate silently applies).
_orig_get_float = db.get_float
def _patched_get_float(key, default=0.0):
    if key in ("reversion_rsi_extreme", "reversion_fixed_rr"):
        return 0.0
    return _orig_get_float(key, default)
db.get_float = _patched_get_float

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in
              (_a[0] if len(_a) > 0 else "30m,1h,4h,6h,12h,1d").split(",") if x.strip()]
LIMIT_OVERRIDE = int(_a[1]) if len(_a) > 1 else None

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

RRS = [1.0, 1.5, 2.0, 2.5, 3.0]
STRATS = ("trend", "breakout", "reversion", "emastoch")
FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
MIN_N = 40          # below this the numbers are noise

CANDLES = {"15m": 3000, "30m": 3000, "1h": 3000, "2h": 2000,
           "4h": 2000, "6h": 1500, "8h": 1500, "12h": 1500, "1d": 1500}
TF_HOURS = {"15m": .25, "30m": .5, "1h": 1, "2h": 2, "4h": 4,
            "6h": 6, "8h": 8, "12h": 12, "1d": 24}


def _valid(res):
    if not res or res.get("signal", "NONE") not in ("BUY", "SELL"):
        return None
    e, sl = float(res.get("entry", 0)), float(res.get("sl", 0))
    if e <= 0 or sl <= 0 or abs(e - sl) <= 0:
        return None
    if res["signal"] == "BUY" and sl >= e:
        return None
    if res["signal"] == "SELL" and sl <= e:
        return None
    return (1 if res["signal"] == "BUY" else -1, e, sl)


def collect(df):
    """One pass over the candles; cache each strategy's raw (dir, entry, sl).
    Signals are independent of R:R, so all R:R variants reuse this."""
    n = len(df)
    out = {s: [None] * n for s in STRATS}
    for i in range(WARMUP, n):
        sub = df.iloc[i - WARMUP:i + 1]
        if not ind.has_enough(sub):
            continue
        out["trend"][i] = _valid(trend.run(sub, mtf=False))
        out["breakout"][i] = _valid(breakout.run(sub, mtf=False))
        out["reversion"][i] = _valid(reversion.run(sub, mtf=False))
        out["emastoch"][i] = _valid(emastoch.run(sub))
    return out


def simulate(df, sigs, rr):
    """Returns (frac_through_history, R, hold_bars) per trade."""
    hi = df["high"].astype("float64").tolist()
    lo = df["low"].astype("float64").tolist()
    c = df["close"].astype("float64").tolist()
    n = len(c)
    span = max(1, n - WARMUP)
    out = []
    pos = None
    for i in range(WARMUP, n):
        if pos:
            d = pos["d"]
            ex = None
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
                gross = (ex - pos["e"]) * d
                fee = (pos["e"] + ex) * FEE
                out.append(((pos["i"] - WARMUP) / span,
                            (gross - fee) / pos["risk"], i - pos["i"]))
                pos = None
        if not pos and sigs[i]:
            d, e, sl = sigs[i]
            risk = abs(e - sl)
            pos = {"i": i, "d": d, "e": e, "sl": sl,
                   "tp": e + d * rr * risk, "risk": risk}
    return out


def pf_of(rs):
    if not rs:
        return None
    gW = sum(x for x in rs if x > 0)
    gL = -sum(x for x in rs if x <= 0)
    return gW / gL if gL > 0 else 99.0


print("=" * 104)
print("GRAND SWEEP — every strategy x timeframe x R:R, ranked by R PER MONTH")
print(f"pairs={len(PAIRS)}  strategies={list(STRATS)}  R:R={RRS}")
print("Each strategy keeps its own stop; only the target is set by the R:R.")
print("=" * 104)

rows = []   # dicts of every measured combo

for tf in TIMEFRAMES:
    limit = LIMIT_OVERRIDE or CANDLES.get(tf, 1500)
    tf_h = TF_HOURS.get(tf, 1)
    agg = {(s, rr): [] for s in STRATS for rr in RRS}
    span_days = 0.0
    print(f"\n[progress] {tf}: {len(PAIRS)} pairs (limit {limit}) ...", flush=True)
    for pi, sym in enumerate(PAIRS, 1):
        try:
            raw = bc.get_ohlcv_deep(sym, tf, limit, api_mode="real")
            if raw is None or len(raw) < WARMUP + 60:
                continue
            d = ind.compute_indicators(raw)
            span_days = max(span_days, len(d) * tf_h / 24.0)
            sigs = collect(d)
            for s in STRATS:
                for rr in RRS:
                    agg[(s, rr)] += simulate(d, sigs[s], rr)
            if pi % 5 == 0:
                print(f"[progress]   {pi}/{len(PAIRS)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[progress]   {sym}: err {str(e)[:40]}", flush=True)
            continue

    print(f"\n{'='*104}\n{tf}   ~{span_days:.0f} days history\n{'='*104}")
    print(f"{'strategy':11}{'R:R':>6}{'n':>7}{'win%':>6}{'expR':>9}{'PF':>7}"
          f"{'/30d':>8}{'holdD':>7}{'R/month':>9}  eras(1/2/3)      verdict")
    for s in STRATS:
        for rr in RRS:
            t = agg[(s, rr)]
            if len(t) < MIN_N:
                continue
            rs = [r for _, r, _ in t]
            holds = [h * tf_h / 24.0 for _, _, h in t]
            wins = [x for x in rs if x > 0]
            pf = pf_of(rs)
            exp = sum(rs) / len(rs)
            per30 = len(t) / span_days * 30 if span_days > 0 else 0
            rmonth = exp * per30
            eras = []
            for k in range(3):
                seg = [r for f, r, _ in t if k / 3 <= f < (k + 1) / 3]
                eras.append(pf_of(seg))
            ok = (all(e is not None and e > 1.0 for e in eras)
                  and (eras[2] or 0) >= 1.2 and exp > 0)
            eras_s = "/".join(f"{(e if e else 0):.2f}" for e in eras)
            print(f"{s:11}{'1:'+f'{rr:g}':>6}{len(t):>7}{100*len(wins)//len(t):>6}"
                  f"{exp:>+9.3f}{pf:>7.2f}{per30:>8.1f}{statistics.median(holds):>7.1f}"
                  f"{rmonth:>9.2f}  {eras_s:16}{'✅ ROBUST' if ok else '❌'}")
            rows.append(dict(tf=tf, s=s, rr=rr, n=len(t), win=100*len(wins)//len(t),
                             exp=exp, pf=pf, per30=per30,
                             hold=statistics.median(holds), rmonth=rmonth, ok=ok))

# ---------------------------------------------------------------------------
print("\n" + "=" * 104)
print("THE ANSWER — ROBUST combos ranked by R PER MONTH (what actually grows the account)")
print("=" * 104)
good = [r for r in rows if r["ok"]]
if not good:
    print("  NONE survived the 3-era walk-forward with a positive expectancy.")
    print("  That is the honest result: on this data no configuration of these")
    print("  strategies is dependable. Nothing here is worth trading live.")
else:
    print(f"{'#':>3} {'timeframe':10}{'strategy':11}{'R:R':>6}{'R/month':>9}{'PF':>7}"
          f"{'win%':>6}{'expR':>9}{'/30d':>8}{'holdD':>7}{'n':>7}")
    for i, r in enumerate(sorted(good, key=lambda x: -x["rmonth"])[:20], 1):
        rrlab = f"1:{r['rr']:g}"
        print(f"{i:>3} {r['tf']:10}{r['s']:11}{rrlab:>6}"
              f"{r['rmonth']:>9.2f}{r['pf']:>7.2f}{r['win']:>6}{r['exp']:>+9.3f}"
              f"{r['per30']:>8.1f}{r['hold']:>7.1f}{r['n']:>7}")

    best = max(good, key=lambda x: x["rmonth"])
    fastest = min(good, key=lambda x: x["hold"])
    winniest = max(good, key=lambda x: x["win"])
    print("\n" + "-" * 104)
    print("PICKS")
    print("-" * 104)
    print(f"  Fastest account growth : {best['s']} {best['tf']} 1:{best['rr']:g}"
          f"  → {best['rmonth']:.2f} R/month "
          f"(PF {best['pf']:.2f}, win {best['win']}%, {best['per30']:.1f}/30d, "
          f"median hold {best['hold']:.1f}d)")
    print(f"  Quickest resolution    : {fastest['s']} {fastest['tf']} 1:{fastest['rr']:g}"
          f"  → median hold {fastest['hold']:.1f}d ({fastest['rmonth']:.2f} R/month)")
    print(f"  Highest win rate       : {winniest['s']} {winniest['tf']} 1:{winniest['rr']:g}"
          f"  → {winniest['win']}% wins ({winniest['rmonth']:.2f} R/month)")
    print("\n  Currently live for comparison: trend-12h 1:3 and emastoch-6h 1:3.")
    print("  Find them in the table above — if they are not near the top, the")
    print("  bot is running the wrong configuration.")

print("\n" + "=" * 104)
print("HOW TO READ")
print("=" * 104)
print("R/month = expR x entries per 30 days. It is the growth rate of the account")
print("in units of risk, and it is the only column that combines edge WITH")
print("frequency. A high PF on a handful of trades a month loses to a modest PF")
print("that trades often.")
print("holdD = median days a trade stays open. Long holds tie up capital and are")
print("the reason the live 1:3 positions sat at +0.85R for 18 days.")
print("Only ✅ ROBUST rows (edge in all 3 eras, newest PF >= 1.2) are ranked.")
