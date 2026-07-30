#!/usr/bin/env python3
"""maker_backtest.py — does switching from TAKER (market) entries to MAKER
(limit / post-only) entries actually flip our thin edge to positive? Prove it
BEFORE building it into the live bot.

Built directly on top of swing_sweep.py so the TAKER column reproduces the
numbers you already trust (same signals, same pairs, same SL/TP/exit logic).
For every (timeframe, strategy, R:R) it prints THREE scenarios:

  T  (taker)      = today's bot: market entry, taker fee 0.04% both sides.
  Mc (maker-ceil) = maker fee, but assume every limit fills. UPPER BOUND of the
                    fee benefit — if even this doesn't lift PF, maker cannot help.
  Mr (maker-real) = maker fee + a fill-window model: the limit only fills if
                    price REVISITS the entry within W candles; otherwise the
                    trade is MISSED. Captures the two real costs of maker orders:
                    missed trades + adverse selection. Reports fill% too.

Honest fee model
  taker : entry 0.04% + exit 0.04%              (both market/taker)
  maker : entry 0.02% (limit) + exit: TP is a resting limit -> 0.02% (maker),
          but SL (stop-market) and time-exit -> 0.04% (taker). No entry slip on
          a limit fill; SL still slips like a real stop-market.

Fill window (W): a limit placed after signal candle i rests over candles
i+1..i+W. BUY fills if some candle's low <= entry; SELL if high >= entry. While
a limit is resting (or a position is open) the slot is BUSY — new signals are
ignored, exactly like the real one-slot-per-strategy engine. An unfilled limit
ties the slot up for its whole window, then frees it (counted as a MISS).

Usage:
  cd /root/hpuaung/trading_bot && .venv/bin/python maker_backtest.py
  .venv/bin/python maker_backtest.py 6h,12h,1d 1500 0.0002
     args: timeframes   candles/pair   maker_fee_per_side
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()

# --- neutralise reversion scalping tuning in-process (same as swing_sweep) so
#     we measure the BASE strategies uniformly; rr= controls R:R cleanly. ---
_orig_get_float = db.get_float
def _patched_get_float(key, default=0.0):
    if key in ("reversion_rsi_extreme", "reversion_fixed_rr"):
        return 0.0
    return _orig_get_float(key, default)
db.get_float = _patched_get_float

_a = sys.argv[1:]
TIMEFRAMES = [x.strip() for x in (_a[0] if len(_a) > 0 else
              "6h,8h,12h,1d").split(",") if x.strip()]
LIMIT = int(_a[1]) if len(_a) > 1 else 1500
MAKER_FEE = float(_a[2]) if len(_a) > 2 else 0.0002   # 0.02%/side (Binance base)

PAIRS = [p.strip() for p in db.get_setting(
    "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]

TAKER_FEE = 0.0004     # 0.04%/side — matches the live bot + every other backtest
SLIP = 0.0005          # stop-market slippage, applied to SL exits only
WARMUP = 210
MAXHOLD = 200
FILL_WINDOWS = [1, 2, 3]   # maker-real fill windows to report (candles)
STRATS = ["trend", "reversion", "breakout", "hybrid"]
RRS = [None, 2.0, 3.0]     # native, then forced 1:2 and 1:3
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


def collect_signals(df):
    """One pass over candles → per-strategy list of raw signals {i,d,entry,sl}.
    Signal generation is IDENTICAL to swing_sweep (windowed subframe, same calls)
    so the taker scenario reproduces the trusted numbers. Simulation of
    fill/exit is done separately per scenario, cheaply, from these signals."""
    sigs = {s: [] for s in STRATS}
    n = len(df["close"])
    for i in range(WARMUP, n):
        sub = df.iloc[i - WARMUP:i + 1]
        if not ind.has_enough(sub):
            continue
        tr = trend.run(sub, mtf=False)
        rv = reversion.run(sub, mtf=False)
        bk = breakout.run(sub, mtf=False)
        hy = ai_hybrid.run(sub, tr, rv, bk, ai_threshold=0.60, use_model=False)
        raw = {"trend": tr, "reversion": rv, "breakout": bk, "hybrid": hy}
        for s in STRATS:
            r = _valid(raw[s])
            if r:
                d = 1 if r["signal"] == "BUY" else -1
                sigs[s].append({"i": i, "d": d,
                                "entry": float(r["entry"]), "sl": float(r["sl"]),
                                # keep the strategy's own tp1 so native R:R matches
                                # swing_sweep exactly (no hardcoded fallback).
                                "tp": float(r["tp1"])})
    return sigs


def _simulate_exit(k, d, entry, tp, sl, hi, lo, c, n):
    """From ENTRY candle k, walk forward; return (exit_price, etype, exit_candle)
    or None if still open at the end. SL is checked before TP within a candle
    (conservative). Exits begin the candle AFTER entry — no intrabar lookahead."""
    for j in range(k + 1, n):
        ex = None
        etype = None
        if d > 0:
            if lo[j] <= sl:
                ex, etype = sl * (1 - SLIP), "sl"
            elif hi[j] >= tp:
                ex, etype = tp, "tp"
        else:
            if hi[j] >= sl:
                ex, etype = sl * (1 + SLIP), "sl"
            elif lo[j] <= tp:
                ex, etype = tp, "tp"
        if ex is None and (j - k) >= MAXHOLD:
            ex, etype = c[j], "time"
        if ex is not None:
            return ex, etype, j
    return None


def _fee_R(entry, ex, etype, scenario, risk):
    """Round-trip fee in R units. taker = 0.04% both sides. maker = 0.02% entry;
    exit is maker only when it's the resting TP limit, else taker."""
    if scenario == "taker":
        f = (entry + ex) * TAKER_FEE
    else:
        exit_fee = MAKER_FEE if etype == "tp" else TAKER_FEE
        f = entry * MAKER_FEE + ex * exit_fee
    return f / risk if risk > 0 else 0.0


def sim_taker(signals, rr, hi, lo, c, n, maker_fees=False):
    """Market entry on the signal candle — always fills. One slot: signals that
    fire while a position is open are ignored. maker_fees=True → the 'ceiling'
    scenario (same trades, cheaper fees)."""
    scen = "maker" if maker_fees else "taker"
    out = []
    busy_until = -1
    for sg in signals:
        i = sg["i"]
        if i < busy_until:          # still in a trade (can re-enter ON the exit candle)
            continue
        e, sl, d = sg["entry"], sg["sl"], sg["d"]
        risk = abs(e - sl)
        tp = e + d * rr * risk if rr is not None else None
        if tp is None:              # native: recover TP from the signal's own tp1
            tp = sg.get("tp", e + d * 2 * risk)
        res = _simulate_exit(i, d, e, tp, sl, hi, lo, c, n)
        if res is None:
            break                   # open to the end → no further trades
        ex, etype, j = res
        gross = (ex - e) * d / risk
        out.append(gross - _fee_R(e, ex, etype, scen, risk))
        busy_until = j
    return out


def sim_maker_real(signals, rr, W, hi, lo, c, n):
    """Limit entry at the signal price. It rests over candles i+1..i+W and fills
    only if price revisits it. Unfilled → MISS (slot tied up for the window, then
    freed). Filled → run the trade from the fill candle with maker fees."""
    out = []
    filled = 0
    missed = 0
    free_from = -1
    for sg in signals:
        i = sg["i"]
        if i < free_from:           # slot busy (resting limit or open position)
            continue
        e, sl, d = sg["entry"], sg["sl"], sg["d"]
        risk = abs(e - sl)
        fill_candle = None
        for j in range(i + 1, min(i + W, n - 1) + 1):
            if (d > 0 and lo[j] <= e) or (d < 0 and hi[j] >= e):
                fill_candle = j
                break
        if fill_candle is None:
            missed += 1
            free_from = i + W + 1    # limit occupied the slot for its window
            continue
        filled += 1
        tp = e + d * rr * risk if rr is not None else sg.get("tp", e + d * 2 * risk)
        res = _simulate_exit(fill_candle, d, e, tp, sl, hi, lo, c, n)
        if res is None:
            free_from = n
            break
        ex, etype, jx = res
        gross = (ex - e) * d / risk
        out.append(gross - _fee_R(e, ex, etype, "maker", risk))
        free_from = jx
    attempts = filled + missed
    fill_rate = (100.0 * filled / attempts) if attempts else 0.0
    return out, fill_rate, filled, missed


def stat(t, span_days):
    if not t:
        return None
    w = [x for x in t if x > 0]
    gW = sum(w)
    gL = -sum(x for x in t if x <= 0)
    pf = gW / gL if gL > 0 else 99.0
    exp = sum(t) / len(t)
    per30 = (len(t) / span_days * 30) if span_days > 0 else 0
    return dict(n=len(t), win=100 * len(w) // len(t), exp=exp, pf=pf, per30=per30)


# ---------------------------------------------------------------------------
print("=" * 92)
print("MAKER vs TAKER BACKTEST — does a maker (limit) entry flip the edge positive?")
print(f"pairs={len(PAIRS)}  candles/pair<= {LIMIT}  taker={TAKER_FEE*100:.3f}%/side  "
      f"maker={MAKER_FEE*100:.3f}%/side  fill-windows W={FILL_WINDOWS}")
print("T=taker(today)  Mc=maker-ceiling(100% fill)  Mr=maker-real(fill-window)")
print("=" * 92)

verdicts = []   # (tf, strat, rr, taker_pf, best_maker_pf, best_fill, best_per30)

for tf in TIMEFRAMES:
    sig_all = {s: [] for s in STRATS}   # signals aggregated across pairs, offset per pair
    bars = {s: {} for s in STRATS}      # not used; kept per-pair below
    per_pair = []                       # (signals dict, hi, lo, c, n)
    max_candles = 0
    print(f"\n[progress] {tf}: scanning {len(PAIRS)} pairs ...", flush=True)
    for pi, sym in enumerate(PAIRS, 1):
        try:
            df = bc.get_ohlcv_deep(sym, tf, LIMIT, api_mode="real")
            if df is None or len(df) < WARMUP + 30:
                print(f"[progress]   {tf} {pi}/{len(PAIRS)} {sym}: skip (thin)", flush=True)
                continue
            max_candles = max(max_candles, len(df))
            df = ind.compute_indicators(df)
            hi = df["high"].astype(float).tolist()
            lo = df["low"].astype(float).tolist()
            c = df["close"].astype(float).tolist()
            sigs = collect_signals(df)
            per_pair.append((sigs, hi, lo, c, len(c)))
            print(f"[progress]   {tf} {pi}/{len(PAIRS)} {sym}: {len(df)} candles, "
                  + ", ".join(f"{s}:{len(sigs[s])}" for s in STRATS), flush=True)
        except Exception as _e:  # noqa: BLE001
            print(f"[progress]   {tf} {pi}/{len(PAIRS)} {sym}: err {str(_e)[:30]}", flush=True)
            continue

    span_days = max_candles * TF_HOURS.get(tf, 1) / 24.0
    print(f"\n==== {tf}  (~{max_candles} candles/pair ≈ {span_days:.0f} days history) ====")

    for s in STRATS:
        for rr in RRS:
            rrlab = "nat" if rr is None else f"1:{rr:g}"
            # aggregate each scenario's per-trade R across all pairs
            taker_R, ceil_R = [], []
            real = {W: ([], []) for W in FILL_WINDOWS}   # W -> (R list, [fills,misses])
            for sigs, hi, lo, c, n in per_pair:
                sg = sigs[s]
                taker_R += sim_taker(sg, rr, hi, lo, c, n, maker_fees=False)
                ceil_R += sim_taker(sg, rr, hi, lo, c, n, maker_fees=True)
                for W in FILL_WINDOWS:
                    rlist, frate, f, m = sim_maker_real(sg, rr, W, hi, lo, c, n)
                    real[W][0].extend(rlist)
                    real[W][1].append((f, m))

            st_t = stat(taker_R, span_days)
            st_c = stat(ceil_R, span_days)
            if not st_t:
                continue
            # header line: taker baseline
            print(f"\n{s} {rrlab}")
            print(f"   T   n={st_t['n']:<4} win={st_t['win']:>2}%  expR={st_t['exp']:+.3f}"
                  f"  PF={st_t['pf']:.2f}  /30d={st_t['per30']:.1f}")
            if st_c:
                print(f"   Mc  n={st_c['n']:<4} win={st_c['win']:>2}%  expR={st_c['exp']:+.3f}"
                      f"  PF={st_c['pf']:.2f}  /30d={st_c['per30']:.1f}"
                      f"   (ceiling: maker fee, 100% fill)")
            best_pf, best_fill, best_per30 = 0.0, 0.0, 0.0
            for W in FILL_WINDOWS:
                rlist, fm = real[W]
                tot_f = sum(x[0] for x in fm)
                tot_m = sum(x[1] for x in fm)
                frate = (100.0 * tot_f / (tot_f + tot_m)) if (tot_f + tot_m) else 0.0
                st_r = stat(rlist, span_days)
                if not st_r:
                    print(f"   MrW{W} fill={frate:4.0f}%  (no filled trades)")
                    continue
                print(f"   MrW{W} fill={frate:4.0f}%  n={st_r['n']:<4} win={st_r['win']:>2}%"
                      f"  expR={st_r['exp']:+.3f}  PF={st_r['pf']:.2f}  /30d={st_r['per30']:.1f}")
                if st_r["pf"] > best_pf and st_r["n"] >= 20:
                    best_pf, best_fill, best_per30 = st_r["pf"], frate, st_r["per30"]
            verdicts.append((tf, s, rrlab, st_t["pf"], st_c["pf"] if st_c else 0.0,
                             best_pf, best_fill, best_per30, st_t["per30"]))

# ---------------------------------------------------------------------------
print("\n" + "=" * 92)
print("VERDICT — where does MAKER-REAL beat TAKER on PF while still trading enough?")
print("(taker_PF -> best maker-real_PF ; fill% ; maker /30d vs taker /30d)")
print("=" * 92)
helped = [v for v in verdicts if v[5] > v[3] and v[5] >= 1.0]
if not helped:
    print("  NONE — no combo where a realistic maker fill beats taker AND clears PF>=1.0.")
    print("  => maker fees alone do not save the edge; the honest read is 'change the")
    print("     game or stop', not 'switch to limit orders'. Check Mc (ceiling) rows:")
    print("     if even the ceiling PF is <=1.0, the strategy — not the fee — is the problem.")
else:
    print(f"{'tf':6}{'strategy':10}{'R:R':>5}{'takerPF':>9}{'makerPF':>9}"
          f"{'fill%':>7}{'mk/30d':>8}{'tk/30d':>8}")
    for tf, s, rrlab, tpf, cpf, mpf, mfill, mper, tper in sorted(
            helped, key=lambda v: -(v[5] - v[3])):
        print(f"{tf:6}{s:10}{rrlab:>5}{tpf:>9.2f}{mpf:>9.2f}"
              f"{mfill:>6.0f}%{mper:>8.1f}{tper:>8.1f}")
print("\nHow to read: pick a row where maker PF clearly beats taker PF AND fill% is")
print("high enough that /30d stays usable. That combo is where building maker/limit")
print("entries is worth the code. If the table is empty, don't build it — the fee was")
print("never the real problem.")
