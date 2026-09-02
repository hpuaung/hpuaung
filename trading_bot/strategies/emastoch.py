"""
strategies/emastoch.py — Strategy 5: EMA stack + slope + Stochastic pullback.

The user's own design, and the only strategy here validated on a large sample
AND a 3-era walk-forward with the NEWEST era strongest:

    6h, R:R 1:3, confirm window 5   ->  PF 1.74   expR +0.473   n=203
    eras 2.34 / 1.51 / 1.86  (newest strongest — no decay)

The rules, exactly as backtested:
  * EMA 9 > 21 > 200 (BUY) or 9 < 21 < 200 (SELL) — the stack must be ordered.
  * EMA 9 and 21 must be SLOPING, measured in ATR per bar so the test is
    independent of chart scale (the "30 degrees" idea made measurable); EMA200
    only has to lean the right way.
  * Stochastic %K must have crossed UP through 20 (BUY) / DOWN through 80 (SELL)
    within the last `confirm_bars` bars — i.e. a pullback just ended.
  * RSI crossing 55 up / 45 down is OPTIONAL and OFF by default: the ablation
    showed it changes almost nothing (PF 1.79 vs 1.74) while costing entries.
  * The "clean close beyond EMA9/21" rule was REMOVED — it cost both entries and
    PF, because it demands a close at a high while the Stochastic rule demands a
    pullback low. They fight each other.

Stop = 1.5 x ATR (what was backtested). TP1 is a 1:3 multiple of that; the
engine's {slot}_fixed_rr can override it.

Returns {"signal": BUY/SELL/NONE, "entry", "sl", "tp1", "tp2", "tp3"}.
"""

import database as db
from utils.indicators import safe, has_enough

NONE = {"signal": "NONE", "entry": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp3": 0}

SLOPE_LB = 5          # bars over which EMA slope is measured (matches backtest)


def _crossed_up(series, level, bars):
    """True if `series` crossed UP through `level` within the last `bars` bars."""
    n = len(series)
    for i in range(max(1, n - bars), n):
        try:
            if series.iloc[i] > level >= series.iloc[i - 1]:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _crossed_down(series, level, bars):
    n = len(series)
    for i in range(max(1, n - bars), n):
        try:
            if series.iloc[i] < level <= series.iloc[i - 1]:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def run(df_entry, df_confirm=None, df_trend=None, mtf=False):
    # EMA200 + the slope lookback need a long warm-up.
    if not has_enough(df_entry, need=210):
        return dict(NONE)

    close_s = df_entry["close"].astype("float64")
    close = float(close_s.iloc[-1])
    atr = safe(df_entry.get("atr"), default=0.0)
    if close <= 0 or atr <= 0:
        return dict(NONE)

    # EMA9 is not part of the shared indicator set — compute it here.
    ema9_s = close_s.ewm(span=9, adjust=False).mean()
    ema9 = float(ema9_s.iloc[-1])
    ema21_s = df_entry["ema21"].astype("float64")
    ema200_s = df_entry["ema200"].astype("float64")
    ema21 = float(ema21_s.iloc[-1])
    ema200 = float(ema200_s.iloc[-1])

    # Tunables (defaults are the validated values).
    slope_min = db.get_float("emastoch_slope_min", 0.10)
    bars = db.get_int("emastoch_confirm_bars", 5)
    sl_mult = db.get_float("emastoch_sl_atr", 1.5)
    use_rsi = db.get_bool("emastoch_use_rsi", False)

    def slope(series):
        """EMA rise per bar, expressed in ATR — scale free."""
        if len(series) <= SLOPE_LB:
            return 0.0
        prev = float(series.iloc[-1 - SLOPE_LB])
        return (float(series.iloc[-1]) - prev) / (SLOPE_LB * atr)

    s9, s21, s200 = slope(ema9_s), slope(ema21_s), slope(ema200_s)

    stack_bull = ema9 > ema21 > ema200
    stack_bear = ema9 < ema21 < ema200
    slope_bull = s9 >= slope_min and s21 >= slope_min and s200 > 0
    slope_bear = s9 <= -slope_min and s21 <= -slope_min and s200 < 0

    stoch = df_entry.get("stoch_k")
    if stoch is None:
        return dict(NONE)
    stoch = stoch.astype("float64")
    st_up = _crossed_up(stoch, 20.0, bars)
    st_dn = _crossed_down(stoch, 80.0, bars)

    if use_rsi:
        rsi_s = df_entry.get("rsi")
        if rsi_s is None:
            return dict(NONE)
        rsi_s = rsi_s.astype("float64")
        rsi_up = _crossed_up(rsi_s, 55.0, bars)
        rsi_dn = _crossed_down(rsi_s, 45.0, bars)
    else:
        rsi_up = rsi_dn = True

    risk = sl_mult * atr

    if stack_bull and slope_bull and st_up and rsi_up:
        sl = close - risk
        return {
            "signal": "BUY",
            "entry": close,
            "sl": sl,
            "tp1": close + 3.0 * risk,
            "tp2": close + 4.5 * risk,
            "tp3": close + 6.0 * risk,
        }

    if stack_bear and slope_bear and st_dn and rsi_dn:
        sl = close + risk
        return {
            "signal": "SELL",
            "entry": close,
            "sl": sl,
            "tp1": close - 3.0 * risk,
            "tp2": close - 4.5 * risk,
            "tp3": close - 6.0 * risk,
        }

    return dict(NONE)
