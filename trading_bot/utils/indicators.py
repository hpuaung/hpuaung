"""
utils/indicators.py — shared technical indicator computation + ML features.

Indicators are implemented in pure pandas/numpy (no third-party TA library) so
the bot has no fragile/abandoned dependency. Column names match what the
strategy modules and the LightGBM feature builder expect. Everything is computed
once per cycle into stable columns to keep a single pass per timeframe (lighter
on a 1GB VPS).
"""

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Primitive indicator helpers (Wilder smoothing where standard TA uses it)
# ---------------------------------------------------------------------------
def _ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def _sma(series, length):
    return series.rolling(length).mean()


def _rma(series, length):
    """Wilder's smoothing (RMA) used by RSI/ATR/ADX."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def _rsi(close, length=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _true_range(high, low, close):
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def _atr(high, low, close, length=14):
    return _rma(_true_range(high, low, close), length)


def _adx(high, low, close, length=14):
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    atr = _atr(high, low, close, length)
    plus_di = 100 * _rma(plus_dm, length) / atr.replace(0, np.nan)
    minus_di = 100 * _rma(minus_dm, length) / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _rma(dx, length)


def _macd(close, fast=12, slow=26, signal=9):
    macd = _ema(close, fast) - _ema(close, slow)
    macd_signal = _ema(macd, signal)
    return macd, macd_signal, macd - macd_signal


def _bbands(close, length=20, std=2.0):
    mid = _sma(close, length)
    sd = close.rolling(length).std(ddof=0)
    return mid - std * sd, mid, mid + std * sd


def _stoch(high, low, close, k=14, d=3, smooth_k=3):
    ll = low.rolling(k).min()
    hh = high.rolling(k).max()
    fast_k = 100 * (close - ll) / (hh - ll).replace(0, np.nan)
    return fast_k.rolling(smooth_k).mean()  # smoothed %K


def _cci(high, low, close, length=20):
    tp = (high + low + close) / 3.0
    sma_tp = tp.rolling(length).mean()
    mad = tp.rolling(length).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))


def _supertrend(high, low, close, length=7, multiplier=3.0):
    """Return a direction series: +1 uptrend (bullish), -1 downtrend (bearish)."""
    atr = _atr(high, low, close, length)
    hl2 = (high + low) / 2.0
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n)
    c = close.values
    ub = upper.values
    lb = lower.values

    for i in range(1, n):
        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        # Carry forward the protective bands.
        if np.isnan(prev_upper) or ub[i] < prev_upper or c[i - 1] > prev_upper:
            final_upper[i] = ub[i]
        else:
            final_upper[i] = prev_upper
        if np.isnan(prev_lower) or lb[i] > prev_lower or c[i - 1] < prev_lower:
            final_lower[i] = lb[i]
        else:
            final_lower[i] = prev_lower
        # Determine trend direction.
        if not np.isnan(prev_upper) and c[i] > prev_upper:
            direction[i] = 1
        elif not np.isnan(prev_lower) and c[i] < prev_lower:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(direction, index=close.index)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a stable set of indicator columns to an OHLCV DataFrame.

    Returns the same frame with added columns. Missing/NaN values are left as
    NaN; callers should guard with `has_enough(df)` before reading [-1].
    """
    out = df.copy()
    # Work in float64 for indicator math, regardless of the float32 OHLCV.
    high = out["high"].astype("float64")
    low = out["low"].astype("float64")
    close = out["close"].astype("float64")
    vol = out["volume"].astype("float64")

    out["ema8"] = _ema(close, 8)
    out["ema21"] = _ema(close, 21)
    out["ema50"] = _ema(close, 50)
    out["ema200"] = _ema(close, 200)

    out["adx"] = _adx(high, low, close, 14)

    macd, macd_signal, macd_hist = _macd(close)
    out["macd"] = macd
    out["macd_signal"] = macd_signal
    out["macd_hist"] = macd_hist

    out["supertrend_dir"] = _supertrend(high, low, close, 7, 3.0)

    out["rsi"] = _rsi(close, 14)

    bb_lower, bb_mid, bb_upper = _bbands(close, 20, 2.0)
    out["bb_lower"] = bb_lower
    out["bb_mid"] = bb_mid
    out["bb_upper"] = bb_upper
    out["bb_width"] = bb_upper - bb_lower

    out["stoch_k"] = _stoch(high, low, close, 14, 3, 3)
    out["cci"] = _cci(high, low, close, 20)
    out["atr"] = _atr(high, low, close, 14)
    out["vol_ma"] = _sma(vol, 20)

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
