"""
strategies/breakout.py — Strategy 3: Breakout.

Detects horizontal support/resistance tested >= 3 times, a Bollinger squeeze in
the recent window, then a volume/ATR-confirmed break of the level.
Returns {"signal": BUY/SELL/NONE, "entry", "sl", "tp1", "tp2", "tp3"}.
"""

from utils.indicators import safe, has_enough

NONE = {"signal": "NONE", "entry": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp3": 0}

LOOKBACK = 50
TOLERANCE = 0.003  # 0.3%


def _find_levels(df):
    """
    Return (support, resistance): the lowest/highest price levels (taken from
    candle highs & lows) that were touched at least 3 times within tolerance.
    Falls back to window extremes if nothing qualifies.
    """
    window = df.tail(LOOKBACK)
    highs = window["high"].astype(float).tolist()
    lows = window["low"].astype(float).tolist()
    prices = highs + lows

    confirmed = []
    for level in prices:
        if level <= 0:
            continue
        touches = sum(1 for p in prices if abs(p - level) / level <= TOLERANCE)
        if touches >= 3:
            confirmed.append(level)

    if confirmed:
        return min(confirmed), max(confirmed)
    return float(window["low"].min()), float(window["high"].max())


def _squeeze_recently(df, window=10):
    """True if a Bollinger squeeze occurred within the last `window` candles."""
    bb_width = df.get("bb_width")
    if bb_width is None or len(df) < 25:
        return False
    for i in range(1, window + 1):
        idx = -i
        cur = safe(bb_width, idx)
        # Mean of the 20 widths preceding this candle.
        ref = bb_width.iloc[idx - 20:idx]
        if len(ref) == 0:
            continue
        if cur < float(ref.mean()) * 0.7:
            return True
    return False


def run(df_entry, df_confirm=None, df_trend=None, mtf=False):
    if not has_enough(df_entry, need=60):
        return dict(NONE)

    close = safe(df_entry["close"])
    prev_close = safe(df_entry["close"], -2)
    volume = safe(df_entry["volume"])
    vol_ma = safe(df_entry.get("vol_ma"), default=volume) or 1e-9
    atr = safe(df_entry.get("atr"))
    atr_ref = float(df_entry["atr"].tail(5).mean()) if "atr" in df_entry else atr

    support, resistance = _find_levels(df_entry)
    squeeze_ok = _squeeze_recently(df_entry, window=10)

    last20 = df_entry["close"].tail(20).astype(float)
    consolidation_height = float(last20.max() - last20.min())

    confirm_above = True
    confirm_below = True
    if df_confirm is not None and has_enough(df_confirm, need=20):
        c_close = safe(df_confirm["close"])
        confirm_above = c_close > resistance
        confirm_below = c_close < support

    vol_spike = volume > vol_ma * 2.0
    atr_expand = atr > atr_ref * 1.2

    bullish = (
        close > resistance
        and close > prev_close
        and vol_spike
        and atr_expand
        and squeeze_ok
        and confirm_above
    )
    if bullish:
        return {
            "signal": "BUY",
            "entry": close,
            "sl": resistance * 0.995,
            "tp1": resistance + consolidation_height * 0.5,
            "tp2": resistance + consolidation_height * 1.0,
            "tp3": resistance + consolidation_height * 1.5,
        }

    bearish = (
        close < support
        and vol_spike
        and atr_expand
        and confirm_below
    )
    if bearish:
        return {
            "signal": "SELL",
            "entry": close,
            "sl": support * 1.005,
            "tp1": support - consolidation_height * 0.5,
            "tp2": support - consolidation_height * 1.0,
            "tp3": support - consolidation_height * 1.5,
        }

    return dict(NONE)
