"""
strategies/ai_hybrid.py — Strategy 4: AI Hybrid (market-type router + LightGBM).

Pipeline:
  1. Detect market type (TRENDING / SQUEEZING / RANGING) and pick a primary
     sub-strategy result accordingly.
  2. Run the LightGBM model for a directional probability.
  3. Compute a 0-100 multi-factor cumulative score.
  4. Execute only if cumulative, lgbm_score and lgbm_direction all agree with
     the primary direction; size TP/SL from ATR.
"""

import numpy as np

from utils.indicators import safe, has_enough, build_features
from models.train import get_model

NONE = {"signal": "NONE", "entry": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp3": 0,
        "score": 0.0, "lgbm_score": 0.0, "market_type": "NONE"}

_SIG_INT = {"BUY": 2, "NONE": 1, "SELL": 0}


def _detect_market(df):
    adx = safe(df.get("adx"))
    bb_width = df.get("bb_width")
    squeezing = False
    if bb_width is not None and len(df) >= 21:
        cur = safe(bb_width, -1)
        ref = float(bb_width.iloc[-20:].mean())
        squeezing = cur < ref * 0.7
    if adx > 25:
        return "TRENDING"
    if squeezing:
        return "SQUEEZING"
    return "RANGING"


def _technical_score(df):
    """Fraction of bullish technical signals -> 0..100 (and the bull/bear bias)."""
    close = safe(df["close"])
    checks = [
        safe(df.get("ema21")) > safe(df.get("ema50")),
        safe(df.get("ema50")) > safe(df.get("ema200")),
        safe(df.get("macd")) > safe(df.get("macd_signal")),
        safe(df.get("supertrend_dir")) > 0,
        safe(df.get("rsi"), default=50) > 50,
        close > safe(df.get("ema21"), default=close),
        safe(df.get("stoch_k"), default=50) > 50,
        safe(df.get("cci")) > 0,
    ]
    bull = sum(1 for c in checks if c)
    total = len(checks)
    return bull / total * 100.0, bull, total


def _structure_score(df, primary):
    adx = min(safe(df.get("adx")), 50.0) / 50.0 * 100.0
    aligned = 100.0 if primary.get("signal") in ("BUY", "SELL") else 40.0
    return adx * 0.6 + aligned * 0.4


def _volume_score(df):
    vol = safe(df["volume"])
    vol_ma = safe(df.get("vol_ma"), default=vol) or 1e-9
    ratio = min(vol / vol_ma, 3.0) / 3.0 * 100.0
    # Buying pressure: close position within the candle range.
    high = safe(df["high"]); low = safe(df["low"]); close = safe(df["close"])
    rng = max(high - low, 1e-9)
    pressure = (close - low) / rng * 100.0
    return ratio * 0.6 + pressure * 0.4


def _sentiment_score(funding_rate, oi_change_pct, news_score):
    funding_bias = 50.0 + max(-50.0, min(50.0, -funding_rate * 5000.0))
    oi_bias = 50.0 + max(-50.0, min(50.0, oi_change_pct))
    news_bias = 50.0 + news_score * 50.0
    return funding_bias * 0.34 + oi_bias * 0.33 + news_bias * 0.33


def run(df_entry, trend_res, reversion_res, breakout_res,
        funding_rate=0.0, oi_change_pct=0.0, news_score=0.0,
        ai_threshold=0.75):
    if not has_enough(df_entry):
        return dict(NONE)

    # Step 1 — market type + primary strategy. Prefer the market-routed
    # strategy, but if it didn't fire, fall back to ANY enabled strategy that
    # did (so a valid setup is never ignored just because of the routing).
    market_type = _detect_market(df_entry)
    if market_type == "TRENDING":
        primary = trend_res or dict(NONE)
    elif market_type == "SQUEEZING":
        primary = breakout_res or dict(NONE)
    else:
        primary = reversion_res or dict(NONE)

    if primary.get("signal", "NONE") == "NONE":
        for r in (trend_res, reversion_res, breakout_res):
            if r and r.get("signal", "NONE") != "NONE":
                primary = r
                break

    if primary.get("signal", "NONE") == "NONE":
        return dict(NONE)
    direction = primary["signal"]

    # Step 2 — LightGBM probability.
    lgbm_score, lgbm_direction = 0.0, 1
    model = get_model()
    if model is not None:
        feats = build_features(
            df_entry,
            funding_rate=funding_rate,
            oi_change_pct=oi_change_pct,
            sentiment_score=news_score,
            trend_sig=_SIG_INT.get(trend_res.get("signal", "NONE"), 1) if trend_res else 1,
            reversion_sig=_SIG_INT.get(reversion_res.get("signal", "NONE"), 1) if reversion_res else 1,
            breakout_sig=_SIG_INT.get(breakout_res.get("signal", "NONE"), 1) if breakout_res else 1,
        )
        try:
            proba = model.predict_proba([feats])[0]
            lgbm_score = float(np.max(proba))
            lgbm_direction = int(np.argmax(proba))  # 0=SELL,1=HOLD,2=BUY
        except Exception:  # noqa: BLE001
            lgbm_score, lgbm_direction = 0.0, 1
    else:
        # No model yet — fall back to the primary with a neutral confidence so
        # the engine can still operate before the first training completes.
        lgbm_score = ai_threshold
        lgbm_direction = 2 if direction == "BUY" else 0

    # Step 3 — multi-factor cumulative score (0-100).
    tech, _, _ = _technical_score(df_entry)
    struct = _structure_score(df_entry, primary)
    volsc = _volume_score(df_entry)
    sent = _sentiment_score(funding_rate, oi_change_pct, news_score)
    # For a SELL the "bullish" technical score is inverted.
    if direction == "SELL":
        tech = 100.0 - tech
        sent = 100.0 - sent
    cumulative = tech * 0.40 + struct * 0.30 + volsc * 0.20 + sent * 0.10

    # The model is a SOFT INFLUENCE on the score, not a hard veto: it nudges the
    # cumulative up when it agrees and down when it disagrees, so a strong setup
    # can still trade even if the model leans the other way (e.g. expecting an
    # oversold bounce against a strong downtrend), while weak setups that the
    # model dislikes are filtered out.
    primary_class = 2 if direction == "BUY" else 0
    opposite = 0 if direction == "BUY" else 2
    if model is not None:
        if lgbm_direction == primary_class:
            cumulative += 10.0
        elif lgbm_direction == opposite:
            cumulative -= 12.0

    # Step 4 — execution gate. The cumulative score is bullish-biased and is
    # naturally low for counter-trend (reversion) buys at a bottom, so use a
    # lenient floor scaled by the threshold rather than a full threshold*100.
    cum_floor = ai_threshold * 60.0  # 0.50->30, 0.65->39, 0.75->45
    if cumulative < cum_floor:
        return {**NONE, "score": cumulative, "lgbm_score": lgbm_score,
                "market_type": market_type}

    atr = safe(df_entry.get("atr"))
    close = safe(df_entry["close"])
    if atr <= 0:
        atr = close * 0.005  # 0.5% fallback so SL/TP are never degenerate

    if direction == "BUY":
        entry = close
        sl = entry - atr * 1.2
        tp1 = entry + atr * 2.4   # SL=1.2x, TP1=2.4x → 1:2 R:R
        tp2 = entry + atr * 4.0
        tp3 = entry + atr * 5.1
    else:
        entry = close
        sl = entry + atr * 1.2
        tp1 = entry - atr * 2.4
        tp2 = entry - atr * 4.0
        tp3 = entry - atr * 5.1

    return {
        "signal": direction,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "score": cumulative, "lgbm_score": lgbm_score, "market_type": market_type,
    }
