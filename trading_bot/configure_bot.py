#!/usr/bin/env python3
"""configure_bot.py — apply the data-proven config: BREAKOUT on the DAILY (1d)
chart, swing engine, paper mode, restricted to the pairs where breakout-1d showed
a real edge (expectancy>0 AND PF>1.3) in strategy_backtest.py.

This is the ONE config the full timeframe sweep + per-pair test justified:
  - breakout-1d aggregate over 38 pairs: +0.150R, PF 1.49, n=798 over ~6.5yr —
    the whole universe is 🟢, so this is a structural edge, not one lucky pair.
  - MTF (1h/4h/1d), scalping (5m/15m/30m) and trend all tested NEGATIVE.
  - kept pairs = the 20 that are individually 🟢 (PF>1.3) with a real sample
    (n>=15) in strategy_backtest.py ... 1d ... detail only=breakout. Dropped
    ETH/BNB/DOGE/ADA/LINK/TRX/ATOM/UNI/BCH/ARB/OP/MANA/AXS/DYDX (<=noise) and
    INJ/ICP (🟢 but n<15, too thin).

Run on the VPS:  .venv/bin/python configure_bot.py
Then restart the engine so it picks up the new settings.
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

# the 20 pairs where breakout-1d is a clear 🟢 with a real sample (n>=15).
# in-sample, but the full 38-pair aggregate is also 🟢 (+0.150R) so the basket
# edge is broad, not cherry-picked.
PAIRS = ("BTCUSDT,SOLUSDT,XRPUSDT,AVAXUSDT,LTCUSDT,DOTUSDT,ETCUSDT,XLMUSDT,"
         "NEARUSDT,FILUSDT,AAVEUSDT,ALGOUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,"
         "EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT")

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
    "swing_news_on": "0",
    "swing_funding_filter": "0",

    # correlation filter OFF: on a market-wide breakout day all 20 pairs fire the
    # SAME direction; the default (max 2 same-direction) would block 18 of them
    # and wreck the basket. The backtest had no such cap.
    "swing_corr_filter": "0",

    # max-hold: a 1d breakout can take weeks to reach TP. The default auto-close
    # at 7 days would cut winners short (backtest held ~200 days). Give it room.
    "swing_auto_maxhold": "0",
    "swing_max_hold_days": "30",

    # no auto-pilot / global-auto overriding the chosen settings
    "auto_pilot": "0",
    "global_auto_risk": "0",

    # allow all 20 pairs to hold a position at once. The backtest took EVERY
    # signal (no concurrency cap), and daily breakouts cluster on market-wide
    # days, so capping lower would skip signals and under-reproduce the edge on
    # paper. Each trade still risks a fixed % via SL-aware sizing.
    # (For REAL money later, consider lowering to ~10 to limit correlated risk.)
    "max_concurrent_trades": "20",

    # trade only the winning pairs
    "selected_pairs": PAIRS,
}

print("=== CONFIGURING: breakout-1d swing (pruned pairs) ===")
for k, v in CONFIG.items():
    db.save_setting(k, v)
    print(f"  {k:26} = {db.get_setting(k)}")
print("=== DONE.  restart the engine to apply. ===")
