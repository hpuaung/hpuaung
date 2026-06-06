#!/usr/bin/env python3
"""check_signals.py — scan every selected pair and show why it is / isn't entering.

Run on the VPS:  cd /root/hpuaung/trading_bot && .venv/bin/python check_signals.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
from strategies import trend, reversion, breakout, ai_hybrid
import engine

pairs = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
print("pairs:", pairs)
print("scalp AI threshold:", db.get_float("scalping_ai_threshold", 0.5),
      "| swing:", db.get_float("swing_ai_threshold", 0.5))
print("%-9s %-8s %-5s %-5s %-5s %-5s | %s" % ("PAIR", "ENGINE", "trend", "rev", "brk", "AI", "context"))

for sym in pairs:
    for strat in ("scalping", "swing"):
        try:
            mtf = db.get_bool(f"{strat}_mtf_filter", True)
            e, cf, tr = engine._gather_indicator_frames(sym, strat, "test")
            t = trend.run(e, cf, tr, mtf)["signal"]
            rv = reversion.run(e, cf)["signal"]
            bk = breakout.run(e)["signal"]
            a = ai_hybrid.run(e, trend.run(e, cf, tr, mtf), reversion.run(e, cf),
                              breakout.run(e), ai_threshold=db.get_float(f"{strat}_ai_threshold", 0.5))
            l = e.iloc[-1]
            aligned = bool(l.ema21 > l.ema50 > l.ema200) or bool(l.ema21 < l.ema50 < l.ema200)
            ctx = "rsi=%.0f adx=%.0f emaAligned=%s" % (l.rsi, l.adx, aligned)
            print("%-9s %-8s %-5s %-5s %-5s %-5s | %s" % (sym, strat, t, rv, bk, a["signal"], ctx))
        except Exception as ex:  # noqa: BLE001
            print("%-9s %-8s ERR %s" % (sym, strat, str(ex)[:50]))
