#!/usr/bin/env python3
"""backtest.py — replay the bot's entry strategies over historical candles to
measure edge BEFORE deploying, and compare entry-quality ideas side by side.

It fetches real Binance klines, computes the SAME indicators and runs the SAME
strategy code (trend/reversion/breakout + ai_hybrid) the live bot uses. Exits
use a clean, fixed R:R (SL = sl_mult x ATR, TP = rr x SL) so the ONLY thing that
varies between configs is the ENTRY filter — that isolates entry edge.

Configs compared:
  baseline           - current bot entry (no extra filter)
  +confirm           - require a follow-through candle in the signal direction
  +adx>25            - only enter when ADX >= 25 (trending, not chop)
  +volume>1.2x       - only enter when candle volume >= 1.2x its 20-bar average
  +ALL               - confirm AND adx AND volume together

Usage (on the VPS, in trading_bot/):
  .venv/bin/python backtest.py
  .venv/bin/python backtest.py BTCUSDT,ETHUSDT,SOLUSDT 5m 1500
  .venv/bin/python backtest.py all 5m 1500 2.0      # pairs interval candles rr

Read the OUTPUT: a config is worth deploying only if win% clears the break-even
line for the R:R (at 2:1 that is ~33%) with a positive expectancy (avg R > 0).
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import trend, reversion, breakout, ai_hybrid

db.init_db()

# ---- args -----------------------------------------------------------------
_args = sys.argv[1:]
pairs_arg = _args[0] if len(_args) > 0 else "all"
INTERVAL = _args[1] if len(_args) > 1 else "5m"
LIMIT = int(_args[2]) if len(_args) > 2 else 1500
RR = float(_args[3]) if len(_args) > 3 else 2.0
SL_MULT = float(_args[4]) if len(_args) > 4 else 2.5

if pairs_arg == "all":
    PAIRS = [p.strip() for p in db.get_setting(
        "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004          # taker fee per side
SLIP = 0.0005         # adverse slippage on stop fills
WARMUP = 210
MAXHOLD = 120         # force-close after this many candles (avoid infinite hold)
AI_THRESHOLD = 0.60
USE_MODEL = db.get_bool("ai_model_on", True)


def _signal(sub):
    """Run the bot's hybrid entry on a candle window; return ('BUY'/'SELL'/None)."""
    t = trend.run(sub, mtf=False)
    r = reversion.run(sub, mtf=False)
    b = breakout.run(sub, mtf=False)
    final = ai_hybrid.run(sub, t, r, b, ai_threshold=AI_THRESHOLD, use_model=USE_MODEL)
    s = final.get("signal", "NONE")
    return s if s in ("BUY", "SELL") else None


def _precompute(df):
    """Per candle: raw signal + the metrics the entry filters need (computed once)."""
    n = len(df)
    o = df["open"].astype(float).tolist()
    c = df["close"].astype(float).tolist()
    v = df["volume"].astype(float).tolist()
    adx = df["adx"].fillna(0).astype(float).tolist() if "adx" in df else [0.0]*n
    atr = df["atr"].fillna(0).astype(float).tolist() if "atr" in df else [0.0]*n
    hi = df["high"].astype(float).tolist()
    lo = df["low"].astype(float).tolist()
    sig = [None]*n
    volr = [0.0]*n
    for i in range(WARMUP, n):
        sub = df.iloc[:i+1]
        if not ind.has_enough(sub):
            continue
        sig[i] = _signal(sub)
        vbase = sum(v[i-20:i]) / 20.0 if i >= 20 else (v[i] or 1)
        volr[i] = (v[i] / vbase) if vbase > 0 else 0.0
    return dict(o=o, c=c, v=v, adx=adx, atr=atr, hi=hi, lo=lo, sig=sig, volr=volr, n=n)


def _passes(cfg, d, i):
    s = d["sig"][i]
    if cfg.get("confirm"):
        # follow-through candle: green for BUY / red for SELL and beyond prev close
        if s == "BUY" and not (d["c"][i] > d["o"][i] and d["c"][i] > d["c"][i-1]):
            return False
        if s == "SELL" and not (d["c"][i] < d["o"][i] and d["c"][i] < d["c"][i-1]):
            return False
    if cfg.get("adx") and d["adx"][i] < cfg["adx"]:
        return False
    if cfg.get("vol") and d["volr"][i] < cfg["vol"]:
        return False
    return True


def _simulate(cfg, d):
    """Walk candles, open on filtered signals, exit on clean fixed-R:R SL/TP."""
    trades = []
    pos = None
    for i in range(WARMUP, d["n"]):
        if pos:
            held = i - pos["i"]
            hit = None
            if pos["side"] == "BUY":
                if d["lo"][i] <= pos["sl"]:
                    hit = ("SL", pos["sl"] * (1 - SLIP))
                elif d["hi"][i] >= pos["tp"]:
                    hit = ("TP", pos["tp"])
            else:
                if d["hi"][i] >= pos["sl"]:
                    hit = ("SL", pos["sl"] * (1 + SLIP))
                elif d["lo"][i] <= pos["tp"]:
                    hit = ("TP", pos["tp"])
            if not hit and held >= MAXHOLD:
                hit = ("Time", d["c"][i])
            if hit:
                reason, px = hit
                e = pos["entry"]; dirn = 1 if pos["side"] == "BUY" else -1
                gross = (px - e) * dirn
                fee = (e + px) * FEE
                r = (gross - fee) / pos["risk"] if pos["risk"] > 0 else 0
                trades.append({"side": pos["side"], "reason": reason, "R": r})
                pos = None
        if not pos and d["sig"][i] and _passes(cfg, d, i):
            atr = d["atr"][i]
            if atr <= 0:
                continue
            e = d["c"][i]; side = d["sig"][i]
            risk = SL_MULT * atr
            if side == "BUY":
                sl = e - risk; tp = e + RR * risk
            else:
                sl = e + risk; tp = e - RR * risk
            pos = {"i": i, "side": side, "entry": e, "sl": sl, "tp": tp, "risk": risk}
    return trades


def _report(name, trades):
    n = len(trades)
    if n == 0:
        print(f"{name:14} no trades")
        return
    wins = [t for t in trades if t["R"] > 0]
    R = sum(t["R"] for t in trades)
    grossW = sum(t["R"] for t in wins)
    grossL = -sum(t["R"] for t in trades if t["R"] <= 0)
    pf = grossW / grossL if grossL > 0 else float("inf")
    print(f"{name:14} n={n:<4} win%={100*len(wins)/n:4.0f}  "
          f"expectancy={R/n:+.3f}R  totalR={R:+6.1f}  PF={pf:.2f}")


CONFIGS = [
    ("baseline", {}),
    ("+vol1.2x", {"vol": 1.2}),
    ("+vol1.5x", {"vol": 1.5}),
    ("+vol2.0x", {"vol": 2.0}),
    ("+vol2.5x", {"vol": 2.5}),
    ("+vol2+confirm", {"vol": 2.0, "confirm": True}),
]

print("=" * 70)
print(f"BACKTEST  pairs={len(PAIRS)} interval={INTERVAL} candles={LIMIT} "
      f"R:R=1:{RR} SL={SL_MULT}xATR model={'ON' if USE_MODEL else 'OFF'}")
print(f"break-even win% at 1:{RR} ≈ {100/(1+RR):.0f}%  (need win% above this + expectancy>0)")
print("=" * 70)

alld = {}
for sym in PAIRS:
    try:
        df = bc.get_ohlcv(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < WARMUP + 50:
            print(f"  {sym}: not enough data ({0 if df is None else len(df)})")
            continue
        df = ind.compute_indicators(df)
        alld[sym] = _precompute(df)
        print(f"  {sym}: {len(df)} candles, "
              f"{sum(1 for s in alld[sym]['sig'] if s)} raw signals")
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: ERROR {e}")

print("-" * 70)
for name, cfg in CONFIGS:
    trades = []
    for sym, d in alld.items():
        trades += _simulate(cfg, d)
    _report(name, trades)
print("-" * 70)
print("READ: pick the config with win% well above break-even AND expectancy > 0.")
print("If NONE clear it, the entry signal has no edge — that is the real answer.")
