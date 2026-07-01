#!/usr/bin/env python3
"""configure_bot.py — apply the data-proven config: BREAKOUT on the DAILY (1d)
chart, swing engine, paper mode, restricted to the pairs where breakout-1d showed
a real edge (expectancy>0 AND PF>1.3) in strategy_backtest.py.

This is the ONE config the full timeframe sweep + per-pair test justified:
  - breakout-1d aggregate: +0.120R, PF 1.40 over ~6.5yr (only 🟢 in the sweep)
  - kept pairs (per-pair 1d breakout): BTC(PF3.14) AVAX(2.88) XRP(2.13)
    SOL(1.87) LTC(1.79). Dropped ETH/BNB/LINK/MATIC/ATOM/DOGE/ADA (<=noise).
  - scalping = dead (every strategy negative on 5m/15m/30m) -> OFF
  - trend = marginal (PF ~1.14, -0.42R at 1h) -> OFF, would only dilute breakout

Run on the VPS:  .venv/bin/python configure_bot.py
Then restart the engine so it picks up the new settings.
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

# pairs where breakout-1d is a clear 🟢 (in-sample; daily breakout is structural)
PAIRS = "BTCUSDT,SOLUSDT,XRPUSDT,AVAXUSDT,LTCUSDT"

CONFIG = {
    # engine selection: swing only, scalping off (scalping has no edge)
    "scalping_bot_on": "0",
    "swing_bot_on": "1",
    "swing_mode": "paper",

    # timeframe: daily, fixed (auto-tf off so it can't wander off 1d)
    "swing_auto_tf": "0",
    "swing_timeframe": "1d",
    "swing_mtf_filter": "0",

    # strategy: breakout ONLY (the proven edge); others off
    "swing_hybrid_on": "0",
    "swing_breakout_on": "1",
    "swing_trend_on": "0",
    "swing_reversion_on": "0",

    # exits: use breakout's native SL/TP + partial TP; no ATR override
    "swing_auto_tpsl": "1",
    "swing_partial_tp": "1",
    "atr_sl_enabled": "0",

    # min_rr MUST be low: breakout's SL sits at the broken level (tight), so a
    # high min_rr would block every entry.
    "min_rr_ratio": "0.3",
    "min_tp_pct": "0.0",

    # let the raw breakout edge through — no model/filters layered on top
    "ai_model_on": "0",
    "swing_session_filter": "0",
    "swing_min_lgbm": "0",
    "swing_win_filter": "0",
    "swing_dir_filter": "0",
    "swing_hour_filter": "0",
    "swing_session_pair_filter": "0",

    # no auto-pilot / global-auto overriding the chosen settings
    "auto_pilot": "0",
    "global_auto_risk": "0",

    # trade only the winning pairs
    "selected_pairs": PAIRS,
}

print("=== CONFIGURING: breakout-1d swing (pruned pairs) ===")
for k, v in CONFIG.items():
    db.save_setting(k, v)
    print(f"  {k:26} = {db.get_setting(k)}")
print("=== DONE.  restart the engine to apply. ===")
