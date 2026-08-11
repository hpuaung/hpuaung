"""swing_plans.py — two independent engine slots, configured separately:

  SWING slot   — the user picks ONE of three walk-forward-ROBUST plans on the
                 dashboard. Each is a single strategy on one timeframe at a
                 forced R:R 1:3:
                   Plan 1  Breakout-1d  ~9/mo   PF 1.81  (highest edge)
                   Plan 2  Trend-12h    ~20/mo  PF 1.54  (balanced)
                   Plan 3  Trend-6h     ~36/mo  PF 1.32  (most active)

  SCALPING slot — the reversion engine tuned earlier (RSI<20 extreme, R:R 1:3
                 on 30m). Runs INDEPENDENTLY, always, on its own slot. Picking a
                 swing plan does NOT touch it.

Both slots run at the same time (the engine has exactly these two slots). The
swing selector only rewrites the swing_* settings; scalping_* is left alone.
R:R 1:3 on the swing slot is realised by the engine's swing_fixed_rr override;
on the scalping slot by reversion's own reversion_fixed_rr (so scalping_fixed_rr
stays 0 to avoid double-overriding). Exits are full TP1/SL, no partial/BE/trail.
"""
import database as db

PAIRS = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
         "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,UNIUSDT,ETCUSDT,XLMUSDT,"
         "BCHUSDT,NEARUSDT,APTUSDT,ARBUSDT,OPUSDT,FILUSDT,INJUSDT,SUIUSDT,"
         "AAVEUSDT,ALGOUSDT,ICPUSDT,VETUSDT,HBARUSDT,GRTUSDT,SANDUSDT,MANAUSDT,"
         "AXSUSDT,EOSUSDT,THETAUSDT,XTZUSDT,CRVUSDT,DYDXUSDT")

PLAN_NAMES = {1: "Breakout-1d", 2: "Trend-12h", 3: "Trend-6h"}
PLAN_DESC = {
    1: "breakout-1d 1:3 · ~9 entries/mo · PF 1.81 (highest edge)",
    2: "trend-12h 1:3 · ~20 entries/mo · PF 1.54 (balanced)",
    3: "trend-6h 1:3 · ~36 entries/mo · PF 1.32 (most active)",
}

# Global risk limits + universe — shared by both slots.
GLOBAL = {
    "min_rr_ratio": "0.3", "min_tp_pct": "0.0", "max_concurrent_trades": "30",
    "daily_loss_limit_pct": "10", "max_drawdown_pause_pct": "25",
    "lev_risk_hard_cap_pct": "10", "atr_sl_enabled": "0", "ai_model_on": "0",
    "auto_pilot": "0", "global_auto_risk": "0", "selected_pairs": PAIRS,
}


def _clean_slot(slot, *, on, tf, trend, breakout, reversion):
    """Faithful-to-backtest settings for one engine slot: single strategy on one
    timeframe, full exit at TP1/SL (no partial/BE/trail/MTF), every adaptive
    filter off. R:R is applied by the caller (swing via {slot}_fixed_rr,
    scalping via reversion_fixed_rr)."""
    return {
        f"{slot}_bot_on": "1" if on else "0", f"{slot}_mode": "paper",
        f"{slot}_auto_tf": "0", f"{slot}_timeframe": tf, f"{slot}_mtf_filter": "0",
        f"{slot}_trend_on": "1" if trend else "0",
        f"{slot}_breakout_on": "1" if breakout else "0",
        f"{slot}_reversion_on": "1" if reversion else "0", f"{slot}_hybrid_on": "0",
        f"{slot}_emastoch_on": "0",
        f"{slot}_auto_risk": "1", f"{slot}_auto_tpsl": "1",
        f"{slot}_partial_tp": "0", f"{slot}_auto_be": "0", f"{slot}_trail_auto": "0",
        f"{slot}_auto_maxhold": "0", f"{slot}_max_hold_days": "30",
        f"{slot}_corr_filter": "0", f"{slot}_news_on": "0", f"{slot}_funding_filter": "0",
        f"{slot}_session_filter": "0", f"{slot}_win_filter": "0", f"{slot}_dir_filter": "0",
        f"{slot}_hour_filter": "0", f"{slot}_session_pair_filter": "0", f"{slot}_min_lgbm": "0",
    }


# ---- SWING slot: three selectable single-strategy plans (all 1:3) ----------
_SWING = {
    1: {**_clean_slot("swing", on=True, tf="1d",  trend=False, breakout=True,  reversion=False),
        "swing_fixed_rr": "3"},
    2: {**_clean_slot("swing", on=True, tf="12h", trend=True,  breakout=False, reversion=False),
        "swing_fixed_rr": "3"},
    3: {**_clean_slot("swing", on=True, tf="6h",  trend=True,  breakout=False, reversion=False),
        "swing_fixed_rr": "3"},
}

# ---- SCALPING slot: the reversion engine tuned this morning (independent) ---
SCALPING = {
    **_clean_slot("scalping", on=True, tf="30m", trend=False, breakout=False, reversion=True),
    "reversion_rsi_extreme": "20",   # only fire on RSI<=20 / >=80 extremes
    "reversion_fixed_rr": "3",       # reversion sets its own 1:3 TP
    "scalping_fixed_rr": "0",        # so the engine does NOT double-override it
}


def swing_plan_settings(n):
    """Only the swing_* settings for swing plan n."""
    return dict(_SWING[int(n)])


def apply_swing_plan(n):
    """Rewrite the SWING slot only. Scalping is left untouched. Live next scan."""
    for k, v in swing_plan_settings(n).items():
        db.save_setting(k, v)
    db.save_setting("active_swing_plan", str(int(n)))
    return int(n)


# The keys that DEFINE which plan is running: strategy + timeframe + R:R. Every
# other swing_* setting (correlation/session/news filters, partial/BE/trail,
# risk) is an independently-tunable knob that must NOT break plan identity.
# The old all-keys match returned None the moment you enabled, say,
# swing_corr_filter for a correlation cap — so the selector snapped back to
# Plan 1, and re-applying a plan reset every knob (the "plan reverts / settings
# reset" bug). Matching only the identity keys fixes that.
_PLAN_IDENTITY_KEYS = ("swing_timeframe", "swing_trend_on", "swing_breakout_on",
                       "swing_reversion_on", "swing_fixed_rr")


def active_swing_plan():
    """Swing plan the live DB matches on the IDENTITY keys (strategy + timeframe
    + R:R), or None. Independently-tunable filter/risk knobs are ignored so they
    never make a running plan look 'custom'."""
    for n in (1, 2, 3):
        want = swing_plan_settings(n)
        if all(str(db.get_setting(k, "")) == str(want[k]) for k in _PLAN_IDENTITY_KEYS):
            return n
    return None


def apply_scalping():
    """(Re)apply the independent reversion scalping slot."""
    for k, v in SCALPING.items():
        db.save_setting(k, v)


def apply_all(n=1):
    """Full config: global risk + swing plan n + the reversion scalping slot.
    Used to restore a coherent baseline (e.g. the deploy step)."""
    for k, v in GLOBAL.items():
        db.save_setting(k, v)
    apply_swing_plan(n)
    apply_scalping()
    return len({**GLOBAL, **swing_plan_settings(n), **SCALPING})


if __name__ == "__main__":
    import sys
    db.init_db()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    total = apply_all(n)
    print(f"applied {total} settings: swing Plan {n} ({PLAN_NAMES[n]}) + reversion "
          f"scalping slot. restart the engine or wait for the next scan.")
