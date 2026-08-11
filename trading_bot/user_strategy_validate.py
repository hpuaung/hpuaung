#!/usr/bin/env python3
"""user_strategy_validate.py — is the user's strategy REALLY good, or just lucky?

The first sweep found PF 1.85-2.01, beating the live system (trend-12h 1:3 PF
1.54) — but on only n=35 trades, and only at confirmation window W=5. That is
exactly the sample size where luck looks like an edge. This settles it:

  1. Sweep the confirmation window properly (W = 5/10/20/30). A wider window
     produced 1726 signal bars on 12h versus 40 at W=5, so it should yield a
     sample large enough to trust.
  2. Test the rule subsets the ablation pointed at. Dropping the RSI cross and
     the price-action rule gave MORE trades AND a higher PF, so those variants
     are tested as first-class candidates, not afterthoughts.
  3. WALK-FORWARD every promising combo across 3 equal eras. A full-sample PF
     can come from one favourable regime — that is what killed the reversion
     scalper. Only an edge holding in all three eras, and still standing in the
     NEWEST one, is tradable.

  cd /root/hpuaung/trading_bot && .venv/bin/python user_strategy_validate.py
  .venv/bin/python user_strategy_validate.py 1h,4h,6h,12h
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import numpy as np

import database as db
from utils import binance_client as bc
from user_strategy_test import (prepare, signals, stat, FEE, SLIP, WARMUP,
                                MAXHOLD, SL_ATR_MULT, CANDLES, TF_HOURS)

db.init_db()

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in
              (_a[0] if len(_a) > 0 else "1h,4h,6h,12h").split(",") if x.strip()]
PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

WINDOWS = [5, 10, 20, 30]
RRS = [1.5, 2.0, 3.0]
RULESETS = [
    ("FULL (as you wrote it)", dict()),
    ("no RSI cross",           dict(use_rsi=False)),
    ("no price action",        dict(use_pa=False)),
    ("CORE (EMA+slope+Stoch)", dict(use_rsi=False, use_pa=False)),
]
N_TRUST = 100      # below this, PF is not trustworthy
N_WALK = 60        # minimum sample to attempt a 3-era walk-forward


def simulate_frac(d, buy, sell, rr):
    """Same simulation, but each trade also records WHERE in the history it
    happened (0..1) so the sample can be split into eras."""
    hi = d["high"].astype("float64").to_numpy()
    lo = d["low"].astype("float64").to_numpy()
    c = d["close"].astype("float64").to_numpy()
    atr = d["atr"].astype("float64").to_numpy()
    n = len(c)
    span = max(1, n - WARMUP)
    out = []
    pos = None
    for i in range(WARMUP, n):
        if pos:
            dd = pos["d"]
            ex = None
            if dd > 0:
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
                gross = (ex - pos["e"]) * dd
                fee = (pos["e"] + ex) * FEE
                out.append(((pos["i"] - WARMUP) / span, (gross - fee) / pos["risk"]))
                pos = None
        if not pos and (buy[i] or sell[i]):
            a = atr[i]
            if not np.isfinite(a) or a <= 0:
                continue
            dd = 1 if buy[i] else -1
            e = c[i]
            risk = SL_ATR_MULT * a
            pos = {"i": i, "d": dd, "e": e, "sl": e - dd * risk,
                   "tp": e + dd * rr * risk, "risk": risk}
    return out


def pf_of(rs):
    if not rs:
        return None
    gW = sum(x for x in rs if x > 0)
    gL = -sum(x for x in rs if x <= 0)
    return gW / gL if gL > 0 else 99.0


print("=" * 92)
print("VALIDATION — is your strategy a real edge, or a small-sample illusion?")
print(f"pairs={len(PAIRS)}   windows={WINDOWS}   R:R={RRS}   rule sets={len(RULESETS)}")
print("=" * 92)

candidates = []   # (tf, label, w, rr, st, trades)

for tf in TIMEFRAMES:
    limit = CANDLES.get(tf, 1500)
    prepared = []
    span_days = 0.0
    print(f"\n[progress] {tf}: fetching {len(PAIRS)} pairs (limit {limit}) ...", flush=True)
    for pi, sym in enumerate(PAIRS, 1):
        try:
            raw = bc.get_ohlcv_deep(sym, tf, limit, api_mode="real")
            if raw is None or len(raw) < WARMUP + 60:
                continue
            prepared.append(prepare(raw))
            span_days = max(span_days, len(prepared[-1]) * TF_HOURS.get(tf, 1) / 24.0)
            if pi % 10 == 0:
                print(f"[progress]   {pi}/{len(PAIRS)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[progress]   {sym}: err {str(e)[:40]}", flush=True)
            continue
    if not prepared:
        continue

    print(f"\n{'='*92}\n{tf}   ~{span_days:.0f} days history, {len(prepared)} pairs\n{'='*92}")
    print(f"{'rule set':24}{'W':>4}{'R:R':>7}{'n':>7}{'win%':>6}{'expR':>9}"
          f"{'PF':>7}{'/30d':>8}  sample")
    for label, kw in RULESETS:
        for w in WINDOWS:
            for rr in RRS:
                trades = []
                for d in prepared:
                    b, s = signals(d, w, **kw)
                    trades += simulate_frac(d, b, s, rr)
                rs = [r for _, r in trades]
                st = stat(rs, span_days)
                if not st or st["n"] < 20:
                    continue
                tag = "TRUSTED" if st["n"] >= N_TRUST else "thin"
                print(f"{label:24}{w:>4}{'1:'+f'{rr:g}':>7}{st['n']:>7}{st['win']:>6}"
                      f"{st['exp']:>+9.3f}{st['pf']:>7.2f}{st['per30']:>8.1f}  {tag}")
                if st["n"] >= N_WALK and st["pf"] > 1.2:
                    candidates.append((tf, label, w, rr, st, trades))

# ---------------------------------------------------------------------------
print("\n" + "=" * 92)
print("WALK-FORWARD — does the edge survive in all 3 eras (and the NEWEST one)?")
print("=" * 92)
if not candidates:
    print("  No combo reached n>=60 with PF>1.2, so there is nothing solid enough")
    print("  to walk-forward. The strategy did not produce a trustworthy sample.")
else:
    candidates.sort(key=lambda x: -x[4]["pf"])
    print(f"{'tf':6}{'rule set':24}{'W':>4}{'R:R':>7}{'n':>6}{'PF':>7}"
          f"{'era1':>8}{'era2':>8}{'era3(new)':>11}   verdict")
    robust = []
    for tf, label, w, rr, st, trades in candidates[:20]:
        eras = []
        for k in range(3):
            seg = [r for f, r in trades if k / 3 <= f < (k + 1) / 3]
            eras.append(pf_of(seg))
        shown = [f"{(e if e is not None else 0):.2f}" for e in eras]
        ok = all(e is not None and e > 1.0 for e in eras) and (eras[2] or 0) >= 1.2
        verdict = "✅ ROBUST" if ok else "❌ fragile"
        print(f"{tf:6}{label:24}{w:>4}{'1:'+f'{rr:g}':>7}{st['n']:>6}{st['pf']:>7.2f}"
              f"{shown[0]:>8}{shown[1]:>8}{shown[2]:>11}   {verdict}")
        if ok:
            robust.append((tf, label, w, rr, st))

    print("\n" + "=" * 92)
    print("VERDICT")
    print("=" * 92)
    if not robust:
        print("  ❌ Nothing ROBUST. Every promising combo failed in at least one era —")
        print("     usually the newest. That is the same decay that killed the")
        print("     reversion scalper. Do NOT switch the live bot to it.")
    else:
        print("  ✅ ROBUST combos (edge held across all 3 eras, newest PF >= 1.2):\n")
        print(f"  {'tf':6}{'rule set':24}{'W':>4}{'R:R':>7}{'PF':>7}{'expR':>9}{'n':>6}{'/30d':>8}")
        for tf, label, w, rr, st in sorted(robust, key=lambda x: -x[4]["pf"]):
            print(f"  {tf:6}{label:24}{w:>4}{'1:'+f'{rr:g}':>7}{st['pf']:>7.2f}"
                  f"{st['exp']:>+9.3f}{st['n']:>6}{st['per30']:>8.1f}")
        print("\n  Benchmark to beat (same pairs, fees, slippage, and validated the")
        print("  same way): trend-12h 1:3 PF 1.54 n=371 ~15/30d")
        print("                trend-6h  1:3 PF 1.51 n=303 ~24/30d")
        print("\n  Switch only if a ROBUST row beats that PF *and* trades often enough")
        print("  to matter. A higher PF on 3 trades a month is not an improvement.")
