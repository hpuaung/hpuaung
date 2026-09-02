#!/usr/bin/env python3
"""user_strategy_test.py — backtest the user's own 3-indicator + price-action
system across many timeframes and R:R values, for BOTH scalping and swing.

THE RULES BEING TESTED (as specified)
  1. EMA 9 / 21 / 200 stack
       BUY : ema9 > ema21 > ema200, all sloping UP
       SELL: ema9 < ema21 < ema200, all sloping DOWN
     ("30 degrees" cannot be measured without a chart scale, so slope is
      expressed in ATR units per bar; the ablation shows the effect of dropping
      the slope rule entirely.)
  2. RSI two levels, 55 and 45
       BUY : RSI crosses UP through 55
       SELL: RSI crosses DOWN through 45
  3. Stochastic %K, 20 and 80
       BUY : %K crosses UP through 20
       SELL: %K crosses DOWN through 80
  4. Price action
       BUY : candle closes ABOVE both EMA9 and EMA21, bullish body, close in the
             upper half of its range (a clean close, not a rejection wick)
       SELL: mirror image

  These fire in SEQUENCE, not on one bar: %K leaves oversold first, RSI crosses
  55 some bars later, then price reclaims the EMAs. So a cross counts if it
  happened within the last W bars, and W is swept (1 = strict same-bar).

  Stop = ATR x 1.5. Target = R:R multiple of that. Costs identical to every
  other backtest here: 0.04%/side fee, 0.05% slippage on stop exits.

  cd /root/hpuaung/trading_bot && .venv/bin/python user_strategy_test.py
  .venv/bin/python user_strategy_test.py 15m,30m,1h,4h,6h,12h
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

import database as db
from utils import binance_client as bc
from utils import indicators as ind

db.init_db()

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in
              (_a[0] if len(_a) > 0 else "15m,30m,1h,4h,6h,12h").split(",") if x.strip()]

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

RRS = [1.0, 1.5, 2.0, 3.0]
WINDOWS = [1, 5, 10, 20]      # confirmation windows to sweep
FEE = 0.0004
SLIP = 0.0005
WARMUP = 210
MAXHOLD = 200
SL_ATR_MULT = 1.5
SLOPE_LB = 5
SLOPE_MIN = 0.10
MIN_TRADES = 30               # below this a result is not worth reporting

CANDLES = {"15m": 5000, "30m": 5000, "1h": 4000, "2h": 3000,
           "4h": 2000, "6h": 1500, "8h": 1500, "12h": 1500, "1d": 1500}
TF_HOURS = {"15m": .25, "30m": .5, "1h": 1, "2h": 2, "4h": 4,
            "6h": 6, "8h": 8, "12h": 12, "1d": 24}


def prepare(df):
    """Add ema9 + every raw condition. Cross columns stay RAW (single-bar) so the
    confirmation window can be applied cheaply afterwards."""
    d = ind.compute_indicators(df)
    close = d["close"].astype("float64")
    d["ema9"] = close.ewm(span=9, adjust=False).mean()
    atr = d["atr"].replace(0, np.nan)

    d["stack_bull"] = (d["ema9"] > d["ema21"]) & (d["ema21"] > d["ema200"])
    d["stack_bear"] = (d["ema9"] < d["ema21"]) & (d["ema21"] < d["ema200"])

    for name in ("ema9", "ema21", "ema200"):
        d[f"sl_{name}"] = (d[name] - d[name].shift(SLOPE_LB)) / (SLOPE_LB * atr)
    d["slope_bull"] = ((d["sl_ema9"] >= SLOPE_MIN) & (d["sl_ema21"] >= SLOPE_MIN)
                       & (d["sl_ema200"] > 0))
    d["slope_bear"] = ((d["sl_ema9"] <= -SLOPE_MIN) & (d["sl_ema21"] <= -SLOPE_MIN)
                       & (d["sl_ema200"] < 0))

    rsi, rsi_p = d["rsi"], d["rsi"].shift(1)
    d["x_rsi_up"] = (rsi > 55) & (rsi_p <= 55)
    d["x_rsi_dn"] = (rsi < 45) & (rsi_p >= 45)

    k, k_p = d["stoch_k"], d["stoch_k"].shift(1)
    d["x_st_up"] = (k > 20) & (k_p <= 20)
    d["x_st_dn"] = (k < 80) & (k_p >= 80)

    rng = (d["high"] - d["low"]).replace(0, np.nan)
    d["pa_bull"] = ((close > d["ema9"]) & (close > d["ema21"])
                    & (close > d["open"]) & (((close - d["low"]) / rng) > 0.5))
    d["pa_bear"] = ((close < d["ema9"]) & (close < d["ema21"])
                    & (close < d["open"]) & (((d["high"] - close) / rng) > 0.5))
    return d


def _win(col, w):
    """True if the raw cross fired within the last w bars (inclusive)."""
    return col.fillna(False).rolling(max(1, w), min_periods=1).max().astype(bool)


def signals(d, w, use_slope=True, use_rsi=True, use_stoch=True, use_pa=True):
    buy = d["stack_bull"].fillna(False).copy()
    sell = d["stack_bear"].fillna(False).copy()
    if use_slope:
        buy &= d["slope_bull"].fillna(False); sell &= d["slope_bear"].fillna(False)
    if use_rsi:
        buy &= _win(d["x_rsi_up"], w); sell &= _win(d["x_rsi_dn"], w)
    if use_stoch:
        buy &= _win(d["x_st_up"], w); sell &= _win(d["x_st_dn"], w)
    if use_pa:
        buy &= d["pa_bull"].fillna(False); sell &= d["pa_bear"].fillna(False)
    return buy.to_numpy(), sell.to_numpy()


def simulate(d, buy, sell, rr):
    hi = d["high"].astype("float64").to_numpy()
    lo = d["low"].astype("float64").to_numpy()
    c = d["close"].astype("float64").to_numpy()
    atr = d["atr"].astype("float64").to_numpy()
    n = len(c)
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
                out.append((gross - fee) / pos["risk"])
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


def stat(t, span_days):
    if not t:
        return None
    w = [x for x in t if x > 0]
    gW = sum(w)
    gL = -sum(x for x in t if x <= 0)
    return dict(n=len(t), win=100 * len(w) // len(t), exp=sum(t) / len(t),
                pf=(gW / gL if gL > 0 else 99.0),
                per30=(len(t) / span_days * 30 if span_days > 0 else 0))


if __name__ == "__main__":
    print("=" * 86)
    print("YOUR STRATEGY — EMA(9/21/200)+slope · RSI 55/45 cross · Stoch 20/80 cross")
    print("                + clean close beyond EMA9/21 · SL = 1.5xATR")
    print(f"pairs={len(PAIRS)}   slope>={SLOPE_MIN} ATR/bar   windows tested={WINDOWS}")
    print("=" * 86)

    overall_best = []

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
            print(f"==== {tf}: no data ====")
            continue

        kind = "SCALP" if TF_HOURS.get(tf, 1) < 4 else "SWING"
        print(f"\n{'='*86}\n{tf}  [{kind}]   ~{span_days:.0f} days history, {len(prepared)} pairs\n{'='*86}")

        # --- A. where do the rules collapse? progressive AND at the widest window
        wmax = max(WINDOWS)
        counts = []
        for label, kw in (("EMA stack", dict(use_slope=False, use_rsi=False, use_stoch=False, use_pa=False)),
                          ("+ slope", dict(use_rsi=False, use_stoch=False, use_pa=False)),
                          ("+ RSI cross", dict(use_stoch=False, use_pa=False)),
                          ("+ Stoch cross", dict(use_pa=False)),
                          ("+ price action (FULL)", dict())):
            tot = 0
            for d in prepared:
                b, s = signals(d, wmax, **kw)
                tot += int(b.sum() + s.sum())
            counts.append((label, tot))
        print(f"  signal bars, adding one rule at a time (window={wmax}):")
        for label, tot in counts:
            print(f"    {label:24} {tot:>7}")
        if counts[-1][1] == 0:
            print("    ⚠️  the FULL rule set never triggers on this timeframe.")

        # --- B. how many entries at each confirmation window
        print(f"  entries by confirmation window:")
        win_counts = {}
        for w in WINDOWS:
            tot = 0
            for d in prepared:
                b, s = signals(d, w)
                tot += int(b.sum() + s.sum())
            win_counts[w] = tot
            print(f"    W={w:<3} {tot:>7} signal bars")

        # --- C. R:R sweep at the smallest window that produces enough trades
        usable = [w for w in WINDOWS if win_counts[w] >= MIN_TRADES]
        if not usable:
            print(f"  → fewer than {MIN_TRADES} signals at every window; nothing to measure.")
            continue
        w_use = usable[0]
        print(f"\n  R:R sweep at W={w_use}:")
        print(f"  {'R:R':>6}{'n':>7}{'win%':>6}{'expR':>9}{'PF':>7}{'entries/30d':>13}")
        for rr in RRS:
            t = []
            for d in prepared:
                b, s = signals(d, w_use)
                t += simulate(d, b, s, rr)
            st = stat(t, span_days)
            if not st:
                print(f"  {'1:'+f'{rr:g}':>6}{'0':>7}   no trades")
                continue
            edge = st["exp"] > 0 and st["pf"] > 1.3 and st["n"] >= MIN_TRADES
            print(f"  {'1:'+f'{rr:g}':>6}{st['n']:>7}{st['win']:>6}{st['exp']:>+9.3f}"
                  f"{st['pf']:>7.2f}{st['per30']:>13.1f}" + ("  EDGE" if edge else ""))
            if edge:
                overall_best.append((tf, rr, w_use, st))

        # --- D. ablation: which rule earns its place
        print(f"\n  ablation @ R:R 1:2, W={w_use} — which rule helps?")
        print(f"  {'variant':26}{'n':>7}{'win%':>6}{'expR':>9}{'PF':>7}")
        for label, kw in (("ALL rules (as specified)", dict()),
                          ("without slope", dict(use_slope=False)),
                          ("without RSI cross", dict(use_rsi=False)),
                          ("without Stoch cross", dict(use_stoch=False)),
                          ("without price action", dict(use_pa=False)),
                          ("EMA stack ONLY", dict(use_slope=False, use_rsi=False,
                                                  use_stoch=False, use_pa=False))):
            t = []
            for d in prepared:
                b, s = signals(d, w_use, **kw)
                t += simulate(d, b, s, 2.0)
            st = stat(t, span_days)
            if not st:
                print(f"  {label:26}{'0':>7}   no trades")
                continue
            print(f"  {label:26}{st['n']:>7}{st['win']:>6}{st['exp']:>+9.3f}{st['pf']:>7.2f}")

    print("\n" + "=" * 86)
    print("RANKED — combos clearing expR>0, PF>1.3, n>=30")
    print("=" * 86)
    if not overall_best:
        print("  NONE — on this data the system does not clear the edge bar at any")
        print("  timeframe or R:R tested. The per-timeframe tables above show whether")
        print("  that is because it never triggers, or because it triggers and loses.")
    else:
        print(f"{'timeframe':11}{'R:R':>6}{'W':>4}{'PF':>7}{'expR':>9}{'n':>7}{'entries/30d':>13}")
        for tf, rr, w, st in sorted(overall_best, key=lambda x: -x[3]["pf"]):
            print(f"{tf:11}{'1:'+f'{rr:g}':>6}{w:>4}{st['pf']:>7.2f}{st['exp']:>+9.3f}"
                  f"{st['n']:>7}{st['per30']:>13.1f}")

    print("\n" + "=" * 86)
    print("HOW TO READ")
    print("=" * 86)
    print("• 'signal bars, adding one rule at a time' shows WHERE the system dies: if")
    print("  the count collapses to 0 when a rule is added, that rule contradicts the")
    print("  others (e.g. Stoch crossing UP through 20 happens at a LOW, while the")
    print("  price-action rule demands a close ABOVE the EMAs — opposite moments).")
    print("• In the ablation, if removing a rule RAISES PF, that rule costs money.")
    print("• Benchmark, same pairs/fees/slippage: trend-12h 1:3 PF ~1.54,")
    print("  trend-6h 1:3 PF ~1.32. Beat that and it is worth switching to.")
