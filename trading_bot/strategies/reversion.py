"""
strategies/reversion.py — Strategy 2: Mean Reversion.

Buys oversold extremes back toward the mean; sells overbought extremes.
Returns {"signal": BUY/SELL/NONE, "entry", "sl", "tp1", "tp2", "tp3"}.
"""

from utils.indicators import safe, has_enough

NONE = {"signal": "NONE", "entry": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp3": 0}


def run(df_entry, df_confirm=None, df_trend=None, mtf=False):
    if not has_enough(df_entry, need=60):
        return dict(NONE)

    close = safe(df_entry["close"])
    rsi = safe(df_entry.get("rsi"), default=50.0)
    bb_lower = safe(df_entry.get("bb_lower"), default=close)
    bb_mid = safe(df_entry.get("bb_mid"), default=close)
    bb_upper = safe(df_entry.get("bb_upper"), default=close)
    stoch_k = safe(df_entry.get("stoch_k"), default=50.0)
    cci = safe(df_entry.get("cci"), default=0.0)
    ema21 = safe(df_entry.get("ema21"), default=close)

    confirm_rsi = None
    if df_confirm is not None and has_enough(df_confirm, need=60):
        confirm_rsi = safe(df_confirm.get("rsi"), default=50.0)

    # Oversold BUY — require 3 of 5 indicators to agree (majority vote).
    # The old 5-condition AND was too strict for 4h swing candles where all
    # five rarely coincide simultaneously; scalping on 5m is fine since noise
    # causes frequent extreme readings, but 4h candles are more deliberate.
    _oversold_signals = [
        rsi <= 40,
        close <= bb_lower * 1.005,
        stoch_k < 30,
        cci < -80,
        close < ema21 * 0.99,
    ]
    oversold = (
        sum(_oversold_signals) >= 3
        and (confirm_rsi is None or confirm_rsi <= 50)
    )
    if oversold:
        return {
            "signal": "BUY",
            "entry": close,
            "sl": close * 0.99,
            "tp1": bb_mid,
            "tp2": ema21,
            "tp3": bb_upper,
        }

    # Overbought SELL — mirrored majority vote (3 of 5).
    _overbought_signals = [
        rsi >= 60,
        close >= bb_upper * 0.995,
        stoch_k > 70,
        cci > 80,
        close > ema21 * 1.01,
    ]
    overbought = (
        sum(_overbought_signals) >= 3
        and (confirm_rsi is None or confirm_rsi >= 50)
    )
    if overbought:
        return {
            "signal": "SELL",
            "entry": close,
            "sl": close * 1.01,
            "tp1": bb_mid,
            "tp2": ema21,
            "tp3": bb_lower,
        }

    return dict(NONE)
