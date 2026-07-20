#!/usr/bin/env python3
"""configure_scalp.py — deploy the best scalping config we found to the SCALPING
engine (PAPER ONLY), so it runs alongside the swing 1d breakout and gives the
activity swing can't. Config: REVERSION with the RSI-extreme gate (RSI<20 buy /
>80 sell) + a fixed R:R 1:3 exit, on 30m.

⚠️ HONEST: in backtest this cleared the edge bar (PF 1.31-1.34), BUT the
walk-forward showed the edge DECAYED in the most recent third (15m PF 0.80, 30m
PF 1.07) — mean-reversion dies in trending regimes. So this is a PAPER EXPERIMENT
to get activity + live confirmation, NOT a proven money-maker. Watch it; if it
keeps losing live, turn scalping back off. Swing 1d is the robust edge.

Run on the VPS:  .venv/bin/python configure_scalp.py   (then restart futures-engine)
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

CONFIG = {
    # scalping engine ON (paper), swing left exactly as-is
    "scalping_bot_on": "1",
    "scalping_mode": "paper",

    # 30m fixed (walk-forward newest segment was less-bad on 30m than 15m)
    "scalping_auto_tf": "0",
    "scalping_timeframe": "30m",
    "scalping_mtf_filter": "0",

    # reversion ONLY
    "scalping_trend_on": "0",
    "scalping_reversion_on": "1",
    "scalping_breakout_on": "0",
    "scalping_hybrid_on": "0",

    # the winning tuning: RSI<20/>80 extreme gate + fixed R:R 1:3
    "reversion_rsi_extreme": "20",
    "reversion_fixed_rr": "3",

    # use the strategy's own SL + (R:R 1:3) TP1 — auto_tpsl ON but ATR-SL OFF so
    # nothing overrides the reversion levels; single TP1 exit (no partial/trail).
    "scalping_auto_tpsl": "1",
    "atr_sl_enabled": "0",
    "scalping_partial_tp": "0",
    "scalping_auto_be": "0",
    "scalping_trail_auto": "0",

    # let the raw signal through — no model / adaptive filters layered on top
    "ai_model_on": "0",
    "scalping_win_filter": "0",
    "scalping_dir_filter": "0",
    "scalping_hour_filter": "0",
    "scalping_session_filter": "0",
    "scalping_session_pair_filter": "0",
    "scalping_corr_filter": "0",
    "scalping_news_on": "0",
    "scalping_funding_filter": "0",
    "scalping_min_lgbm": "0",

    # R:R 1:3 (=3.0) clears these easily; keep them low so nothing blocks it
    "min_rr_ratio": "0.3",
    "min_tp_pct": "0.0",
    "auto_pilot": "0",
    "global_auto_risk": "0",
}

print("=== CONFIGURING: scalping = reversion + RSI<20 + R:R 1:3 (30m, PAPER) ===")
for k, v in CONFIG.items():
    db.save_setting(k, v)
    print(f"  {k:26} = {db.get_setting(k)}")
print("=== DONE. swing 1d untouched. restart futures-engine to apply. ===")
print("NOTE: edge decayed in recent backtest data — PAPER experiment, watch it.")
