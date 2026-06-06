"""
strategies/trend.py — Strategy 1: Trend Following.

Reads an indicator-enriched DataFrame (see utils.indicators.compute_indicators)
for the entry timeframe, plus optional confirm/trend frames for MTF filtering.
Returns {"signal": BUY/SELL/NONE, "entry", "sl", "tp1", "tp2", "tp3"}.
"""

from utils.indicators import safe, has_enough

NONE = {"signal": "NONE", "entry": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp3": 0}


def _ema_bull(df):
    return (safe(df["ema21"]) > safe(df["ema50"]) > safe(df["ema200"]))


def _ema_bear(df):
    return (safe(df["ema21"]) < safe(df["ema50"]) < safe(df["ema200"]))


def _ascending(series):
    a, b, c = safe(series, -3), safe(series, -2), safe(series, -1)
    return c > b > a


def _descending(series):
    a, b, c = safe(series, -3), safe(series, -2), safe(series, -1)
    return c < b < a


def run(df_entry, df_confirm=None, df_trend=None, mtf=False):
    if not has_enough(df_entry):
        return dict(NONE)

    close = safe(df_entry["close"])
    ema21 = safe(df_entry["ema21"])
    ema50 = safe(df_entry["ema50"])
    adx = safe(df_entry.get("adx"))
    st_dir = safe(df_entry.get("supertrend_dir"))
    macd = safe(df_entry.get("macd"))
    macd_sig = safe(df_entry.get("macd_signal"))
    # Entry trigger: either a pullback to EMA21 (≤1.5%) OR a strong trend
    # (ADX > 30) where we ride momentum without waiting for a pullback —
    # otherwise strong, fast-moving trends (price far from EMA21) are missed.
    near_ema21 = abs(close - ema21) / max(ema21, 1e-9) <= 0.015
    strong_trend = adx > 30
    entry_ok = near_ema21 or strong_trend

    # Previous swing levels over last 20 candles.
    recent = df_entry.tail(20)
    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())

    confirm_ok_bull = True
    confirm_ok_bear = True
    if mtf and df_confirm is not None and has_enough(df_confirm):
        confirm_ok_bull = _ema_bull(df_confirm)
        confirm_ok_bear = _ema_bear(df_confirm)
    trend_ok_bull = True
    trend_ok_bear = True
    if mtf and df_trend is not None and has_enough(df_trend):
        trend_ok_bull = _ema_bull(df_trend)
        trend_ok_bear = _ema_bear(df_trend)

    # NOTE: the strict "last 3 highs & lows ascending" check was dropped — it
    # contradicted the EMA21 pullback requirement (a pullback dips recent
    # candles), so the two could never be true together. Trend is now confirmed
    # by EMA structure + ADX + Supertrend + MACD + MTF, entered on the pullback.
    bullish = (
        _ema_bull(df_entry)
        and adx > 20
        and st_dir > 0
        and entry_ok
        and macd > macd_sig
        and confirm_ok_bull
        and trend_ok_bull
    )
    if bullish:
        return {
            "signal": "BUY",
            "entry": close,
            "sl": ema50 * 0.999,
            "tp1": swing_high,
            "tp2": swing_high * 1.03,
            "tp3": swing_high * 1.06,
        }

    bearish = (
        _ema_bear(df_entry)
        and adx > 20
        and st_dir < 0
        and entry_ok
        and macd < macd_sig
        and confirm_ok_bear
        and trend_ok_bear
    )
    if bearish:
        return {
            "signal": "SELL",
            "entry": close,
            "sl": ema50 * 1.001,
            "tp1": swing_low,
            "tp2": swing_low * 0.97,
            "tp3": swing_low * 0.94,
        }

    return dict(NONE)
