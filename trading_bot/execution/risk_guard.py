"""
execution/risk_guard.py — the always-on Global Risk Guard.

Every guard returns (allowed: bool, reason: str). The signal aggregator calls
them in order and only executes when all pass. Health-based multipliers and the
win/loss streak adjustment are also computed here.
"""

from datetime import datetime, timezone

import database as db


# ---------------------------------------------------------------------------
# Health ratio + multiplier
# ---------------------------------------------------------------------------
def health_ratio(equity, starting_balance):
    if starting_balance and starting_balance > 0:
        return equity / starting_balance * 100.0
    return 100.0


def health_multiplier(health):
    if health >= 75:
        return 1.00
    if health >= 50:
        return 0.75
    if health >= 25:
        return 0.50
    return 0.0  # <25% -> STOP


def health_zone(health):
    if health >= 75:
        return "green", "Normal"
    if health >= 50:
        return "yellow", "Caution"
    if health >= 25:
        return "orange", "Danger"
    return "red", "ALL ENGINES STOP"


def apply_health_guard(equity, starting_balance, strategy):
    """
    Returns (allowed, reason, multiplier). Encodes the drawdown recovery rules:
      75-50%  -> 0.75x, max 5 pairs
      50-25%  -> 0.50x, scalping paused (swing only)
      <25%    -> all stop
    """
    health = health_ratio(equity, starting_balance)
    mult = health_multiplier(health)
    if mult == 0.0:
        return False, f"Health {health:.0f}% < 25% — ALL ENGINES STOP", 0.0
    if health < 50 and strategy == "scalping":
        return False, f"Health {health:.0f}% — scalping paused (swing only)", mult
    return True, "", mult


# ---------------------------------------------------------------------------
# Daily loss guard
# ---------------------------------------------------------------------------
def today_pnl():
    trades = db.get_today_trades()
    return sum(float(t.get("net_pnl") or 0.0) for t in trades)


def apply_daily_loss_guard(equity):
    limit_pct = db.get_float("daily_loss_limit_pct", 10.0)
    pnl = today_pnl()
    if equity > 0 and pnl < 0 and (abs(pnl) / equity * 100.0) >= limit_pct:
        return False, f"Daily loss {abs(pnl)/equity*100:.1f}% >= {limit_pct:.0f}%"
    return True, ""


# ---------------------------------------------------------------------------
# Hard cap guard (cannot be disabled)
# ---------------------------------------------------------------------------
def apply_hard_cap_guard(leverage, risk_pct):
    cap = db.get_float("lev_risk_hard_cap_pct", 10.0)
    if leverage * risk_pct > cap:
        return False, f"Lev×Risk {leverage*risk_pct:.1f}% > hard cap {cap:.0f}%"
    return True, ""


# ---------------------------------------------------------------------------
# Correlation guard
# ---------------------------------------------------------------------------
def apply_correlation_guard(side, strategy):
    if not db.get_bool(f"{strategy}_corr_filter", False):
        return True, ""
    max_corr = db.get_int(f"{strategy}_max_corr_trades", 2)
    same_dir = db.count_open_positions(side=side)
    if same_dir >= max_corr:
        return False, f"{same_dir} open {side} trades >= max {max_corr}"
    return True, ""


# ---------------------------------------------------------------------------
# Blackout guard
# ---------------------------------------------------------------------------
def check_blackout(df):
    """
    Evaluate volume-spike AND ATR-expansion on the latest candle. Sets/keeps the
    blackout_active flag in DB. Returns True if blackout is currently active.
    """
    if not db.get_bool("blackout_on", False):
        return False
    try:
        vol = float(df["volume"].iloc[-1])
        vol_ma = float(df["volume"].tail(20).mean()) or 1e-9
        atr = float(df["atr"].iloc[-1]) if "atr" in df else 0.0
        atr_ref = float(df["atr"].tail(20).mean()) if "atr" in df else 1e-9
    except Exception:  # noqa: BLE001
        return False

    vol_x = db.get_float("blackout_volume_spike_x", 5.0)
    atr_x = db.get_float("blackout_atr_expand_x", 3.0)
    triggered = (vol > vol_ma * vol_x) and (atr > atr_ref * atr_x)
    if triggered:
        db.save_setting("blackout_active", "1")
        db.save_setting("blackout_until", db.utcnow_str())
        db.log_event("BLACKOUT", "volume+ATR spike — entries frozen")
    return db.get_bool("blackout_active", False)


def apply_blackout_guard():
    if not db.get_bool("blackout_on", False):
        return True, ""
    if db.get_bool("blackout_active", False):
        return False, "Blackout active — no new entries"
    return True, ""


# ---------------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------------
SESSIONS = {
    "london": (8, 12),
    "ny": (13, 17),
    "asia": (0, 4),
}


def apply_session_filter(strategy):
    if not db.get_bool(f"{strategy}_session_filter", False):
        return True, ""

    now = datetime.now(timezone.utc)
    # Weekend off?
    if db.get_bool(f"{strategy}_weekend_off", False) and now.weekday() >= 5:
        return False, "Weekend trading off"

    hour = now.hour
    active = []
    for name, (start, end) in SESSIONS.items():
        if db.get_bool(f"{strategy}_{name}_on", True):
            active.append((start, end))
    if not active:
        return True, ""  # no sessions enabled -> do not block
    for start, end in active:
        if start <= hour < end:
            return True, ""
    return False, f"UTC hour {hour} outside active sessions"


# ---------------------------------------------------------------------------
# Concurrency guard
# ---------------------------------------------------------------------------
def apply_concurrency_guard():
    max_trades = db.get_int("max_concurrent_trades", 5)
    if db.count_open_positions() >= max_trades:
        return False, f"Max concurrent trades {max_trades} reached"
    return True, ""


# ---------------------------------------------------------------------------
# Win/loss streak adjustment
# ---------------------------------------------------------------------------
def update_streak(net_pnl):
    """Call after each trade close to maintain win/loss streaks + risk adj."""
    if not db.get_bool("win_streak_bonus_on", False):
        return
    win = net_pnl > 0
    win_streak = db.get_int("win_streak", 0)
    loss_streak = db.get_int("loss_streak", 0)

    if win:
        win_streak += 1
        loss_streak = 0
    else:
        loss_streak += 1
        win_streak = 0

    adj = db.get_float("streak_risk_adj", 0.0)
    if win_streak >= db.get_int("streak_win_count", 3):
        adj += db.get_float("streak_bonus_pct", 0.1)
    if loss_streak >= db.get_int("streak_loss_count", 3):
        adj -= db.get_float("streak_cut_pct", 0.5)
    # Keep the cumulative adjustment within a sane band.
    adj = max(-3.0, min(3.0, adj))

    db.save_setting("win_streak", win_streak)
    db.save_setting("loss_streak", loss_streak)
    db.save_setting("streak_risk_adj", f"{adj:.3f}")


def streak_risk_adjustment():
    if not db.get_bool("win_streak_bonus_on", False):
        return 0.0
    return db.get_float("streak_risk_adj", 0.0)


# ---------------------------------------------------------------------------
# Auto recommendations (used by the engine in Auto mode + shown in the UI)
# ---------------------------------------------------------------------------
def recommended_leverage(equity):
    """Leverage scaled to account size — smaller balances trade smaller."""
    if equity <= 0:
        return 5
    if equity < 100:
        return 3
    if equity < 1000:
        return 5
    if equity < 10000:
        return 8
    return 10


def recommended_risk(strategy):
    """Risk % adapted from the bot's own win-rate history (SQLite)."""
    wr, n = db.winrate(strategy)
    rec = 1.0
    if n >= 15 and wr is not None:
        if wr < 40:
            rec = 0.5
        elif wr < 50:
            rec = 0.8
        elif wr > 60:
            rec = 1.5
    return rec
