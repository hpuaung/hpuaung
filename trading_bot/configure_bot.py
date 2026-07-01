#!/usr/bin/env python3
"""configure_bot.py — apply the data-proven config: BREAKOUT on the DAILY (1d)
chart, swing engine, paper mode, restricted to the pairs where breakout-1d showed
a real edge (expectancy>0 AND PF>1.3) in strategy_backtest.py.

This is the ONE config every backtest justified:
  - breakout-1d over the 38-pair universe: strongly 🟢 (walk-forward all 3 eras
    green, PF ~2.0-2.4; out-of-sample PF 2.02 on unseen data).
  - MTF (1h/4h/1d), scalping (5m/15m/30m) and trend all tested NEGATIVE.
  - PAIR SELECTION DOESN'T HELP: the OOS test showed hand-picked pairs (PF 1.90)
    did WORSE than the whole universe (PF 2.02) and the "dropped" pairs (PF 2.13).
    The edge is in the strategy, not the pair choice — so trade the broad universe.

Run on the VPS:  .venv/bin/python configure_bot.py
Then restart the engine so it picks up the new settings.
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
db.init_db()

# BROAD 38-pair universe. The out-of-sample test (oos_backtest.py) proved that
# hand-picking pairs by past edge does NOT help — the "dropped" pairs did just as
# well on unseen data (PF 2.13 vs 1.90 for the selected ones). The edge lives in
# the STRATEGY (daily breakout), not in which pairs you pick, so we trade the
# whole liquid universe: more trades, same/better edge, zero selection bias.
PAIRS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
         "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,UNIUSDT,ETCUSDT,XLMUSDT,"
         "BCHUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,FILUSDT,INJUSDT,SUIUSDT,"
         "AAVEUSDT,ALGOUSDT,ICPUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,MANAUSDT,"
         "AXSUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT,DYDXUSDT")

CONFIG = {
    # engine selection: swing only, scalping off (scalping has no edge)
    "scalping_bot_on": "0",
    "swing_bot_on": "1",
    "swing_mode": "paper",

    # timeframe: daily. Auto-tf is now SAFE (its swing preset was changed from 4h
    # to 1d in engine._effective_tfs), so Auto ON also gives 1d. Entry is also set
    # to 1d explicitly so switching to Manual on the dashboard still lands on 1d.
    "swing_auto_tf": "1",
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
    # trailing OFF: the backtest exited on SL/TP1 only, so trailing would deviate
    # from the measured edge (it can cut winners short or exit early).
    "swing_trail_auto": "0",
    "swing_auto_be": "1",

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

    # broad basket: allow many concurrent positions so clustered market-wide
    # breakouts aren't skipped (the backtest took every signal). Each trade still
    # risks a fixed % via SL-aware sizing.
    # (For REAL money later, lower this to ~10-15 to limit correlated risk.)
    "max_concurrent_trades": "30",

    # trade only the winning pairs
    "selected_pairs": PAIRS,
}

print("=== CONFIGURING: breakout-1d swing (pruned pairs) ===")
for k, v in CONFIG.items():
    db.save_setting(k, v)
    print(f"  {k:26} = {db.get_setting(k)}")
print("=== DONE.  restart the engine to apply. ===")
