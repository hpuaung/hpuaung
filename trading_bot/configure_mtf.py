#!/usr/bin/env python3
"""configure_mtf.py — user-requested MULTI-TIMEFRAME config (paper).

  SWING     : entry 1h  · confirm 4h  · HTF-trend 1d
  SCALPING  : entry 3m  · confirm 15m · HTF-trend 1h
  strategies: breakout + trend (reversion/hybrid off)
  pairs     : all 10 majors

NOTE (honest): single-timeframe backtests showed breakout at 1h/4h and trend at
low TFs are NEGATIVE on their own. This config bets that the 4h/1d (swing) and
15m/1h (scalp) MTF *confirm + trend filter* only lets aligned entries through and
turns that around. That is UNPROVEN — this is a paper experiment. Watch results.

Run on the VPS:  .venv/bin/python configure_mtf.py   (then restart futures-engine)
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

PAIRS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,"
         "DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,LTCUSDT")

CONFIG = {
    # ---- SWING: 1h entry / 4h confirm / 1d trend ----
    "swing_bot_on": "1",
    "swing_mode": "paper",
    "swing_auto_tf": "0",          # off so our custom TFs are used
    "swing_timeframe": "1h",
    "swing_confirm_tf": "4h",
    "swing_trend_tf": "1d",
    "swing_mtf_filter": "1",       # ON: require confirm+trend alignment
    "swing_breakout_on": "1",
    "swing_trend_on": "1",
    "swing_reversion_on": "0",
    "swing_hybrid_on": "0",
    "swing_auto_tpsl": "1",
    "swing_partial_tp": "1",

    # ---- SCALPING: 3m entry / 15m confirm / 1h trend ----
    "scalping_bot_on": "1",
    "scalping_mode": "paper",
    "scalping_auto_tf": "0",
    "scalping_timeframe": "3m",
    "scalping_confirm_tf": "15m",
    "scalping_trend_tf": "1h",
    "scalping_mtf_filter": "1",
    "scalping_breakout_on": "1",
    "scalping_trend_on": "1",
    "scalping_reversion_on": "0",
    "scalping_hybrid_on": "0",
    "scalping_auto_tpsl": "1",
    "scalping_partial_tp": "1",

    # ---- shared: keep breakout's tight SL usable, no extra filters/model ----
    "atr_sl_enabled": "0",
    "min_rr_ratio": "0.3",
    "min_tp_pct": "0.0",
    "ai_model_on": "0",
    "auto_pilot": "0",
    "global_auto_risk": "0",
    "max_concurrent_trades": "6",
    "selected_pairs": PAIRS,
}

# turn off the per-section entry filters so signals actually reach execution
for s in ("swing", "scalping"):
    for f in ("session_filter", "win_filter", "dir_filter", "hour_filter",
              "session_pair_filter"):
        CONFIG[f"{s}_{f}"] = "0"
    CONFIG[f"{s}_min_lgbm"] = "0"

print("=== CONFIGURING: multi-timeframe swing + scalping (paper) ===")
for k, v in CONFIG.items():
    db.save_setting(k, v)
    print(f"  {k:26} = {db.get_setting(k)}")
print("=== DONE.  restart the engine to apply. ===")
