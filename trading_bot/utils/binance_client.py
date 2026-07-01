"""
utils/binance_client.py — Binance Futures client factory + market data helpers.

Centralises client creation (testnet vs live), OHLCV fetching with a short TTL
cache (to spare both the rate limit and RAM), exchange-info filter parsing, and
account equity reads. Everything goes through the rate limiter + retry wrappers.
"""

import math
import time
import threading

import numpy as np
import pandas as pd

from binance.client import Client
from binance.exceptions import BinanceAPIException

import database as db
from utils.rate_limiter import rate_limited, with_retry
from utils import vps_optimizer

# 300 candles: enough to fully warm up EMA200 (has_enough needs >=210) while
# staying light on 1GB RAM. Fetching only 200 left EMA200 short of the guard,
# so no signal ever passed.
DEFAULT_KLINES = 300

# Cache clients per (api_mode) and exchange filters per symbol.
_clients = {}
_clients_lock = threading.Lock()
_exchange_filters = {}

# OHLCV cache: key -> (timestamp, DataFrame). Flushed by the VPS optimizer.
_ohlcv_cache = {}
_ohlcv_lock = threading.Lock()
vps_optimizer.register_cache(_ohlcv_cache)

# How long a klines result stays fresh, by interval (seconds).
_INTERVAL_TTL = {
    "1m": 20, "3m": 30, "5m": 45, "15m": 60, "30m": 90,
    "1h": 120, "2h": 180, "4h": 300, "6h": 300,
    "1d": 600, "3d": 900, "1w": 1800,
}


def _credentials(api_mode):
    if api_mode == "real":
        return (db.get_setting("binance_live_api", ""),
                db.get_setting("binance_live_secret", ""))
    return (db.get_setting("binance_testnet_api", ""),
            db.get_setting("binance_testnet_secret", ""))


def has_credentials(api_mode):
    """True only if BOTH key and secret are present for the mode."""
    key, secret = _credentials(api_mode)
    return bool(key) and bool(secret)


def get_client(api_mode="test") -> Client:
    """Return a cached Binance client for the given mode ('test'/'real')."""
    with _clients_lock:
        if api_mode in _clients:
            return _clients[api_mode]
        key, secret = _credentials(api_mode)
        testnet = api_mode != "real"
        client = Client(key or None, secret or None, testnet=testnet)
        if testnet:
            client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        _clients[api_mode] = client
        return client


def reset_clients():
    """Drop cached clients so new API keys take effect."""
    with _clients_lock:
        _clients.clear()


# ---------------------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------------------
@with_retry(max_retries=2, base_delay=2)
@rate_limited(weight=5)
def test_connection(api_mode="test"):
    """Return (ok: bool, message: str). Also reads equity to prove auth works."""
    try:
        client = get_client(api_mode)
        acct = client.futures_account()
        bal = float(acct.get("totalWalletBalance", 0.0))
        return True, f"Connected. Wallet balance: {bal:.2f} USDT"
    except BinanceAPIException as e:
        return False, f"Binance error: {e.message}"
    except Exception as e:  # noqa: BLE001
        return False, f"Error: {e}"


# ---------------------------------------------------------------------------
# Account equity (live, never hardcoded)
# ---------------------------------------------------------------------------
@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=5)
def get_equity(api_mode="test"):
    """Live total wallet balance in USDT from Binance Futures."""
    client = get_client(api_mode)
    acct = client.futures_account()
    return float(acct.get("totalWalletBalance", 0.0))


@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=5)
def get_available_balance(api_mode="test"):
    client = get_client(api_mode)
    acct = client.futures_account()
    return float(acct.get("availableBalance", 0.0))


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------
@with_retry()
@rate_limited(weight=2)
def _raw_klines(client, symbol, interval, limit):
    return client.futures_klines(symbol=symbol, interval=interval, limit=limit)


@with_retry()
@rate_limited(weight=5)
def _raw_klines_page(client, symbol, interval, limit, end_time=None):
    if end_time is None:
        return client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    return client.futures_klines(
        symbol=symbol, interval=interval, limit=limit, endTime=end_time)


def get_ohlcv_deep(symbol, interval="5m", total=6000, api_mode="test") -> pd.DataFrame:
    """Backtest-only: fetch up to `total` klines by paging backwards (Binance caps
    each request at 1500). NOT used by the live bot — it only calls this from the
    backtest scripts when a long history is needed to get a trustworthy sample on
    low timeframes. Stops early when the exchange runs out of history."""
    if total <= 1500:
        return get_ohlcv(symbol, interval, total, api_mode)
    client = get_client(api_mode)
    rows = []
    end_time = None
    seen = set()
    while len(rows) < total:
        want = min(1500, total - len(rows))
        page = _raw_klines_page(client, symbol, interval, want, end_time)
        if not page:
            break
        # prepend older data; dedupe on open_time in case of boundary overlap
        page = [r for r in page if int(r[0]) not in seen]
        if not page:
            break
        for r in page:
            seen.add(int(r[0]))
        rows = page + rows
        end_time = int(rows[0][0]) - 1
        if len(page) < want:
            break
    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype("float32")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["open_time", "open", "high", "low", "close", "volume"]]


def get_ohlcv(symbol, interval="5m", limit=DEFAULT_KLINES, api_mode="test") -> pd.DataFrame:
    """
    Return an OHLCV DataFrame with a short TTL cache. Numeric columns use
    float32 to halve memory versus the default float64.
    """
    key = f"{api_mode}:{symbol}:{interval}:{limit}"
    ttl = _INTERVAL_TTL.get(interval, 60)
    now = time.time()
    with _ohlcv_lock:
        cached = _ohlcv_cache.get(key)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

    client = get_client(api_mode)
    raw = _raw_klines(client, symbol, interval, limit)
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbav", "tqav", "ignore",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype("float32")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["open_time", "open", "high", "low", "close", "volume"]]

    with _ohlcv_lock:
        _ohlcv_cache[key] = (now, df)
    return df


# ---------------------------------------------------------------------------
# Mark price / last price
# ---------------------------------------------------------------------------
@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=1)
def get_price(symbol, api_mode="test"):
    client = get_client(api_mode)
    data = client.futures_symbol_ticker(symbol=symbol)
    return float(data["price"])


# ---------------------------------------------------------------------------
# Funding rate & open interest
# ---------------------------------------------------------------------------
@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=1)
def get_funding_rate(symbol, api_mode="test"):
    """Latest funding rate as a float (e.g. 0.0001 = 0.01%)."""
    client = get_client(api_mode)
    data = client.futures_mark_price(symbol=symbol)
    return float(data.get("lastFundingRate", 0.0))


@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=1)
def get_open_interest(symbol, api_mode="test"):
    client = get_client(api_mode)
    data = client.futures_open_interest(symbol=symbol)
    return float(data.get("openInterest", 0.0))


def get_oi_change_pct(symbol, api_mode="test"):
    """
    Approximate OI change % using the 5m open-interest-hist endpoint when
    available; falls back to 0.0 if not permitted on testnet.
    """
    try:
        client = get_client(api_mode)
        hist = _oi_hist(client, symbol)
        if len(hist) >= 2:
            prev = float(hist[-2]["sumOpenInterest"])
            cur = float(hist[-1]["sumOpenInterest"])
            if prev > 0:
                return (cur - prev) / prev * 100.0
    except Exception:  # noqa: BLE001 - optional data
        pass
    return 0.0


@with_retry(max_retries=2, base_delay=2)
@rate_limited(weight=1)
def _oi_hist(client, symbol):
    return client.futures_open_interest_hist(symbol=symbol, period="5m", limit=2)


# ---------------------------------------------------------------------------
# Exchange info filters (tickSize / stepSize / min / max qty)
# ---------------------------------------------------------------------------
@with_retry()
@rate_limited(weight=1)
def _fetch_symbol_info(client, symbol):
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            return s
    return None


def get_filters(symbol, api_mode="test"):
    """Return dict with tickSize, stepSize, minQty, maxQty (cached per symbol)."""
    if symbol in _exchange_filters:
        return _exchange_filters[symbol]
    client = get_client(api_mode)
    s = _fetch_symbol_info(client, symbol)
    result = {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001, "maxQty": 1e9}
    if s:
        for f in s["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                result["tickSize"] = float(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                result["stepSize"] = float(f["stepSize"])
                result["minQty"] = float(f["minQty"])
                result["maxQty"] = float(f["maxQty"])
    _exchange_filters[symbol] = result
    return result


def round_price(price, tick_size):
    """Round a price to the symbol's tick size precision."""
    s = ("%.10f" % tick_size).rstrip("0")
    precision = len(s.split(".")[-1]) if "." in s else 0
    return round(price, precision)


def truncate_qty(qty, step_size):
    """Floor a quantity down to the symbol's step size precision."""
    s = ("%.10f" % step_size).rstrip("0")
    precision = len(s.split(".")[-1]) if "." in s else 0
    factor = 10 ** precision
    return math.floor(qty * factor) / factor
