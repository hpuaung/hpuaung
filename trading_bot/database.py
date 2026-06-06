"""
database.py — SQLite persistence layer + message bus between UI and engine.

The whole bot communicates through this single SQLite database:
    UI writes settings        -> DB
    Engine reads settings     <- DB
    Engine writes positions   -> DB
    UI reads positions/trades <- DB

A single module-level connection is shared across threads
(check_same_thread=False) and every write is serialized through one
threading.Lock so the daemon engine thread and the Streamlit threads never
corrupt each other.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_bot.db")

# One global write lock shared by every thread in the process.
_db_lock = threading.Lock()
_conn = None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def get_conn():
    """Return the shared sqlite3 connection, creating it on first use."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _conn.row_factory = sqlite3.Row
        # WAL gives us concurrent readers while a single writer holds the lock.
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def utcnow_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def init_db():
    """Create all tables if they do not exist and seed default settings."""
    conn = get_conn()
    with _db_lock:
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS active_positions (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol             TEXT,
                strategy           TEXT,
                side               TEXT,
                entry_price        REAL,
                entry_qty          REAL,
                sl_price           REAL,
                tp1                REAL,
                tp2                REAL,
                tp3                REAL,
                tp1_closed         INTEGER DEFAULT 0,
                tp2_closed         INTEGER DEFAULT 0,
                tp3_closed         INTEGER DEFAULT 0,
                status             TEXT DEFAULT 'open',
                timestamp          TEXT,
                leverage           INTEGER,
                timeframe          TEXT,
                atr_at_entry       REAL,
                trailing_active    INTEGER DEFAULT 0,
                trail_sl_price     REAL,
                paper_mode         INTEGER DEFAULT 0,
                order_id           TEXT,
                health_at_entry    REAL,
                funding_rate       REAL,
                open_interest      REAL,
                session            TEXT,
                lgbm_score         REAL,
                news_score         REAL,
                effective_leverage REAL,
                effective_risk_pct REAL,
                fees_estimated     REAL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT,
                strategy        TEXT,
                pair            TEXT,
                side            TEXT,
                entry_price     REAL,
                exit_price      REAL,
                qty             REAL,
                leverage        INTEGER,
                pnl_amount      REAL,
                pnl_percent     REAL,
                fees_paid       REAL,
                net_pnl         REAL,
                close_reason    TEXT,
                paper_mode      INTEGER DEFAULT 0,
                status          TEXT,
                hold_duration   TEXT,
                lgbm_score      REAL,
                news_score      REAL,
                session         TEXT,
                entry_timestamp TEXT,
                exit_timestamp  TEXT
            )
        """)

        # Rolling signal log shown in the dashboard (Recent Signals).
        c.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT,
                pair        TEXT,
                strategies  TEXT,
                ai_score    REAL,
                news_score  REAL,
                action      TEXT
            )
        """)

        # Generic event / audit log (VPS cleans, errors, guard trips...).
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                kind      TEXT,
                message   TEXT
            )
        """)

        # Daily/monthly API usage counters (e.g. gnews_2026-06-05 -> 12).
        c.execute("""
            CREATE TABLE IF NOT EXISTS counters (
                name  TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)

        conn.commit()
    _seed_defaults()


# ---------------------------------------------------------------------------
# Settings (key/value store)
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]


def get_float(key, default=0.0):
    try:
        return float(get_setting(key, default))
    except (TypeError, ValueError):
        return float(default)


def get_int(key, default=0):
    try:
        return int(float(get_setting(key, default)))
    except (TypeError, ValueError):
        return int(default)


def get_bool(key, default=False):
    """Settings store booleans as '0'/'1' strings."""
    val = get_setting(key, "1" if default else "0")
    return str(val) == "1"


def save_setting(key, value):
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()


def save_settings(mapping):
    """Bulk save a dict of settings in a single transaction."""
    conn = get_conn()
    with _db_lock:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [(k, str(v)) for k, v in mapping.items()],
        )
        conn.commit()


def get_all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Active positions
# ---------------------------------------------------------------------------
def insert_position(pos: dict) -> int:
    conn = get_conn()
    keys = list(pos.keys())
    placeholders = ",".join("?" for _ in keys)
    cols = ",".join(keys)
    with _db_lock:
        cur = conn.execute(
            f"INSERT INTO active_positions ({cols}) VALUES ({placeholders})",
            [pos[k] for k in keys],
        )
        conn.commit()
        return cur.lastrowid


def update_position(pos_id: int, fields: dict):
    if not fields:
        return
    conn = get_conn()
    sets = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [pos_id]
    with _db_lock:
        conn.execute(f"UPDATE active_positions SET {sets} WHERE id=?", vals)
        conn.commit()


def get_open_positions(strategy=None, paper_mode=None):
    conn = get_conn()
    q = "SELECT * FROM active_positions WHERE status='open'"
    args = []
    if strategy is not None:
        q += " AND strategy=?"
        args.append(strategy)
    if paper_mode is not None:
        q += " AND paper_mode=?"
        args.append(paper_mode)
    q += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_position(pos_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM active_positions WHERE id=?", (pos_id,)).fetchone()
    return dict(row) if row else None


def count_open_positions(side=None, strategy=None):
    conn = get_conn()
    q = "SELECT COUNT(*) AS n FROM active_positions WHERE status='open'"
    args = []
    if side is not None:
        q += " AND side=?"
        args.append(side)
    if strategy is not None:
        q += " AND strategy=?"
        args.append(strategy)
    return conn.execute(q, args).fetchone()["n"]


def delete_position(pos_id: int):
    conn = get_conn()
    with _db_lock:
        conn.execute("DELETE FROM active_positions WHERE id=?", (pos_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Trades (closed history)
# ---------------------------------------------------------------------------
def insert_trade(trade: dict) -> int:
    conn = get_conn()
    keys = list(trade.keys())
    placeholders = ",".join("?" for _ in keys)
    cols = ",".join(keys)
    with _db_lock:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            [trade[k] for k in keys],
        )
        conn.commit()
        return cur.lastrowid


def get_trades(limit=None, paper_mode=None):
    conn = get_conn()
    q = "SELECT * FROM trades"
    args = []
    if paper_mode is not None:
        q += " WHERE paper_mode=?"
        args.append(paper_mode)
    q += " ORDER BY id DESC"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def get_today_trades():
    """All trades whose exit happened today (UTC)."""
    conn = get_conn()
    today = today_utc_str()
    rows = conn.execute(
        "SELECT * FROM trades WHERE substr(COALESCE(exit_timestamp, timestamp),1,10)=? "
        "ORDER BY id DESC",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def clear_paper_trades():
    conn = get_conn()
    with _db_lock:
        conn.execute("DELETE FROM trades WHERE paper_mode=1")
        conn.execute("DELETE FROM active_positions WHERE paper_mode=1")
        conn.commit()


# ---------------------------------------------------------------------------
# Signals log
# ---------------------------------------------------------------------------
def log_signal(pair, strategies, ai_score, news_score, action):
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO signals(timestamp, pair, strategies, ai_score, news_score, action) "
            "VALUES (?,?,?,?,?,?)",
            (utcnow_str(), pair, strategies, ai_score, news_score, action),
        )
        # Keep only the most recent 200 rows to stay light on a 1GB box.
        conn.execute(
            "DELETE FROM signals WHERE id NOT IN "
            "(SELECT id FROM signals ORDER BY id DESC LIMIT 200)"
        )
        conn.commit()


def get_recent_signals(limit=10):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Events log
# ---------------------------------------------------------------------------
def log_event(kind, message):
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO events(timestamp, kind, message) VALUES (?,?,?)",
            (utcnow_str(), kind, message),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT 500)"
        )
        conn.commit()


def get_recent_events(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Counters (API usage)
# ---------------------------------------------------------------------------
def incr_counter(name, amount=1) -> int:
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=value+?",
            (name, amount, amount),
        )
        conn.commit()
        row = conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return row["value"] if row else 0


def get_counter(name) -> int:
    conn = get_conn()
    row = conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
    return row["value"] if row else 0


def gnews_today_key():
    return f"gnews_{today_utc_str()}"


def hf_month_key():
    return f"hf_{datetime.now(timezone.utc).strftime('%Y-%m')}"


# ---------------------------------------------------------------------------
# Default settings seeding
# ---------------------------------------------------------------------------
DEFAULTS = {
    # API & credentials
    "binance_testnet_api": "",
    "binance_testnet_secret": "",
    "binance_live_api": "",
    "binance_live_secret": "",
    "hf_token": "",
    "gnews_api": "",
    "telegram_token": "",
    "telegram_chat_id": "",

    # Global config
    "paper_trading_mode": "1",
    "starting_balance": "0",
    "daily_loss_limit_pct": "10.0",
    "max_drawdown_pause_pct": "25.0",
    "max_concurrent_trades": "5",
    "lev_risk_hard_cap_pct": "10.0",
    "selected_pairs": "BTCUSDT,ETHUSDT,SOLUSDT",

    # Scalping engine
    "scalping_bot_on": "0",
    "scalping_api_mode": "test",
    "scalping_timeframe": "5m",
    "scalping_confirm_tf": "15m",
    "scalping_trend_tf": "1h",
    "scalping_mtf_filter": "1",
    "scalping_auto_risk": "1",
    "scalping_base_leverage": "5",
    "scalping_base_risk_pct": "1.0",
    "scalping_tp1_pct": "1.5",
    "scalping_tp2_pct": "2.5",
    "scalping_tp3_pct": "4.0",
    "scalping_tp1_close_pct": "50",
    "scalping_tp2_close_pct": "30",
    "scalping_tp3_close_pct": "20",
    "scalping_sl_pct": "0.8",
    "scalping_partial_tp": "1",
    "scalping_auto_be": "1",
    "scalping_max_trades": "3",
    "scalping_ai_threshold": "0.75",
    "scalping_trend_on": "1",
    "scalping_reversion_on": "1",
    "scalping_breakout_on": "1",
    "scalping_hybrid_on": "1",
    "scalping_news_on": "0",
    "scalping_gnews_weight": "0.3",
    "scalping_hf_min_score": "0.60",
    "scalping_gnews_cache_min": "30",
    "scalping_session_filter": "0",
    "scalping_london_on": "1",
    "scalping_ny_on": "1",
    "scalping_asia_on": "1",
    "scalping_weekend_off": "0",
    "scalping_funding_filter": "0",
    "scalping_funding_weight": "0.20",
    "scalping_corr_filter": "1",
    "scalping_max_corr_trades": "2",

    # Swing engine
    "swing_bot_on": "0",
    "swing_api_mode": "test",
    "swing_timeframe": "4h",
    "swing_confirm_tf": "1d",
    "swing_trend_tf": "3d",
    "swing_mtf_filter": "1",
    "swing_auto_risk": "1",
    "swing_base_leverage": "3",
    "swing_base_risk_pct": "1.0",
    "swing_tp1_pct": "2.0",
    "swing_tp2_pct": "4.0",
    "swing_tp3_pct": "7.0",
    "swing_tp1_close_pct": "50",
    "swing_tp2_close_pct": "30",
    "swing_tp3_close_pct": "20",
    "swing_sl_pct": "2.5",
    "swing_partial_tp": "1",
    "swing_auto_be": "1",
    "swing_trail_pct": "1.5",
    "swing_max_hold_days": "7",
    "swing_ai_threshold": "0.80",
    "swing_trend_on": "1",
    "swing_reversion_on": "1",
    "swing_breakout_on": "1",
    "swing_hybrid_on": "1",
    "swing_news_on": "0",
    "swing_gnews_weight": "0.3",
    "swing_hf_min_score": "0.60",
    "swing_gnews_cache_min": "30",
    "swing_session_filter": "0",
    "swing_london_on": "1",
    "swing_ny_on": "1",
    "swing_asia_on": "1",
    "swing_weekend_off": "0",
    "swing_funding_filter": "0",
    "swing_funding_weight": "0.20",
    "swing_corr_filter": "1",
    "swing_max_corr_trades": "2",

    # VPS optimizer
    "vps_auto_clean_on": "1",
    "vps_ram_threshold_pct": "80",

    # Performance optimizer
    "win_streak_bonus_on": "0",
    "streak_win_count": "3",
    "streak_bonus_pct": "0.1",
    "streak_loss_count": "3",
    "streak_cut_pct": "0.5",

    # Blackout mode
    "blackout_on": "0",
    "blackout_volume_spike_x": "5.0",
    "blackout_atr_expand_x": "3.0",
    "blackout_before_min": "15",
    "blackout_after_min": "15",
    "blackout_action": "no_entry",

    # LightGBM
    "lgbm_retrain_schedule": "weekly",
    "lgbm_train_period": "6m",
    "lgbm_last_trained": "",
    "lgbm_accuracy": "0",

    # Notifications
    "notify_trade_open": "1",
    "notify_trade_close": "1",
    "notify_daily_report": "1",
    "notify_risk_alert": "1",
    "notify_engine_stop": "1",
    "notify_status_on": "1",
    "notify_status_interval_hr": "4",

    # Dashboard login (default 'admin'; set empty to disable the login screen)
    "admin_password": "admin",

    # Simplified Auto controls (timeframe + TP/SL + trailing)
    "scalping_auto_tf": "1",
    "swing_auto_tf": "1",
    "scalping_auto_tpsl": "1",
    "swing_auto_tpsl": "1",
    "scalping_tp_pct": "1.5",
    "swing_tp_pct": "4.0",
    "scalping_trail_auto": "0",
    "swing_trail_auto": "0",
    "scalping_trail_pct": "0.5",

    # Runtime/internal flags
    "blackout_active": "0",
    "win_streak": "0",
    "loss_streak": "0",
    "streak_risk_adj": "0.0",
    "emergency_stop": "0",
}


def _seed_defaults():
    """Insert any default that is not already present (never overwrite)."""
    conn = get_conn()
    existing = {r["key"] for r in conn.execute("SELECT key FROM settings").fetchall()}
    missing = {k: v for k, v in DEFAULTS.items() if k not in existing}
    if missing:
        save_settings(missing)
