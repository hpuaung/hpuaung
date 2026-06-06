"""
utils/news.py — GNews headlines + HuggingFace FinBERT sentiment (cloud only).

No local model is ever loaded. FinBERT runs through the HuggingFace inference
API. Both GNews and HF results are cached for 30 minutes per symbol to respect
the free-tier limits (GNews 100/day, HF 30k/month).
"""

import time
import threading

import requests

import database as db
from utils import vps_optimizer

GNEWS_URL = "https://gnews.io/api/v4/search"
# HuggingFace retired api-inference.huggingface.co; the current serverless
# inference endpoint is the router host.
HF_URL = "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert"
HF_URL_LEGACY = "https://api-inference.huggingface.co/models/ProsusAI/finbert"

GNEWS_DAILY_HARD_STOP = 95   # stop hitting the API, use cache only
HF_MONTHLY_LIMIT = 30000

# symbol -> (timestamp, value). Flushed under memory pressure.
_news_cache = {}
_hf_cache = {}
_lock = threading.Lock()
vps_optimizer.register_cache(_news_cache)
vps_optimizer.register_cache(_hf_cache)

# Map common futures symbols to a readable base name for the news query.
_BASE_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "Binance Coin",
    "XRP": "Ripple", "ADA": "Cardano", "DOGE": "Dogecoin", "LINK": "Chainlink",
    "AVAX": "Avalanche", "MATIC": "Polygon", "DOT": "Polkadot", "LTC": "Litecoin",
    "TRX": "Tron", "ATOM": "Cosmos",
}


def _base_name(symbol):
    base = symbol.replace("USDT", "").replace("USD", "")
    return _BASE_NAMES.get(base, base)


def get_cached_gnews(symbol, cache_min=30):
    """Return a concatenated headlines string for `symbol` (cached)."""
    now = time.time()
    ttl = cache_min * 60
    with _lock:
        cached = _news_cache.get(symbol)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

    token = db.get_setting("gnews_api", "")
    if not token:
        return ""

    # Respect the daily budget — once near the cap, serve stale cache only.
    used_today = db.get_counter(db.gnews_today_key())
    if used_today >= GNEWS_DAILY_HARD_STOP:
        with _lock:
            cached = _news_cache.get(symbol)
        return cached[1] if cached else ""

    query = f"{_base_name(symbol)} crypto"
    try:
        resp = requests.get(
            GNEWS_URL,
            params={"q": query, "lang": "en", "max": 5, "token": token},
            timeout=10,
        )
        db.incr_counter(db.gnews_today_key())
        if resp.status_code == 200:
            data = resp.json()
            headlines = " ".join(a.get("title", "") for a in data.get("articles", []))
            headlines = headlines[:512]
            with _lock:
                _news_cache[symbol] = (now, headlines)
            return headlines
    except Exception as e:  # noqa: BLE001
        db.log_event("GNEWS_ERROR", f"{symbol}: {e}")
    return ""


def call_huggingface_api(text):
    """
    Send text to FinBERT, return net_sentiment in [-1, 1]
    (positive_score - negative_score). Returns 0.0 on any failure.
    """
    if not text:
        return 0.0
    token = db.get_setting("hf_token", "")
    if not token:
        return 0.0

    used = db.get_counter(db.hf_month_key())
    if used >= HF_MONTHLY_LIMIT:
        return 0.0

    headers = {"Authorization": f"Bearer {token}"}
    payload = {"inputs": text[:512]}
    db.incr_counter(db.hf_month_key())
    for url in (HF_URL, HF_URL_LEGACY):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            if resp.status_code != 200:
                db.log_event("HF_ERROR", f"{resp.status_code} {resp.text[:80]}")
                continue
            data = resp.json()
            # Response is typically [[{label, score}, ...]] or [{...}].
            scores = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else data
            pos = neg = 0.0
            for item in scores:
                label = str(item.get("label", "")).lower()
                score = float(item.get("score", 0.0))
                if label == "positive":
                    pos = score
                elif label == "negative":
                    neg = score
            return max(-1.0, min(1.0, pos - neg))
        except Exception as e:  # noqa: BLE001
            db.log_event("HF_ERROR", f"{url.split('/')[2]}: {e}")
    return 0.0


def get_sentiment(symbol, cache_min=30):
    """
    Combined helper: fetch (cached) news + FinBERT score for a symbol.
    Returns net_sentiment in [-1, 1]. The HF result is itself cached so we do
    not re-call the model for the same headlines within the TTL window.
    """
    now = time.time()
    ttl = cache_min * 60
    with _lock:
        cached = _hf_cache.get(symbol)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

    headlines = get_cached_gnews(symbol, cache_min)
    score = call_huggingface_api(headlines)
    with _lock:
        _hf_cache[symbol] = (now, score)
    db.save_setting("last_sentiment_result", f"{symbol}: {score:+.3f}")
    return score


def gnews_requests_today():
    return db.get_counter(db.gnews_today_key())


def hf_requests_month():
    return db.get_counter(db.hf_month_key())
