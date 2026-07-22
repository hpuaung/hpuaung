"""proven_config.py — the ONE data-proven configuration (single source of truth).

'Auto' on the dashboard = apply() these exact settings: swing 1d breakout +
scalping reversion(RSI<20, R:R 1:3) + the global risk limits, all validated by
backtest / walk-forward / OOS. If manual tinkering drifts the config, one click
restores it. Used by the dashboard button and can be run standalone.
"""
import database as db

PAIRS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
         "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,UNIUSDT,ETCUSDT,XLMUSDT,"
         "BCHUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,FILUSDT,INJUSDT,SUIUSDT,"
         "AAVEUSDT,ALGOUSDT,ICPUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,MANAUSDT,"
         "AXSUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT,DYDXUSDT")

PROVEN = {
    # ---- global risk (shared by BOTH engines) ----
    "min_rr_ratio": "0.3",            # low: breakout SL is tight (R:R can be <1)
    "min_tp_pct": "0.0",
    "max_concurrent_trades": "30",
    "daily_loss_limit_pct": "10",
    "max_drawdown_pause_pct": "25",
    "lev_risk_hard_cap_pct": "10",
    "atr_sl_enabled": "0",
    "ai_model_on": "0",
    "auto_pilot": "0",
    "global_auto_risk": "0",
    "selected_pairs": PAIRS,

    # ---- SWING: 1d breakout (walk-forward + OOS validated) ----
    "swing_bot_on": "1", "swing_mode": "paper",
    "swing_auto_tf": "1", "swing_timeframe": "1d", "swing_mtf_filter": "0",
    "swing_breakout_on": "1", "swing_trend_on": "0",
    "swing_reversion_on": "0", "swing_hybrid_on": "0",
    "swing_auto_tpsl": "1", "swing_partial_tp": "1",
    "swing_trail_auto": "0", "swing_auto_be": "1",
    "swing_auto_maxhold": "0", "swing_max_hold_days": "30",
    "swing_corr_filter": "0", "swing_news_on": "0", "swing_funding_filter": "0",
    "swing_session_filter": "0", "swing_win_filter": "0", "swing_dir_filter": "0",
    "swing_hour_filter": "0", "swing_session_pair_filter": "0", "swing_min_lgbm": "0",

    # ---- SCALPING: reversion + RSI<20 extreme + R:R 1:3 (30m) ----
    "scalping_bot_on": "1", "scalping_mode": "paper",
    "scalping_auto_tf": "0", "scalping_timeframe": "30m", "scalping_mtf_filter": "0",
    "scalping_reversion_on": "1", "scalping_trend_on": "0",
    "scalping_breakout_on": "0", "scalping_hybrid_on": "0",
    "reversion_rsi_extreme": "20", "reversion_fixed_rr": "3",
    "scalping_auto_tpsl": "1", "scalping_partial_tp": "0",
    "scalping_auto_be": "0", "scalping_trail_auto": "0",
    "scalping_corr_filter": "0", "scalping_news_on": "0", "scalping_funding_filter": "0",
    "scalping_session_filter": "0", "scalping_win_filter": "0", "scalping_dir_filter": "0",
    "scalping_hour_filter": "0", "scalping_session_pair_filter": "0", "scalping_min_lgbm": "0",
}


def apply():
    """Write every proven setting to the DB. Returns the number applied."""
    for k, v in PROVEN.items():
        db.save_setting(k, v)
    return len(PROVEN)


def drift():
    """Return the list of settings whose current value differs from proven,
    so the dashboard can show whether the live config matches 'Auto'."""
    out = []
    for k, v in PROVEN.items():
        if str(db.get_setting(k, "")) != v:
            out.append((k, db.get_setting(k, ""), v))
    return out


if __name__ == "__main__":
    db.init_db()
    n = apply()
    print(f"applied {n} proven settings. restart futures-engine to take effect.")
