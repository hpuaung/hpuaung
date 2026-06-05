"""
models/train.py — LightGBM training + a process-wide model holder.

Training runs in a background thread so it never blocks the Streamlit UI. The
config is intentionally small (n_estimators=100, max_depth=5, num_leaves=20,
n_jobs=1) to fit comfortably in 1GB RAM.

Labels (3-class):
    future_return = close[i+3] / close[i] - 1
    > +1%  -> 2 (BUY)
    < -1%  -> 0 (SELL)
    else   -> 1 (HOLD)
"""

import os
import threading
from datetime import datetime

import numpy as np

import database as db

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "lgbm_model.pkl")

_model = None
_model_lock = threading.Lock()
_training = False

# Candle history per pair used for training (kept modest for RAM).
_TRAIN_LIMIT = {"1m": 1000, "3m": 1000, "5m": 1500, "6m": 1500, "1y": 1500}


def get_model():
    """Return the loaded model, loading from disk on first access."""
    global _model
    with _model_lock:
        if _model is None and os.path.exists(MODEL_PATH):
            try:
                import joblib
                _model = joblib.load(MODEL_PATH)
            except Exception as e:  # noqa: BLE001
                db.log_event("LGBM_LOAD_ERROR", str(e))
                _model = None
        return _model


def is_training():
    return _training


def _interval_for_period(period):
    # Map a training "period" to a klines interval used to gather samples.
    return {"1m": "15m", "3m": "1h", "6m": "4h", "1y": "1d"}.get(period, "4h")


def _build_dataset(period, api_mode):
    """Assemble (X, y) from all selected pairs' history."""
    from utils import indicators, binance_client
    pairs = [p.strip() for p in db.get_setting("selected_pairs", "BTCUSDT").split(",") if p.strip()]
    interval = _interval_for_period(period)
    limit = _TRAIN_LIMIT.get(period, 1500)

    X, y = [], []
    for symbol in pairs:
        try:
            df = binance_client.get_ohlcv(symbol, interval=interval, limit=limit, api_mode=api_mode)
        except Exception as e:  # noqa: BLE001
            db.log_event("LGBM_DATA_ERROR", f"{symbol}: {e}")
            continue
        if not indicators.has_enough(df, need=210):
            continue
        ind = indicators.compute_indicators(df)
        closes = ind["close"].astype("float32").values
        n = len(ind)
        # Leave room for the 3-candle forward label; skip warm-up rows.
        for i in range(210, n - 3):
            row = ind.iloc[: i + 1]
            feat = indicators.build_features(row)
            future_return = closes[i + 3] / max(closes[i], 1e-9) - 1.0
            if future_return > 0.01:
                label = 2
            elif future_return < -0.01:
                label = 0
            else:
                label = 1
            X.append(feat)
            y.append(label)
    return np.array(X, dtype="float32"), np.array(y, dtype="int8")


def train_lgbm_model(period=None, api_mode=None):
    """Train and persist the model. Safe to call from a background thread."""
    global _model, _training
    if _training:
        return
    _training = True
    try:
        import lightgbm as lgb
        import joblib
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score

        period = period or db.get_setting("lgbm_train_period", "6m")
        api_mode = api_mode or ("real" if db.get_setting("scalping_api_mode") == "real" else "test")

        db.log_event("LGBM_TRAIN", f"start period={period}")
        X, y = _build_dataset(period, api_mode)
        if len(X) < 200 or len(set(y.tolist())) < 2:
            db.log_event("LGBM_TRAIN", f"insufficient data: {len(X)} samples")
            return

        X_train, X_val, y_train, y_val = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        model = lgb.LGBMClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.05,
            num_leaves=20,
            class_weight="balanced",
            n_jobs=1,
            subsample=0.8,
            colsample_bytree=0.8,
            verbose=-1,
        )
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(10, verbose=False)],
        )
        acc = accuracy_score(y_val, model.predict(X_val))

        joblib.dump(model, MODEL_PATH)
        with _model_lock:
            _model = model
        db.save_setting("lgbm_last_trained", str(datetime.utcnow()))
        db.save_setting("lgbm_accuracy", f"{acc:.4f}")
        db.log_event("LGBM_TRAIN", f"done acc={acc:.4f} samples={len(X)}")
    except Exception as e:  # noqa: BLE001
        db.log_event("LGBM_TRAIN_ERROR", str(e))
    finally:
        _training = False


def train_in_background(period=None, api_mode=None):
    """Kick off training in a daemon thread (no-op if already training)."""
    if _training:
        return
    t = threading.Thread(
        target=train_lgbm_model, kwargs={"period": period, "api_mode": api_mode},
        daemon=True, name="lgbm-train",
    )
    t.start()


def ensure_model_on_start():
    """Load the model if present, otherwise start a background training run."""
    if os.path.exists(MODEL_PATH):
        get_model()
    else:
        train_in_background()
