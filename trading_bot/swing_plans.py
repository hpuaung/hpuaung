"""swing_plans.py — the three walk-forward-validated swing deployments the user
picks between on the dashboard. The engine has exactly two slots ("swing" and
"scalping"), each running ONE timeframe, so a plan = what each slot trades.

All three share the same swing slot: breakout on the DAILY chart at a forced
R:R 1:3 (walk-forward ROBUST, PF 1.81). They differ only in the second slot:

  Plan 1  Balanced      2nd slot = trend 12h 1:3   (~28 entries/mo total, PF 1.54)
  Plan 2  Max activity  2nd slot = trend  6h 1:3   (~45 entries/mo total, PF 1.32)
  Plan 3  Purist        2nd slot = OFF             (~9  entries/mo, PF 1.81 only)

Every combo below cleared the walk-forward bar (all 3 eras positive, newest
PF>=1.2). The fragile ones the sweep flagged (fast-TF breakout, trend-8h) are
deliberately excluded. R:R 1:3 is realised by the engine's {slot}_fixed_rr
override; exits are the strategy SL or the 1:3 TP with no partial/BE/trail so
live behaviour matches the backtest.
"""
import database as db

PAIRS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
         "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,UNIUSDT,ETCUSDT,XLMUSDT,"
         "BCHUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,FILUSDT,INJUSDT,SUIUSDT,"
         "AAVEUSDT,ALGOUSDT,ICPUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,MANAUSDT,"
         "AXSUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT,DYDXUSDT")

PLAN_NAMES = {1: "Balanced", 2: "Max activity", 3: "Purist"}
PLAN_DESC = {
    1: "breakout-1d + trend-12h (both 1:3) · ~28 entries/mo · PF 1.54–1.81",
    2: "breakout-1d + trend-6h (both 1:3) · ~45 entries/mo · PF 1.32–1.81",
    3: "breakout-1d 1:3 only · ~9 entries/mo · PF 1.81 (highest edge)",
}

# Global risk limits + universe — shared by every plan.
GLOBAL = {
    "min_rr_ratio": "0.3", "min_tp_pct": "0.0", "max_concurrent_trades": "30",
    "daily_loss_limit_pct": "10", "max_drawdown_pause_pct": "25",
    "lev_risk_hard_cap_pct": "10", "atr_sl_enabled": "0", "ai_model_on": "0",
    "auto_pilot": "0", "global_auto_risk": "0", "selected_pairs": PAIRS,
    "reversion_rsi_extreme": "0", "reversion_fixed_rr": "0",
}


def _slot(slot, *, on, tf, trend, breakout):
    """Faithful-to-backtest settings for one engine slot: single strategy on one
    timeframe, forced R:R 1:3, full exit at TP1/SL (no partial/BE/trail/MTF),
    every adaptive filter off."""
    return {
        f"{slot}_bot_on": "1" if on else "0",
        f"{slot}_mode": "paper",
        f"{slot}_auto_tf": "0", f"{slot}_timeframe": tf, f"{slot}_mtf_filter": "0",
        f"{slot}_trend_on": "1" if trend else "0",
        f"{slot}_breakout_on": "1" if breakout else "0",
        f"{slot}_reversion_on": "0", f"{slot}_hybrid_on": "0",
        f"{slot}_fixed_rr": "3",
        f"{slot}_auto_risk": "1", f"{slot}_auto_tpsl": "1",
        f"{slot}_partial_tp": "0", f"{slot}_auto_be": "0", f"{slot}_trail_auto": "0",
        f"{slot}_auto_maxhold": "0", f"{slot}_max_hold_days": "30",
        f"{slot}_corr_filter": "0", f"{slot}_news_on": "0", f"{slot}_funding_filter": "0",
        f"{slot}_session_filter": "0", f"{slot}_win_filter": "0", f"{slot}_dir_filter": "0",
        f"{slot}_hour_filter": "0", f"{slot}_session_pair_filter": "0", f"{slot}_min_lgbm": "0",
    }


# The swing slot is identical in all plans: breakout on the daily chart, 1:3.
_SWING = _slot("swing", on=True, tf="1d", trend=False, breakout=True)

# The second slot ("scalping") is what the plan chooses.
_SECOND = {
    1: _slot("scalping", on=True, tf="12h", trend=True, breakout=False),
    2: _slot("scalping", on=True, tf="6h",  trend=True, breakout=False),
    3: _slot("scalping", on=False, tf="12h", trend=False, breakout=False),
}


def plan_settings(n):
    """Full settings dict for plan n (global + swing slot + second slot)."""
    return {**GLOBAL, **_SWING, **_SECOND[int(n)]}


def apply_plan(n):
    """Write every setting for plan n. Live-read by the engine next scan."""
    for k, v in plan_settings(n).items():
        db.save_setting(k, v)
    db.save_setting("active_swing_plan", str(int(n)))
    return int(n)


def active_plan():
    """Return the plan number whose settings the live DB currently matches, or
    None if the config has been hand-edited away from every plan."""
    for n in (1, 2, 3):
        if all(str(db.get_setting(k, "")) == v for k, v in plan_settings(n).items()):
            return n
    return None


if __name__ == "__main__":
    import sys
    db.init_db()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    apply_plan(n)
    print(f"applied swing Plan {n} ({PLAN_NAMES[n]}: {PLAN_DESC[n]}). "
          "restart the engine (or wait for the next scan).")
