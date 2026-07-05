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

        # Self-learning memory: the market features at each entry + whether the
        # resulting trade won. A model is trained on this so the bot learns which
        # entry conditions actually produce wins.
        c.execute("""
            CREATE TABLE IF NOT EXISTS learning (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                strategy  TEXT,
                pair      TEXT,
                features  TEXT,
                won       INTEGER,
                net_pnl   REAL,
                paper_mode INTEGER DEFAULT 1
            )
        """)
        # Migration: add paper_mode column if upgrading from older schema.
        try:
            c.execute("ALTER TABLE learning ADD COLUMN paper_mode INTEGER DEFAULT 1")
            conn.commit()
        except Exception:
            pass

        # Migration: store the entry feature vector on the open position so it
        # can be paired with the outcome when the trade closes. Check the column
        # via PRAGMA (no failing ALTER on every init, which could leave the
        # streamlit process's transaction state dirty and silently drop writes).
        existing_cols = {r[1] for r in c.execute("PRAGMA table_info(active_positions)").fetchall()}
        if "entry_features" not in existing_cols:
            c.execute("ALTER TABLE active_positions ADD COLUMN entry_features TEXT")

        conn.commit()
    _seed_defaults()
    _seed_from_env()


# ---------------------------------------------------------------------------
# Self-learning (entry features + win/loss outcome)
# ---------------------------------------------------------------------------
def add_learning(strategy, pair, features_json, won, net_pnl, paper_mode=True):
    conn = get_conn()
    with _db_lock:
        conn.execute(
            "INSERT INTO learning(timestamp, strategy, pair, features, won, net_pnl, paper_mode) "
            "VALUES (?,?,?,?,?,?,?)",
            (utcnow_str(), strategy, pair, features_json, 1 if won else 0, net_pnl,
             1 if paper_mode else 0),
        )
        conn.commit()


def learning_count():
    conn = get_conn()
    return conn.execute("SELECT COUNT(*) AS n FROM learning").fetchone()["n"]


def learning_dataset():
    """Return (list_of_feature_lists, list_of_won) from the learning table.
    By default (learn_from_paper=1) every closed trade feeds the win model, so
    the bot keeps self-learning while paper trading. Set learn_from_paper=0 to
    restrict learning to real fills (paper_mode=0) once trading live."""
    import json as _json
    conn = get_conn()
    if get_bool("learn_from_paper", True):
        q = "SELECT features, won FROM learning WHERE features IS NOT NULL"
    else:
        q = "SELECT features, won FROM learning WHERE features IS NOT NULL AND paper_mode=0"
    rows = conn.execute(q).fetchall()
    X, y = [], []
    for r in rows:
        try:
            feats = _json.loads(r["features"])
            if isinstance(feats, list) and feats:
                X.append(feats)
                y.append(int(r["won"]))
        except Exception:  # noqa: BLE001
            continue
    return X, y


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


def auto_flag(key, default=True):
    """A per-feature 'auto' flag that is ALSO forced on by the master Auto Pilot.

    When auto_pilot is on the bot fully self-manages (timeframe, AI threshold,
    risk sizing, ATR TP/SL, trailing, break-even, max-hold and every adaptive
    win-rate filter), so the user can switch one toggle and walk away. When
    auto_pilot is off, the individual per-engine auto toggle applies as before."""
    if get_bool("auto_pilot", False):
        return True
    return get_bool(key, default)


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


def export_backup_bytes():
    """Return a consistent snapshot of the whole DB as bytes (sqlite backup API),
    safe to download while the engine is running."""
    import tempfile
    conn = get_conn()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    dst = sqlite3.connect(tmp.name)
    with _db_lock:
        conn.backup(dst)
    dst.close()
    with open(tmp.name, "rb") as f:
        data = f.read()
    os.remove(tmp.name)
    return data


def restore_backup_bytes(data: bytes):
    """Overwrite the database file with an uploaded backup. The caller should
    restart the process afterwards so all connections re-open cleanly."""
    global _conn
    with _db_lock:
        try:
            if _conn is not None:
                _conn.close()
        except Exception:  # noqa: BLE001
            pass
        _conn = None
        with open(DB_PATH, "wb") as f:
            f.write(data)
        # Drop any WAL/SHM side files so the restored DB is authoritative.
        for ext in ("-wal", "-shm"):
            try:
                os.remove(DB_PATH + ext)
            except OSError:
                pass


def paper_balance():
    """
    The simulated paper wallet = starting capital + realized PnL of all closed
    paper trades. This is what the paper account is actually 'worth', so it goes
    up and down with wins/losses (the real testnet wallet never moves on paper
    trades). The engine uses this for sizing + health in paper mode, so the
    account compounds just like a real one.
    """
    conn = get_conn()
    starting = get_float("starting_balance", 0.0) or 0.0
    if starting <= 0:
        starting = 5000.0
    row = conn.execute("SELECT COALESCE(SUM(net_pnl), 0) AS s FROM trades WHERE paper_mode=1").fetchone()
    realized = float(row["s"] or 0.0)
    return starting + realized


def winrate(strategy=None, pair=None, side=None, paper_mode=None):
    """
    Historical win rate (%) and trade count for a context, read from the trades
    table — the bot's own learning memory. Returns (win_rate_or_None, n).
    paper_mode=0 for real only, =1 for paper only, None for all.
    """
    conn = get_conn()
    q = "SELECT net_pnl FROM trades WHERE 1=1"
    args = []
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    if pair:
        q += " AND pair=?"
        args.append(pair)
    if side:
        q += " AND side=?"
        args.append(side)
    if paper_mode is not None:
        q += " AND paper_mode=?"
        args.append(paper_mode)
    rows = conn.execute(q, args).fetchall()
    n = len(rows)
    if n == 0:
        return None, 0
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    return wins / n * 100.0, n


def winrate_hour(hour, strategy=None, paper_mode=0):
    """Win rate (%, n) for a specific UTC hour (0-23). Real trades only by default."""
    conn = get_conn()
    q = ("SELECT net_pnl FROM trades "
         "WHERE CAST(substr(COALESCE(exit_timestamp,timestamp),12,2) AS INTEGER)=?")
    args = [hour]
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    if paper_mode is not None:
        q += " AND paper_mode=?"
        args.append(paper_mode)
    rows = conn.execute(q, args).fetchall()
    n = len(rows)
    if n == 0:
        return None, 0
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    return wins / n * 100.0, n


def winrate_session_pair(session, pair, strategy=None, paper_mode=0):
    """Win rate (%, n) for a session × pair combo. Real trades only by default."""
    conn = get_conn()
    q = "SELECT net_pnl FROM trades WHERE session=? AND pair=?"
    args = [session, pair]
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    if paper_mode is not None:
        q += " AND paper_mode=?"
        args.append(paper_mode)
    rows = conn.execute(q, args).fetchall()
    n = len(rows)
    if n == 0:
        return None, 0
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    return wins / n * 100.0, n


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
    result = []
    for r in rows:
        d = dict(r)
        d.setdefault("type", d.get("kind", ""))  # alias for callers expecting 'type'
        result.append(d)
    return result


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
    "max_concurrent_trades": "4",
    "lev_risk_hard_cap_pct": "10.0",
    "paper_slippage_pct": "0.05",
    "min_rr_ratio": "2.0",
    "min_tp_pct": "0.4",
    "learn_from_paper": "1",
    "atr_sl_enabled": "1",
    "atr_sl_mult": "2.5",
    # Master Auto Pilot: one switch forces every per-engine auto + adaptive
    # filter on so the bot fully self-manages from win-rate history + balance.
    "auto_pilot": "0",
    # LightGBM direction model influence on entries (off = pure technical signals).
    "ai_model_on": "1",
    # 4-hourly Claude/rule-based auto-adjuster (ai_monitor.py). OFF: it's a
    # scalping-era tool that auto-removes pairs / changes risk, which fights the
    # breakout-1d broad-basket config. Left off so it can't mutate settings.
    "ai_monitor_on": "0",
    # Global risk limits managed automatically (max concurrent scales w/ balance).
    "global_auto_risk": "0",
    "selected_pairs": "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,LINKUSDT,AVAXUSDT,DOGEUSDT,ADAUSDT,LTCUSDT,DOTUSDT,ATOMUSDT",

    # Scalping engine
    "scalping_bot_on": "0",
    "scalping_api_mode": "test",
    "scalping_timeframe": "5m",
    "scalping_confirm_tf": "15m",
    "scalping_trend_tf": "1h",
    "scalping_mtf_filter": "1",
    "scalping_auto_risk": "0",
    "scalping_base_leverage": "10",
    "scalping_base_risk_pct": "1.8",
    "scalping_tp1_pct": "0.5",
    "scalping_tp2_pct": "0.8",
    "scalping_tp3_pct": "1.2",
    "scalping_tp1_close_pct": "100",
    "scalping_tp2_close_pct": "0",
    "scalping_tp3_close_pct": "0",
    "scalping_sl_pct": "0.3",
    "scalping_partial_tp": "0",
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
    "scalping_max_corr_trades": "3",

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
    "swing_tp1_pct": "3.0",
    "swing_tp2_pct": "6.0",
    "swing_tp3_pct": "10.0",
    "swing_tp1_close_pct": "40",
    "swing_tp2_close_pct": "35",
    "swing_tp3_close_pct": "25",
    "swing_sl_pct": "2.0",
    "swing_partial_tp": "1",
    "swing_auto_be": "1",
    "swing_trail_pct": "2.0",
    "swing_max_hold_days": "2",
    "swing_ai_threshold": "0.85",
    "swing_min_lgbm": "0.0",
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
    "scalping_trail_pct": "0.7",
    "scalping_auto_threshold": "1",
    "swing_auto_threshold": "1",
    "swing_auto_maxhold": "1",
    "scalping_win_filter": "1",
    "swing_win_filter": "1",
    "win_filter_min": "0.45",
    # Adaptive entry pattern filters (self-learning from own trade history)
    "scalping_dir_filter": "1",
    "swing_dir_filter": "1",
    "scalping_hour_filter": "1",
    "swing_hour_filter": "1",
    "scalping_session_pair_filter": "1",
    "swing_session_pair_filter": "1",
    # Per-engine trading mode: 'paper' (simulated on testnet) or 'real' (live).
    "scalping_mode": "paper",
    "swing_mode": "paper",

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


# Map .env / environment variable names to settings keys. Lets API keys, tokens
# etc. live in trading_bot/.env (git-ignored) so they survive a DB reset and
# never need re-entering in the dashboard.
_ENV_MAP = {
    "BINANCE_TESTNET_API": "binance_testnet_api",
    "BINANCE_TESTNET_SECRET": "binance_testnet_secret",
    "BINANCE_LIVE_API": "binance_live_api",
    "BINANCE_LIVE_SECRET": "binance_live_secret",
    "HF_TOKEN": "hf_token",
    "GNEWS_API": "gnews_api",
    "TELEGRAM_TOKEN": "telegram_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}


def _seed_from_env():
    """Seed secret settings from trading_bot/.env (and the process environment)
    so keys persist across DB resets / fresh clones. Only fills a setting that is
    currently empty — a value set in the dashboard is never overwritten."""
    values = {}
    env_path = os.path.join(os.path.dirname(DB_PATH), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    for env_key in _ENV_MAP:
        if os.environ.get(env_key):
            values[env_key] = os.environ[env_key]  # real env wins over .env file
    for env_key, set_key in _ENV_MAP.items():
        val = values.get(env_key, "")
        if val and not (get_setting(set_key) or ""):
            try:
                save_setting(set_key, val)
            except Exception:  # noqa: BLE001
                pass
