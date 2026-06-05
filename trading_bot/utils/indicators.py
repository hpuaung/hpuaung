"""
utils/indicators.py — shared technical indicator computation + ML features.

All indicators are computed once per cycle into stable, renamed columns so the
strategy modules and the LightGBM feature builder read from the same frame.
This keeps a single pandas_ta pass per timeframe (lighter on 1GB RAM).
"""

import numpy as np
import pandas as pd


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a stable set of indicator columns to an OHLCV DataFrame.

    Returns the same frame with added columns. Missing/NaN values are left as
    NaN; callers should guard with `has_enough(df)` before reading [-1].
    """
    import pandas_ta as ta  # lazy: only needed for indicator computation
    out = df.copy()
    high, low, close, vol = out["high"], out["low"], out["close"], out["volume"]

    # Trend EMAs
    out["ema8"] = ta.ema(close, length=8)
    out["ema21"] = ta.ema(close, length=21)
    out["ema50"] = ta.ema(close, length=50)
    out["ema200"] = ta.ema(close, length=200)

    # ADX / DI
    adx = ta.adx(high, low, close, length=14)
    if adx is not None:
        out["adx"] = adx.get("ADX_14")

    # MACD
    macd = ta.macd(close)
    if macd is not None:
        out["macd"] = macd.get("MACD_12_26_9")
        out["macd_signal"] = macd.get("MACDs_12_26_9")
        out["macd_hist"] = macd.get("MACDh_12_26_9")

    # Supertrend (7, 3.0) -> direction column is +1 (up) / -1 (down)
    st = ta.supertrend(high, low, close, length=7, multiplier=3.0)
    if st is not None:
        dir_col = [c for c in st.columns if c.startswith("SUPERTd")]
        if dir_col:
            out["supertrend_dir"] = st[dir_col[0]]

    # RSI
    out["rsi"] = ta.rsi(close, length=14)

    # Bollinger Bands (20, 2.0)
    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None:
        out["bb_lower"] = bb.get("BBL_20_2.0")
        out["bb_mid"] = bb.get("BBM_20_2.0")
        out["bb_upper"] = bb.get("BBU_20_2.0")
        out["bb_width"] = out["bb_upper"] - out["bb_lower"]

    # Stochastic (14, 3, 3)
    stoch = ta.stoch(high, low, close, k=14, d=3, smooth_k=3)
    if stoch is not None:
        kcol = [c for c in stoch.columns if c.startswith("STOCHk")]
        if kcol:
            out["stoch_k"] = stoch[kcol[0]]

    # CCI (20)
    out["cci"] = ta.cci(high, low, close, length=20)

    # ATR (14)
    out["atr"] = ta.atr(high, low, close, length=14)

    # Volume moving average (20)
    out["vol_ma"] = ta.sma(vol, length=20)

    return out


def has_enough(df: pd.DataFrame, need=210) -> bool:
    """True if there are enough rows for EMA200 etc. to be valid."""
    return df is not None and len(df) >= need


def safe(series, idx=-1, default=0.0):
    """Read series[idx] returning a default for NaN/empty/out-of-range."""
    try:
        val = series.iloc[idx]
        if pd.isna(val):
            return default
        return float(val)
    except Exception:  # noqa: BLE001
        return default


def build_features(df_ind: pd.DataFrame, funding_rate=0.0, oi_change_pct=0.0,
                   sentiment_score=0.0, trend_sig=0, reversion_sig=0,
                   breakout_sig=0):
    """
    Build the 23-feature vector for the LightGBM model from the last candle.

    Order matches the spec exactly:
      rsi, ema8_ratio, ema21_ratio, ema50_ratio, ema200_ratio,
      bb_position, bb_width_ratio, stoch_k, cci_norm, adx, macd_hist,
      supertrend_dir, atr_ratio, volume_ratio, funding_rate, oi_change_pct,
      sentiment_score, candle_body_ratio, upper_wick_ratio, lower_wick_ratio,
      trend_signal_int, reversion_signal_int, breakout_signal_int
    """
    close = safe(df_ind["close"], -1, 0.0)
    high = safe(df_ind["high"], -1, close)
    low = safe(df_ind["low"], -1, close)
    open_ = safe(df_ind["open"], -1, close)
    rng = max(high - low, 1e-9)

    ema8 = safe(df_ind.get("ema8", pd.Series([close])), -1, close)
    ema21 = safe(df_ind.get("ema21", pd.Series([close])), -1, close)
    ema50 = safe(df_ind.get("ema50", pd.Series([close])), -1, close)
    ema200 = safe(df_ind.get("ema200", pd.Series([close])), -1, close)

    bb_lower = safe(df_ind.get("bb_lower", pd.Series([close])), -1, close)
    bb_upper = safe(df_ind.get("bb_upper", pd.Series([close])), -1, close)
    bb_mid = safe(df_ind.get("bb_mid", pd.Series([close])), -1, close)
    bb_range = max(bb_upper - bb_lower, 1e-9)
    bb_position = (close - bb_lower) / bb_range  # 0..1 within the bands
    bb_width_ratio = bb_range / max(bb_mid, 1e-9)

    atr = safe(df_ind.get("atr", pd.Series([0.0])), -1, 0.0)
    vol = safe(df_ind["volume"], -1, 0.0)
    vol_ma = safe(df_ind.get("vol_ma", pd.Series([vol])), -1, vol) or 1e-9

    body = abs(close - open_)
    upper_wick = high - max(close, open_)
    lower_wick = min(close, open_) - low

    features = [
        safe(df_ind.get("rsi", pd.Series([50.0])), -1, 50.0),
        close / max(ema8, 1e-9),
        close / max(ema21, 1e-9),
        close / max(ema50, 1e-9),
        close / max(ema200, 1e-9),
        bb_position,
        bb_width_ratio,
        safe(df_ind.get("stoch_k", pd.Series([50.0])), -1, 50.0),
        safe(df_ind.get("cci", pd.Series([0.0])), -1, 0.0) / 100.0,  # normalized
        safe(df_ind.get("adx", pd.Series([0.0])), -1, 0.0),
        safe(df_ind.get("macd_hist", pd.Series([0.0])), -1, 0.0),
        safe(df_ind.get("supertrend_dir", pd.Series([0.0])), -1, 0.0),
        atr / max(close, 1e-9),                       # atr_ratio
        vol / vol_ma,                                  # volume_ratio
        funding_rate,
        oi_change_pct,
        sentiment_score,
        body / rng,                                    # candle_body_ratio
        upper_wick / rng,                              # upper_wick_ratio
        lower_wick / rng,                              # lower_wick_ratio
        int(trend_sig),
        int(reversion_sig),
        int(breakout_sig),
    ]
    return np.array(features, dtype="float32")


FEATURE_NAMES = [
    "rsi", "ema8_ratio", "ema21_ratio", "ema50_ratio", "ema200_ratio",
    "bb_position", "bb_width_ratio", "stoch_k", "cci_norm", "adx", "macd_hist",
    "supertrend_dir", "atr_ratio", "volume_ratio", "funding_rate", "oi_change_pct",
    "sentiment_score", "candle_body_ratio", "upper_wick_ratio", "lower_wick_ratio",
    "trend_signal_int", "reversion_signal_int", "breakout_signal_int",
]
