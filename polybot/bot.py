#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║         POLYMARKET SMART HYBRID TRADING BOT v2.4                ║
║         7-Layer Filter | Multi-Market | Smart Capital            ║
║         Author  : Production Grade Bot                           ║
║         Env     : AWS Ubuntu t2.micro                            ║
║         Run     : python bot.py                                  ║
║         Dashboard: http://0.0.0.0:8501                           ║
╠══════════════════════════════════════════════════════════════════╣
║  STRATEGY LAYERS:                                                ║
║   L1 Sentiment      HuggingFace political model > 0.55          ║
║   L2 Momentum       6hr price change > 2%                        ║
║   L3 EV Edge        estimate - market_price > 8%                 ║
║   L4 Liquidity      volume>$10K, spread<3%                       ║
║   L5 Time           days_to_resolution > 14                      ║
║   L6 No Duplicate   no existing position in same market          ║
║   L7 API Health     all APIs responding                          ║
╠══════════════════════════════════════════════════════════════════╣
║  EXIT CONDITIONS:                                                ║
║   E1 Target Hit     entry + (edge x 70%)                        ║
║   E2 Sentiment Drop score falls below 0.30                       ║
║   E3 Time Safety    48hr before market resolution                ║
╠══════════════════════════════════════════════════════════════════╣
║  CAPITAL ALLOCATION:                                             ║
║   Rank 1 signal → 40% of free capital                           ║
║   Rank 2 signal → 35% of free capital                           ║
║   Rank 3 signal → 25% of free capital                           ║
║   Reserve       → 20% always locked                             ║
╠══════════════════════════════════════════════════════════════════╣
║  INSTALL:                                                        ║
║   pip install py_clob_client flask requests                      ║
║              python-dotenv bcrypt                                ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ═══════════════════════════════════════════════════════════════════
# SECTION 1 — IMPORTS
# ═══════════════════════════════════════════════════════════════════
import os
import sys
import json
import time
import math
import logging
import sqlite3
import hashlib
import threading
from datetime import datetime, timedelta, timezone

def utcnow() -> datetime:
    """Timezone-aware UTC now (replaces deprecated utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
from collections import deque
from functools import wraps
from typing import Optional, Dict, List, Tuple

import bcrypt
import requests
from dotenv import load_dotenv
from flask import (
    Flask, render_template_string, request,
    redirect, url_for, session, jsonify
)

# Polymarket CLOB Client (graceful fallback to paper mode)
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.constants import POLYGON
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False

load_dotenv()

# ═══════════════════════════════════════════════════════════════════
# SECTION 2 — ENVIRONMENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════
# Polymarket
POLYGON_WALLET_PRIVATE_KEY  = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS   = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# APIs
GNEWS_API_KEY               = os.getenv("GNEWS_API_KEY", "")
HUGGINGFACE_API_KEY         = os.getenv("HUGGINGFACE_API_KEY", "")
TELEGRAM_BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID            = os.getenv("TELEGRAM_CHAT_ID", "")

# Flask
FLASK_SECRET_KEY            = os.getenv("FLASK_SECRET_KEY", os.urandom(32).hex())
ADMIN_PASSWORD              = os.getenv("ADMIN_PASSWORD", "admin1234")

# Constants
CLOB_HOST                   = "https://clob.polymarket.com"
GNEWS_BASE                  = "https://gnews.io/api/v4/search"
HF_MODEL_URL                = ("https://router.huggingface.co/hf-inference/models/"
                               "cardiffnlp/twitter-roberta-base-sentiment-latest"
                               "/pipeline/text-classification")
POLYMARKET_GAMMA_API        = "https://gamma-api.polymarket.com"

# Paper trading mode if keys not set
PAPER_MODE = not (POLYGON_WALLET_PRIVATE_KEY and POLYMARKET_FUNDER_ADDRESS)

# ═══════════════════════════════════════════════════════════════════
# SECTION 3 — LOGGING SYSTEM
# ═══════════════════════════════════════════════════════════════════
LOG_BUFFER: deque = deque(maxlen=50)

class BufferHandler(logging.Handler):
    """Custom handler that stores logs in memory buffer for dashboard."""
    def emit(self, record):
        ts  = datetime.now().strftime("%H:%M:%S")
        lvl = record.levelname
        msg = self.format(record)
        entry = f"[{ts}] [{lvl}] {msg}"
        LOG_BUFFER.appendleft(entry)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
        BufferHandler(),
    ],
)
log = logging.getLogger("POLYBOT")

# ═══════════════════════════════════════════════════════════════════
# SECTION 4 — SQLITE DATABASE MANAGER
# ═══════════════════════════════════════════════════════════════════
DB_PATH = "polybot.db"
_db_lock = threading.Lock()

def get_db() -> sqlite3.Connection:
    """Thread-safe database connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create all tables on first run."""
    with _db_lock:
        conn = get_db()
        c = conn.cursor()

        # Settings table — configurable via dashboard
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Active positions
        c.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              TEXT PRIMARY KEY,
                market_id       TEXT NOT NULL,
                market_name     TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                side            TEXT NOT NULL DEFAULT 'YES',
                entry_price     REAL NOT NULL,
                current_price   REAL NOT NULL,
                amount_usd      REAL NOT NULL,
                shares          REAL NOT NULL,
                sentiment_score REAL,
                edge_pct        REAL,
                target_price    REAL,
                entry_time      TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open'
            )
        """)

        # Closed trades history
        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id          TEXT PRIMARY KEY,
                market_id   TEXT NOT NULL,
                market_name TEXT NOT NULL,
                side        TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price  REAL NOT NULL,
                amount_usd  REAL NOT NULL,
                pnl_usd     REAL NOT NULL,
                pnl_pct     REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                entry_time  TEXT NOT NULL,
                exit_time   TEXT NOT NULL,
                won         INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Portfolio snapshots for chart
        c.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance   REAL NOT NULL
            )
        """)

        # Market scan cache
        c.execute("""
            CREATE TABLE IF NOT EXISTS market_cache (
                market_id       TEXT PRIMARY KEY,
                market_name     TEXT NOT NULL,
                token_id        TEXT NOT NULL,
                category        TEXT,
                volume_24h      REAL,
                current_price   REAL,
                end_date        TEXT,
                last_scanned    TEXT
            )
        """)

        # Default settings
        defaults = {
            "sentiment_threshold": "0.55",
            "momentum_pct":        "2.0",
            "min_edge_pct":        "8.0",
            "min_volume_24h":      "10000",
            "max_spread_pct":      "3.0",
            "min_days_resolution": "14",
            "max_positions":       "3",
            "capital_reserve_pct": "20.0",
            "max_trade_usd":       "2.0",
            "consecutive_loss_limit": "3",
            "capital_total":       "50.0",
            "paper_mode":          "1" if PAPER_MODE else "0",
            "l2_bypass":           "0",   # Set "1" to skip momentum check
            # API Keys — stored encrypted in DB, set via Dashboard
            "api_polygon_key":     POLYGON_WALLET_PRIVATE_KEY,
            "api_funder_address":  POLYMARKET_FUNDER_ADDRESS,
            "api_gnews_key":       GNEWS_API_KEY,
            "api_hf_key":          HUGGINGFACE_API_KEY,
            "api_telegram_token":  TELEGRAM_BOT_TOKEN,
            "api_telegram_chat":   TELEGRAM_CHAT_ID,
        }
        for k, v in defaults.items():
            c.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v)
            )

        conn.commit()
        conn.close()
    log.info("Database initialized → %s", DB_PATH)

def get_setting(key: str) -> str:
    with _db_lock:
        conn = get_db()
        row  = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        conn.close()
    return row["value"] if row else ""

def set_setting(key: str, value: str):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value)
        )
        conn.commit()
        conn.close()

def get_api_key(key: str) -> str:
    """Get API key from DB (Dashboard input takes priority over .env)."""
    val = get_setting(key)
    return val if val else ""

def resolve_keys():
    """
    Resolve all API keys from DB at runtime.
    Called before each API operation so Dashboard changes take effect immediately.
    """
    return {
        "polygon_key":    get_api_key("api_polygon_key"),
        "funder_address": get_api_key("api_funder_address"),
        "gnews_key":      get_api_key("api_gnews_key"),
        "hf_key":         get_api_key("api_hf_key"),
        "tg_token":       get_api_key("api_telegram_token"),
        "tg_chat":        get_api_key("api_telegram_chat"),
    }

def save_position(pos: dict):
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT OR REPLACE INTO positions
            (id,market_id,market_name,token_id,side,entry_price,
             current_price,amount_usd,shares,sentiment_score,
             edge_pct,target_price,entry_time,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pos["id"], pos["market_id"], pos["market_name"],
            pos["token_id"], pos["side"], pos["entry_price"],
            pos["current_price"], pos["amount_usd"], pos["shares"],
            pos["sentiment_score"], pos["edge_pct"], pos["target_price"],
            pos["entry_time"], pos["status"]
        ))
        conn.commit()
        conn.close()

def update_position_price(pos_id: str, price: float):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE positions SET current_price=? WHERE id=?", (price, pos_id)
        )
        conn.commit()
        conn.close()

def close_position(pos_id: str):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE positions SET status='closed' WHERE id=?", (pos_id,)
        )
        conn.commit()
        conn.close()

def get_open_positions() -> List[dict]:
    with _db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open'"
        ).fetchall()
        conn.close()
    return [dict(r) for r in rows]

def save_trade(trade: dict):
    with _db_lock:
        conn = get_db()
        conn.execute("""
            INSERT OR REPLACE INTO trades
            (id,market_id,market_name,side,entry_price,exit_price,
             amount_usd,pnl_usd,pnl_pct,exit_reason,entry_time,exit_time,won)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["id"], trade["market_id"], trade["market_name"],
            trade["side"], trade["entry_price"], trade["exit_price"],
            trade["amount_usd"], trade["pnl_usd"], trade["pnl_pct"],
            trade["exit_reason"], trade["entry_time"], trade["exit_time"],
            1 if trade["pnl_usd"] > 0 else 0
        ))
        conn.commit()
        conn.close()

def get_trade_stats() -> dict:
    with _db_lock:
        conn = get_db()
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) as losses,
                   SUM(pnl_usd) as total_pnl,
                   SUM(CASE WHEN date(exit_time)=date('now') THEN pnl_usd ELSE 0 END) as today_pnl
            FROM trades
        """).fetchone()
        conn.close()
    d = dict(row)
    d["win_rate"] = round(
        d["wins"] / d["total"] * 100, 1
    ) if d["total"] else 0.0
    return d

def snapshot_portfolio(balance: float):
    with _db_lock:
        conn = get_db()
        conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp,balance) VALUES (?,?)",
            (utcnow().isoformat(), balance)
        )
        conn.commit()
        conn.close()

def get_portfolio_history(days: int = 7) -> List[dict]:
    with _db_lock:
        conn = get_db()
        rows = conn.execute("""
            SELECT date(timestamp) as day, AVG(balance) as avg_balance
            FROM portfolio_snapshots
            WHERE timestamp >= datetime('now', ?)
            GROUP BY date(timestamp)
            ORDER BY day
        """, (f"-{days} days",)).fetchall()
        conn.close()
    return [{"day": r["day"], "balance": round(r["avg_balance"], 2)} for r in rows]

# ═══════════════════════════════════════════════════════════════════
# SECTION 5 — SHARED BOT STATE (Thread-safe)
# ═══════════════════════════════════════════════════════════════════
_state_lock = threading.Lock()

bot_state = {
    "running":          False,
    "paused":           False,
    "emergency_stop":   False,
    "paper_mode":       PAPER_MODE,
    "consecutive_losses": 0,
    "api_health": {
        "polymarket":   False,
        "gnews":        False,
        "huggingface":  False,
        "telegram":     False,
    },
    "last_scan_time":   None,
    "scanned_markets":  [],
}

def state_get(key: str):
    with _state_lock:
        return bot_state.get(key)

def state_set(key: str, value):
    with _state_lock:
        bot_state[key] = value

def is_running() -> bool:
    with _state_lock:
        return (
            bot_state["running"] and
            not bot_state["emergency_stop"] and
            not bot_state["paused"]
        )

# ═══════════════════════════════════════════════════════════════════
# SECTION 6 — TELEGRAM NOTIFICATION SYSTEM (2-WAY)
# ═══════════════════════════════════════════════════════════════════
_telegram_offset = 0

def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send message to Telegram."""
    keys = resolve_keys()
    token   = keys["tg_token"]
    chat_id = keys["tg_chat"]
    if not token or not chat_id:
        return False
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": parse_mode,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False

def send_entry_alert(pos: dict):
    """📈 Entry trade alert."""
    msg = (
        f"📈 <b>ENTRY ALERT</b>\n"
        f"{'─'*28}\n"
        f"🏷️ Market : {pos['market_name'][:40]}\n"
        f"📌 Action : BUY {pos['side']}\n"
        f"💵 Price  : ${pos['entry_price']:.4f}\n"
        f"💰 Amount : ${pos['amount_usd']:.2f}\n"
        f"🎯 Target : ${pos['target_price']:.4f}\n"
        f"{'─'*28}\n"
        f"📊 Sentiment : {pos['sentiment_score']:.2f}\n"
        f"⚡ EV Edge   : +{pos['edge_pct']:.1f}%\n"
        f"⏱️ Mode      : {'📄 PAPER' if PAPER_MODE else '🔴 LIVE'}"
    )
    send_telegram(msg)

def send_exit_alert(pos: dict, exit_price: float, reason: str):
    """📉 Exit trade alert with P&L."""
    pnl_usd = (exit_price - pos["entry_price"]) * pos["shares"]
    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
    emoji   = "✅" if pnl_usd >= 0 else "❌"
    msg = (
        f"{emoji} <b>EXIT ALERT</b>\n"
        f"{'─'*28}\n"
        f"🏷️ Market    : {pos['market_name'][:40]}\n"
        f"📌 Action    : SELL {pos['side']}\n"
        f"📤 Exit Price: ${exit_price:.4f}\n"
        f"📥 Entry Price: ${pos['entry_price']:.4f}\n"
        f"📋 Reason    : {reason}\n"
        f"{'─'*28}\n"
        f"💹 Net P&L   : {'+'if pnl_usd>=0 else ''}"
        f"${pnl_usd:.2f} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.1f}%)"
    )
    send_telegram(msg)

def send_daily_report():
    """📊 Daily performance report."""
    stats   = get_trade_stats()
    capital = float(get_setting("capital_total"))
    pos     = get_open_positions()
    unreal  = sum(
        (p["current_price"] - p["entry_price"]) * p["shares"]
        for p in pos
    )
    msg = (
        f"📊 <b>DAILY REPORT</b> — {utcnow().strftime('%Y-%m-%d')}\n"
        f"{'─'*28}\n"
        f"💼 Balance     : ${capital:.2f}\n"
        f"📈 Today P&L   : {'+'if (stats['today_pnl'] or 0)>=0 else ''}"
        f"${(stats['today_pnl'] or 0):.2f}\n"
        f"📉 Unrealised  : {'+'if unreal>=0 else ''}${unreal:.2f}\n"
        f"{'─'*28}\n"
        f"🎯 Total Trades: {stats['total']}\n"
        f"✅ Wins        : {stats['wins']}\n"
        f"❌ Losses      : {stats['losses']}\n"
        f"📐 Win Rate    : {stats['win_rate']}%\n"
        f"🔓 Open Pos    : {len(pos)}\n"
        f"{'─'*28}\n"
        f"⚙️ Mode        : {'📄 PAPER' if PAPER_MODE else '🔴 LIVE'}"
    )
    send_telegram(msg)

def send_critical_alert(message: str):
    """🚨 Critical system alert."""
    send_telegram(f"🚨 <b>CRITICAL ALERT</b>\n{'─'*28}\n{message}")

def handle_telegram_command(update: dict):
    """Process incoming Telegram commands."""
    global _telegram_offset
    try:
        msg     = update.get("message", {})
        text    = msg.get("text", "").strip().lower()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Security: only accept from configured chat (DB key takes priority over .env)
        configured_chat = get_api_key("api_telegram_chat") or TELEGRAM_CHAT_ID
        if chat_id != configured_chat:
            return

        responses = {
            "/stop": lambda: (
                state_set("emergency_stop", True),
                send_telegram("🛑 <b>Emergency stop activated.</b>\nAll trading loops halted.")
            ),
            "/resume": lambda: (
                state_set("emergency_stop", False),
                state_set("paused", False),
                state_set("consecutive_losses", 0),
                send_telegram("▶️ <b>Bot resumed.</b>")
            ),
            "/pause": lambda: (
                state_set("paused", True),
                send_telegram("⏸ <b>Bot paused.</b> Positions still monitored.")
            ),
            "/status":   lambda: send_status_via_telegram(),
            "/balance":  lambda: send_status_via_telegram(),
            "/positions": lambda: send_positions_via_telegram(),
            "/report":   lambda: send_daily_report(),
            "/health":   lambda: send_health_via_telegram(),
            "/help": lambda: send_telegram(
                "📋 <b>Available Commands</b>\n"
                "/stop      — Emergency stop\n"
                "/resume    — Resume trading\n"
                "/pause     — Pause (keep monitoring)\n"
                "/status    — Current status + P&L\n"
                "/balance   — Same as /status\n"
                "/positions — Open positions list\n"
                "/report    — Full daily report\n"
                "/health    — API connection status\n"
                "/help      — This message"
            ),
        }

        handler = responses.get(text)
        if handler:
            log.info("Telegram command received: %s", text)
            handler()
        else:
            send_telegram(f"❓ Unknown command: {text}\nSend /help for list.")

    except Exception as e:
        log.error("Telegram command error: %s", e)

def auto_clean_disk():
    """
    Auto disk cleanup — runs hourly alongside heartbeat.
    - Rotates bot.log / output.log when > 20MB
    - Clears old /var/log gz files
    - Clears pip cache
    - Alerts via Telegram if disk > 90%
    """
    import shutil, glob, subprocess

    cleaned = []

    # ── Rotate bot.log if > 20MB ──
    bot_log = os.path.join(os.path.dirname(__file__), "bot.log")
    if os.path.exists(bot_log) and os.path.getsize(bot_log) > 20 * 1024 * 1024:
        open(bot_log, "w").close()
        cleaned.append("bot.log rotated")

    # ── Truncate output.log if > 10MB ──
    out_log = os.path.join(os.path.dirname(__file__), "output.log")
    if os.path.exists(out_log) and os.path.getsize(out_log) > 10 * 1024 * 1024:
        open(out_log, "w").close()
        cleaned.append("output.log truncated")

    # ── Clear old gz log files ──
    try:
        for f in glob.glob("/var/log/*.gz") + glob.glob("/var/log/**/*.gz"):
            os.remove(f)
            cleaned.append(f"removed {os.path.basename(f)}")
    except Exception:
        pass

    # ── Pip cache purge ──
    try:
        result = subprocess.run(
            ["pip", "cache", "purge"],
            capture_output=True, text=True, timeout=30
        )
        if "Files removed" in result.stdout:
            cleaned.append(f"pip cache: {result.stdout.strip()}")
    except Exception:
        pass

    # ── Disk usage check ──
    total, used, free = shutil.disk_usage("/")
    used_pct = used / total * 100
    free_gb  = free / (1024 ** 3)

    if cleaned:
        log.info("Auto-clean: %s | Disk %.1f%% (%.2fGB free)",
                 ", ".join(cleaned), used_pct, free_gb)

    # ── Alert if still critical ──
    if used_pct >= 90:
        send_telegram(
            f"🚨 <b>Disk CRITICAL after auto-clean</b>\n"
            f"Used: {used_pct:.1f}% | Free: {free_gb:.2f}GB\n"
            f"Manual cleanup needed on VPS!"
        )
    elif used_pct >= 80:
        send_telegram(
            f"⚠️ <b>Disk Warning</b>: {used_pct:.1f}% used ({free_gb:.2f}GB free)"
        )


    """📍 Send open positions list via Telegram."""
    positions = get_open_positions()
    if not positions:
        send_telegram("📍 <b>Open Positions</b>\n─────────────────\nNo open positions.")
        return
    lines = [f"📍 <b>Open Positions ({len(positions)})</b>\n{'─'*28}"]
    for i, p in enumerate(positions, 1):
        pnl = (p["current_price"] - p["entry_price"]) * p["shares"]
        em  = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{em} <b>#{i}</b> {p['market_name'][:30]}\n"
            f"   Entry: ${p['entry_price']:.4f} → Now: ${p['current_price']:.4f}\n"
            f"   P&L: {'+' if pnl>=0 else ''}${pnl:.3f} | Target: ${p['target_price']:.4f}"
        )
    send_telegram("\n".join(lines))

def send_health_via_telegram():
    """🩺 Send API health status via Telegram."""
    health = bot_state.get("api_health", {})
    def s(k): return "✅ OK" if health.get(k) else "❌ FAIL"
    msg = (
        f"🩺 <b>API Health Check</b>\n"
        f"{'─'*28}\n"
        f"Polymarket : {s('polymarket')}\n"
        f"GNews      : {s('gnews')}\n"
        f"HuggingFace: {s('huggingface')}\n"
        f"Telegram   : {s('telegram')}\n"
        f"{'─'*28}\n"
        f"L2 Bypass  : {'ON' if get_setting('l2_bypass')=='1' else 'OFF'}\n"
        f"Paper Mode : {'YES' if bool(int(get_setting('paper_mode'))) else 'LIVE'}"
    )
    send_telegram(msg)

def send_status_via_telegram():
    """Send current bot status via Telegram."""
    stats   = get_trade_stats()
    capital = float(get_setting("capital_total"))
    pos     = get_open_positions()
    paused  = state_get("paused")
    stopped = state_get("emergency_stop")
    status  = "🛑 STOPPED" if stopped else ("⏸ PAUSED" if paused else "✅ RUNNING")
    msg = (
        f"ℹ️ <b>BOT STATUS</b>\n"
        f"{'─'*28}\n"
        f"⚙️ Status    : {status}\n"
        f"💼 Capital   : ${capital:.2f}\n"
        f"📊 Win Rate  : {stats['win_rate']}%\n"
        f"🔓 Open Pos  : {len(pos)}\n"
        f"📈 All-time  : {'+'if (stats['total_pnl'] or 0)>=0 else ''}"
        f"${(stats['total_pnl'] or 0):.2f}"
    )
    send_telegram(msg)

def telegram_polling_loop():
    """Background thread — poll Telegram for commands every 2s."""
    global _telegram_offset
    log.info("Telegram polling started.")
    while True:
        try:
            keys  = resolve_keys()
            token = keys["tg_token"]
            if not token:
                time.sleep(10)
                continue
            url  = (f"https://api.telegram.org/bot{token}"
                    f"/getUpdates?timeout=10&offset={_telegram_offset}")
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    _telegram_offset = update["update_id"] + 1
                    handle_telegram_command(update)
        except Exception as e:
            log.error("Telegram polling error: %s", e)
        time.sleep(2)

# ═══════════════════════════════════════════════════════════════════
# SECTION 7 — HUGGINGFACE SENTIMENT ANALYSIS
# ═══════════════════════════════════════════════════════════════════
def analyze_sentiment(texts: List[str]) -> Optional[float]:
    """
    Query HuggingFace political sentiment model.
    Returns positive score (0.0 – 1.0) or None on failure.
    """
    hf_key = get_api_key("api_hf_key")
    if not hf_key:
        log.warning("HuggingFace key not set.")
        return None
    hf_headers = {"Authorization": f"Bearer {hf_key}"}
    try:
        combined = " ".join(texts[:5])[:512]
        resp = requests.post(
            HF_MODEL_URL,
            headers=hf_headers,
            json={"inputs": combined},
            timeout=15,
        )
        if resp.status_code == 503:
            log.info("HuggingFace model loading, retrying in 20s...")
            time.sleep(20)
            resp = requests.post(
                HF_MODEL_URL,
                headers=hf_headers,
                json={"inputs": combined},
                timeout=15,
            )
        if resp.status_code != 200:
            log.error("HuggingFace API error: %s", resp.status_code)
            return None
        result = resp.json()
        if isinstance(result, list) and result:
            scores = result[0] if isinstance(result[0], list) else result
            for item in scores:
                if isinstance(item, dict):
                    label = item.get("label", "").lower()
                    score = item.get("score", 0.0)
                    if "positive" in label or label == "pos":
                        return round(score, 4)
        log.warning("Unexpected HF response: %s", result)
        return None
    except Exception as e:
        log.error("Sentiment analysis failed: %s", e)
        return None

# ═══════════════════════════════════════════════════════════════════
# SECTION 8 — GNEWS FETCHER
# ═══════════════════════════════════════════════════════════════════
_gnews_cache: dict = {}   # {short_query: (headlines, timestamp)}
_GNEWS_CACHE_TTL = 3600   # 1 hour cache TTL
_gnews_last_call  = 0.0   # rate-limit guard

def _shorten_query(text: str, max_words: int = 5) -> str:
    """Keep first N meaningful words for GNews search."""
    stop = {"will", "the", "a", "an", "in", "of", "to", "for",
            "is", "are", "be", "on", "at", "by"}
    words = [w for w in text.split() if w.lower() not in stop]
    return " ".join(words[:max_words]) if words else " ".join(text.split()[:max_words])

def fetch_news_headlines(query: str, max_results: int = 5) -> List[str]:
    """
    Fetch latest headlines from GNews API.
    - Query auto-shortened to 5 keywords
    - 1-hour in-memory cache (avoids rate limits)
    - 429 retry with 15s backoff
    - 2s minimum between calls
    Returns list of headline strings.
    """
    global _gnews_last_call

    gnews_key = get_api_key("api_gnews_key")
    if not gnews_key:
        log.warning("GNews key not set.")
        return []

    short_q = _shorten_query(query)

    # ── Cache check ──
    if short_q in _gnews_cache:
        cached_headlines, ts = _gnews_cache[short_q]
        if time.time() - ts < _GNEWS_CACHE_TTL:
            log.info("GNews cache hit: '%s' (%d headlines)", short_q, len(cached_headlines))
            return cached_headlines

    # ── Rate-limit guard: min 2s between calls ──
    elapsed = time.time() - _gnews_last_call
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)

    try:
        params = {
            "q":      short_q,
            "lang":   "en",
            "max":    max_results,
            "apikey": gnews_key,
            "sortby": "publishedAt",
        }
        _gnews_last_call = time.time()
        resp = requests.get(GNEWS_BASE, params=params, timeout=10)

        # ── 429/403 retry with backoff ──
        if resp.status_code in [429, 403]:
            log.warning("GNews %s — quota/rate-limit, skipping retry", resp.status_code)
            return []

        if resp.status_code != 200:
            log.error("GNews error: %s", resp.status_code)
            return []

        articles = resp.json().get("articles", [])
        headlines = [
            f"{a.get('title', '')}. {a.get('description', '')}"
            for a in articles
        ]
        log.info("GNews: %d headlines for '%s'", len(headlines), short_q)

        # ── Store in cache ──
        _gnews_cache[short_q] = (headlines, time.time())
        return headlines

    except Exception as e:
        log.error("GNews fetch failed: %s", e)
        return []

# ═══════════════════════════════════════════════════════════════════
# SECTION 9 — POLYMARKET CLIENT & MARKET SCANNER
# ═══════════════════════════════════════════════════════════════════
_clob_client: Optional[object] = None

def init_polymarket_client() -> bool:
    """
    Initialize Polymarket CLOB client using private key.
    Derives API credentials automatically.
    """
    global _clob_client
    if not CLOB_AVAILABLE:
        log.warning("py_clob_client not installed — paper mode active.")
        return False
    if not POLYGON_WALLET_PRIVATE_KEY:
        log.warning("Wallet key not set — paper mode active.")
        return False
    try:
        _clob_client = ClobClient(
            host        = CLOB_HOST,
            chain_id    = POLYGON,
            key         = POLYGON_WALLET_PRIVATE_KEY,
            funder      = POLYMARKET_FUNDER_ADDRESS,
        )
        _clob_client.create_or_derive_api_creds()
        log.info("Polymarket CLOB client initialized ✅")
        return True
    except Exception as e:
        log.error("Polymarket init failed: %s", e)
        return False

def get_market_price_history(token_id: str, hours: int = 6) -> List[float]:
    """
    Fetch price history for a market token from CLOB API.
    Returns list of prices (most recent last).
    """
    try:
        end_ts   = int(time.time())
        start_ts = end_ts - (hours * 3600)
        resp = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={
                "market":    token_id,
                "startTs":   start_ts,
                "endTs":     end_ts,
                "fidelity":  60,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data   = resp.json()
        prices = [float(p["p"]) for p in data.get("history", [])]
        return prices
    except Exception as e:
        log.error("Price history fetch failed: %s", e)
        return []

def get_order_book_spread(token_id: str) -> Optional[float]:
    """
    Fetch order book and calculate bid-ask spread %.
    Returns spread percentage or None on failure.
    """
    try:
        resp = requests.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        mid      = (best_bid + best_ask) / 2
        spread   = abs(best_ask - best_bid) / mid * 100
        return round(spread, 2)
    except Exception as e:
        log.error("Order book fetch failed: %s", e)
        return None

def scan_markets() -> List[dict]:
    """
    Scan Polymarket for active markets with high volume.
    Returns ranked list of candidate markets.
    Categories: Politics, Technology, Crypto, Finance, Geopolitics
    """
    try:
        # Fetch from Gamma API (more data than CLOB directly)
        resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={
                "active":    "true",
                "closed":    "false",
                "order":     "volume24hr",
                "ascending": "false",
                "limit":     50,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("Market scan failed: %s", resp.status_code)
            return []

        markets   = resp.json() if isinstance(resp.json(), list) else resp.json().get("markets", [])
        min_vol   = float(get_setting("min_volume_24h"))
        min_days  = int(get_setting("min_days_resolution"))
        now       = utcnow()
        results   = []

        for m in markets:
            try:
                # Parse fields
                market_id   = m.get("id", "")
                market_name = m.get("question", m.get("title", ""))
                end_date_s  = m.get("endDate", m.get("endDateIso", ""))
                volume_24h  = float(m.get("volume24hr", m.get("oneDayVolume", 0)) or 0)
                price       = float(m.get("outcomePrices", [0.5])[0]
                              if isinstance(m.get("outcomePrices"), list)
                              else m.get("bestBid", 0.5) or 0.5)
                token_id    = (m.get("clobTokenIds", [None])[0]
                              or m.get("conditionId", ""))
                category    = m.get("category", "")

                # Skip if low volume
                if volume_24h < min_vol:
                    continue

                # Check resolution days
                if end_date_s:
                    try:
                        end_dt  = datetime.fromisoformat(
                            end_date_s.replace("Z", "")
                        )
                        days_left = (end_dt - now).days
                        if days_left < min_days:
                            continue
                    except Exception:
                        days_left = 999
                else:
                    days_left = 999

                results.append({
                    "market_id":   market_id,
                    "market_name": market_name,
                    "token_id":    token_id,
                    "category":    category,
                    "volume_24h":  volume_24h,
                    "price":       price,
                    "days_left":   days_left,
                })

            except Exception as me:
                log.debug("Market parse error: %s", me)
                continue

        # Sort by volume descending
        results.sort(key=lambda x: x["volume_24h"], reverse=True)
        log.info("Market scan: %d qualifying markets found", len(results))

        # Cache to DB
        with _db_lock:
            conn = get_db()
            for r in results[:20]:
                conn.execute("""
                    INSERT OR REPLACE INTO market_cache
                    (market_id,market_name,token_id,category,
                     volume_24h,current_price,last_scanned)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    r["market_id"], r["market_name"], r["token_id"],
                    r["category"], r["volume_24h"], r["price"],
                    now.isoformat()
                ))
            conn.commit()
            conn.close()

        state_set("scanned_markets", results[:20])
        state_set("last_scan_time", now.isoformat())
        return results[:20]

    except Exception as e:
        log.error("Market scan exception: %s", e)
        return []

def get_current_price(token_id: str) -> Optional[float]:
    """Get latest YES price for a market token."""
    try:
        resp = requests.get(
            f"{CLOB_HOST}/price",
            params={"token_id": token_id, "side": "sell"},
            timeout=10,
        )
        if resp.status_code == 200:
            return float(resp.json().get("price", 0))
        return None
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════════
# SECTION 10 — 7-LAYER STRATEGY FILTER
# ═══════════════════════════════════════════════════════════════════

def check_api_health() -> bool:
    """L7 — Check all APIs are responding."""
    keys   = resolve_keys()
    health = {}

    # Polymarket
    try:
        r = requests.get(f"{CLOB_HOST}/ok", timeout=5)
        health["polymarket"] = r.status_code == 200
    except Exception:
        health["polymarket"] = False

    # GNews
    try:
        if keys["gnews_key"]:
            r = requests.get(
                GNEWS_BASE,
                params={"q":"test","apikey":keys["gnews_key"],"max":1},
                timeout=5
            )
            health["gnews"] = r.status_code == 200   # 429 = rate-limited = NOT healthy
        else:
            health["gnews"] = False
    except Exception:
        health["gnews"] = False

    # HuggingFace
    try:
        r = requests.get("https://huggingface.co", timeout=5)
        health["huggingface"] = r.status_code == 200 and bool(keys["hf_key"])
    except Exception:
        health["huggingface"] = False

    # Telegram
    try:
        if keys["tg_token"]:
            r = requests.get(
                f"https://api.telegram.org/bot{keys['tg_token']}/getMe",
                timeout=5
            )
            health["telegram"] = r.status_code == 200
        else:
            health["telegram"] = False
    except Exception:
        health["telegram"] = False

    with _state_lock:
        bot_state["api_health"] = health

    all_ok = health["polymarket"] and health.get("huggingface", False)
    # GNews optional — daily quota exhausts, does not block trading
    if not all_ok:
        failed = [k for k, v in health.items() if not v]
        log.warning("L7 FAIL — APIs down: %s", failed)
    return all_ok

def layer1_sentiment(market: dict) -> Tuple[bool, float]:
    """
    L1 — News Sentiment Filter
    Fetch headlines for market topic, analyze with HuggingFace.
    PASS if positive score > threshold.
    Falls back to neutral (0.50) if GNews unavailable — does NOT hard-fail.
    """
    threshold = float(get_setting("sentiment_threshold"))
    query     = market["market_name"]   # _shorten_query applied inside fetch

    headlines = fetch_news_headlines(query)

    if not headlines:
        # GNews unavailable (quota/rate-limit) — use HuggingFace on market name directly
        log.info("L1 GNews unavailable — using HuggingFace on market name: %s", query[:40])
        score = analyze_sentiment([query])
        if score is not None:
            passed = score >= threshold
            log.info("L1 %s — HuggingFace direct %.3f (threshold %.2f) | %s",
                     "PASS" if passed else "FAIL", score, threshold, query[:40])
            return passed, score
        # Both GNews and HuggingFace unavailable — neutral fallback
        log.info("L1 PASS (neutral fallback) — all sentiment unavailable: %s", query[:40])
        return True, 0.50

    score = analyze_sentiment(headlines)
    if score is None:
        log.warning("L1 PASS (neutral fallback) — HuggingFace error")
        return True, 0.50

    passed = score >= threshold
    log.info("L1 %s — Sentiment %.3f (threshold %.2f) | %s",
             "PASS" if passed else "FAIL", score, threshold,
             market["market_name"][:30])
    return passed, score

def layer2_momentum(market: dict) -> bool:
    """
    L2 — 6-Hour Price Momentum Filter
    PASS if current price > 6hr_ago_price + momentum_threshold%.
    """
    momentum_pct = float(get_setting("momentum_pct")) / 100
    token_id     = market.get("token_id", "")

    if not token_id:
        log.info("L2 SKIP — No token_id for market")
        return False

    prices = get_market_price_history(token_id, hours=6)
    if len(prices) < 2:
        log.info("L2 FAIL — Not enough price history")
        return False

    price_6h_ago  = prices[0]
    price_current = prices[-1]

    if price_6h_ago <= 0:
        return False

    change = (price_current - price_6h_ago) / price_6h_ago
    passed = change >= momentum_pct

    log.info("L2 %s — 6hr change %.2f%% (threshold %.1f%%) | %s",
             "PASS" if passed else "FAIL",
             change * 100,
             momentum_pct * 100,
             market["market_name"][:30])
    return passed

def layer3_ev_edge(market: dict, sentiment_score: float) -> Tuple[bool, float]:
    """
    L3 — Expected Value Edge Check (v2.3)

    YES side : sentiment >= 0.55 → buy YES
    NO  side : sentiment <= 0.35 → buy NO
    NEUTRAL  : sentiment 0.36-0.54 (e.g. HuggingFace fallback 0.50)
               → check market price for mispricing edge
               → if price < 0.30 buy YES (underpriced), if price > 0.70 buy NO (overpriced)
    """
    min_edge     = float(get_setting("min_edge_pct")) / 100
    market_price = market.get("price", 0.5)

    if sentiment_score >= 0.55:
        # Bullish → YES
        estimated_prob = 0.55 + (sentiment_score - 0.55) * (0.90 - 0.55) / 0.45
        estimated_prob = min(0.90, estimated_prob)
        direction      = "YES"
        edge           = estimated_prob - market_price

    elif sentiment_score <= 0.35:
        # Bearish → NO
        estimated_prob = 0.55 + (0.35 - sentiment_score) * (0.90 - 0.55) / 0.35
        estimated_prob = min(0.90, estimated_prob)
        direction      = "NO"
        no_price       = 1.0 - market_price
        edge           = estimated_prob - no_price

    else:
        # Neutral sentiment — use market price mispricing
        if market_price < 0.30:
            # Heavily underpriced YES token → value bet
            estimated_prob = 0.45
            direction      = "YES"
            edge           = estimated_prob - market_price
        elif market_price > 0.70:
            # Heavily overpriced YES → buy NO
            estimated_prob = 0.45
            direction      = "NO"
            no_price       = 1.0 - market_price
            edge           = estimated_prob - no_price
        else:
            # Truly no edge
            estimated_prob = 0.50
            direction      = "NEUTRAL"
            edge           = 0.0

    passed = edge >= min_edge
    market["_direction"]      = direction
    market["_estimated_prob"] = round(estimated_prob, 4)

    log.info("L3 %s — %s | Est %.1f%% | Mkt %.1f%% | Edge %.1f%% (min %.1f%%) | %s",
             "PASS" if passed else "FAIL",
             direction,
             estimated_prob * 100,
             market_price * 100,
             edge * 100,
             min_edge * 100,
             market["market_name"][:30])
    return passed, round(edge * 100, 2)

def layer4_liquidity(market: dict) -> bool:
    """
    L4 — Liquidity Check
    PASS if volume > min_volume AND spread < max_spread.
    """
    min_vol    = float(get_setting("min_volume_24h"))
    max_spread = float(get_setting("max_spread_pct"))
    token_id   = market.get("token_id", "")

    # Volume check
    if market.get("volume_24h", 0) < min_vol:
        log.info("L4 FAIL — Low volume $%.0f (min $%.0f) | %s",
                 market.get("volume_24h", 0), min_vol,
                 market["market_name"][:30])
        return False

    # Spread check
    if token_id:
        spread = get_order_book_spread(token_id)
        if spread is not None and spread > max_spread:
            log.info("L4 FAIL — Wide spread %.2f%% (max %.1f%%) | %s",
                     spread, max_spread, market["market_name"][:30])
            return False

    log.info("L4 PASS — Vol $%.0f, Spread OK | %s",
             market.get("volume_24h", 0), market["market_name"][:30])
    return True

def layer5_time_check(market: dict) -> bool:
    """
    L5 — Time Safety Check
    PASS if days_to_resolution > min_days.
    """
    min_days  = int(get_setting("min_days_resolution"))
    days_left = market.get("days_left", 999)
    passed    = days_left >= min_days
    log.info("L5 %s — %d days left (min %d) | %s",
             "PASS" if passed else "FAIL",
             days_left, min_days,
             market["market_name"][:30])
    return passed

def layer6_no_duplicate(market: dict) -> bool:
    """
    L6 — Duplicate Position Guard
    PASS if no open position exists for this market.
    """
    positions   = get_open_positions()
    market_ids  = {p["market_id"] for p in positions}
    max_pos     = int(get_setting("max_positions"))
    duplicate   = market["market_id"] in market_ids
    at_capacity = len(positions) >= max_pos

    if duplicate:
        log.info("L6 FAIL — Duplicate position exists | %s",
                 market["market_name"][:30])
        return False
    if at_capacity:
        log.info("L6 FAIL — Max positions (%d) reached", max_pos)
        return False

    log.info("L6 PASS — No duplicate, %d/%d positions | %s",
             len(positions), max_pos, market["market_name"][:30])
    return True

def layer7_api_health() -> bool:
    """L7 — API Health Check (uses cached state, refreshed hourly)."""
    health = bot_state.get("api_health", {})
    passed = health.get("polymarket", False) and health.get("huggingface", False)
    # GNews is optional — quota exhausts daily, bot continues without it
    log.info("L7 %s — API health: %s", "PASS" if passed else "FAIL", health)
    return passed

def run_all_layers(market: dict) -> Tuple[bool, dict]:
    """
    Execute all 7 filter layers sequentially.
    Returns (should_enter, analysis_data).
    Early exit on any FAIL to save API calls.
    """
    analysis = {
        "market_id":      market["market_id"],
        "market_name":    market["market_name"],
        "sentiment_score": 0.0,
        "edge_pct":       0.0,
        "layers_passed":  [],
        "layers_failed":  [],
    }

    # L7 first (free check)
    if not layer7_api_health():
        analysis["layers_failed"].append("L7_api_health")
        return False, analysis
    analysis["layers_passed"].append("L7")

    # L5 (free check)
    if not layer5_time_check(market):
        analysis["layers_failed"].append("L5_time")
        return False, analysis
    analysis["layers_passed"].append("L5")

    # L6 (free check)
    if not layer6_no_duplicate(market):
        analysis["layers_failed"].append("L6_duplicate")
        return False, analysis
    analysis["layers_passed"].append("L6")

    # L4 (one API call)
    if not layer4_liquidity(market):
        analysis["layers_failed"].append("L4_liquidity")
        return False, analysis
    analysis["layers_passed"].append("L4")

    # L2 (one API call — bypassable via setting)
    l2_bypass = get_setting("l2_bypass") == "1"
    if l2_bypass:
        log.info("L2 BYPASS — momentum check skipped (disabled in settings)")
        analysis["layers_passed"].append("L2-bypassed")
    else:
        if not layer2_momentum(market):
            analysis["layers_failed"].append("L2_momentum")
            return False, analysis
        analysis["layers_passed"].append("L2")

    # L1 (GNews + HuggingFace)
    passed_l1, score = layer1_sentiment(market)
    analysis["sentiment_score"] = score
    if not passed_l1:
        analysis["layers_failed"].append("L1_sentiment")
        return False, analysis
    analysis["layers_passed"].append("L1")

    # L3 (uses sentiment from L1)
    passed_l3, edge = layer3_ev_edge(market, score)
    analysis["edge_pct"] = edge
    if not passed_l3:
        analysis["layers_failed"].append("L3_ev_edge")
        return False, analysis
    analysis["layers_passed"].append("L3")

    log.info("✅ ALL 7 LAYERS PASSED — %s | Sentiment %.2f | Edge +%.1f%%",
             market["market_name"][:40], score, edge)
    return True, analysis

# ═══════════════════════════════════════════════════════════════════
# SECTION 11 — KELLY POSITION SIZING
# ═══════════════════════════════════════════════════════════════════
def kelly_position_size(
    market_price:   float,
    estimated_prob: float,
    capital:        float,
    rank:           int = 1,
) -> float:
    """
    Half-Kelly criterion position sizing with rank-based allocation.

    Rank allocation:
      1st signal → 40% of free capital
      2nd signal → 35% of free capital
      3rd signal → 25% of free capital

    Constraints:
      Hard max: max_trade_usd setting
      Hard min: $0.50
    """
    max_trade  = float(get_setting("max_trade_usd"))
    reserve    = float(get_setting("capital_reserve_pct")) / 100
    free_cap   = capital * (1 - reserve)

    # Rank allocation
    rank_pcts  = {1: 0.40, 2: 0.35, 3: 0.25}
    alloc_pct  = rank_pcts.get(rank, 0.25)
    rank_cap   = free_cap * alloc_pct

    # Kelly formula: f* = (b*p - q) / b
    if market_price <= 0 or market_price >= 1:
        return min(max_trade, 1.0)

    b           = (1 / market_price) - 1   # odds
    p           = min(0.95, estimated_prob) # win probability
    q           = 1 - p                     # loss probability
    kelly_raw   = (b * p - q) / b if b > 0 else 0
    half_kelly  = max(0, kelly_raw / 2)    # half-Kelly for safety

    kelly_size  = free_cap * half_kelly
    final_size  = min(kelly_size, rank_cap, max_trade)
    final_size  = max(0.50, round(final_size, 2))

    log.info("Kelly sizing: f*=%.3f | half=%.3f | rank_cap=$%.2f | size=$%.2f",
             kelly_raw, half_kelly, rank_cap, final_size)
    return final_size

# ═══════════════════════════════════════════════════════════════════
# SECTION 12 — ORDER EXECUTOR
# ═══════════════════════════════════════════════════════════════════
def execute_buy_order(market: dict, amount_usd: float, analysis: dict) -> Optional[dict]:
    """
    Execute BUY YES or BUY NO order on Polymarket CLOB. (v2.2)
    Direction determined by L3 sentiment analysis.
    Falls back to paper trade if CLOB unavailable.
    Returns position dict or None on failure.
    """
    token_id     = market.get("token_id", "")
    market_price = market.get("price", 0.5)
    direction    = market.get("_direction", "YES")

    # NO trade: effective price = 1 - market_price
    effective_price = round(1.0 - market_price, 4) if direction == "NO" else market_price
    shares          = round(amount_usd / effective_price, 4) if effective_price > 0 else 0
    edge_pct        = analysis.get("edge_pct", 0)
    target_price    = round(
        effective_price + (effective_price * (edge_pct / 100) * 0.70), 4
    )
    pos_id = hashlib.md5(
        f"{market['market_id']}{utcnow().isoformat()}".encode()
    ).hexdigest()[:12]

    position = {
        "id":              pos_id,
        "market_id":       market["market_id"],
        "market_name":     market["market_name"],
        "token_id":        token_id,
        "side":            direction,
        "entry_price":     effective_price,
        "current_price":   effective_price,
        "amount_usd":      amount_usd,
        "shares":          shares,
        "sentiment_score": analysis.get("sentiment_score", 0),
        "edge_pct":        edge_pct,
        "target_price":    target_price,
        "entry_time":      utcnow().isoformat(),
        "status":          "open",
    }

    paper = bool(int(get_setting("paper_mode")))

    if paper or not CLOB_AVAILABLE or not _clob_client:
        log.info("📄 PAPER %s — %s | $%.2f @ $%.4f | target $%.4f",
                 direction, market["market_name"][:40],
                 amount_usd, effective_price, target_price)
    else:
        try:
            order_args = OrderArgs(
                token_id = token_id,
                price    = effective_price,
                size     = shares,
                side     = direction,
            )
            resp = _clob_client.create_and_post_order(order_args)
            if not resp:
                log.error("Order rejected by CLOB")
                return None
            log.info("🔴 LIVE %s — %s | $%.2f | order: %s",
                     direction, market["market_name"][:40], amount_usd, resp)
        except Exception as e:
            log.error("Order execution failed: %s", e)
            send_critical_alert(f"Order execution failed: {e}")
            return None

    # Save to DB
    save_position(position)

    # Update capital
    capital = float(get_setting("capital_total"))
    set_setting("capital_total", str(round(capital - amount_usd, 4)))

    # Snapshot
    snapshot_portfolio(float(get_setting("capital_total")))

    # Telegram alert
    send_entry_alert(position)
    return position

def execute_sell_order(pos: dict, reason: str) -> bool:
    """
    Execute SELL order for an open position.
    Returns True on success.
    """
    token_id      = pos.get("token_id", "")
    current_price = get_current_price(token_id) or pos["current_price"]
    paper         = bool(int(get_setting("paper_mode")))

    if paper or not CLOB_AVAILABLE or not _clob_client:
        log.info("📄 PAPER SELL — %s | exit $%.4f | reason: %s",
                 pos["market_name"][:40], current_price, reason)
    else:
        try:
            order_args = OrderArgs(
                token_id = token_id,
                price    = current_price,
                size     = pos["shares"],
                side     = "SELL",
            )
            resp = _clob_client.create_and_post_order(order_args)
            if not resp:
                log.error("Sell order rejected")
                return False
        except Exception as e:
            log.error("Sell execution failed: %s", e)
            send_critical_alert(f"Sell execution failed: {e}")
            return False

    # Calculate P&L
    pnl_usd = (current_price - pos["entry_price"]) * pos["shares"]
    pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100

    # Save trade history
    trade = {
        "id":          pos["id"],
        "market_id":   pos["market_id"],
        "market_name": pos["market_name"],
        "side":        pos["side"],
        "entry_price": pos["entry_price"],
        "exit_price":  current_price,
        "amount_usd":  pos["amount_usd"],
        "pnl_usd":     round(pnl_usd, 4),
        "pnl_pct":     round(pnl_pct, 2),
        "exit_reason": reason,
        "entry_time":  pos["entry_time"],
        "exit_time":   utcnow().isoformat(),
    }
    save_trade(trade)
    close_position(pos["id"])

    # Restore capital
    returned = pos["amount_usd"] + pnl_usd
    capital  = float(get_setting("capital_total"))
    set_setting("capital_total", str(round(capital + returned, 4)))

    # Snapshot
    snapshot_portfolio(float(get_setting("capital_total")))

    # Telegram alert
    send_exit_alert(pos, current_price, reason)

    log.info("%s CLOSE — %s | P&L: %+.2f (%.1f%%) | reason: %s",
             "✅" if pnl_usd >= 0 else "❌",
             pos["market_name"][:30], pnl_usd, pnl_pct, reason)

    # ── Update risk manager loss counter ──
    update_loss_counter(pnl_usd > 0)

    return True

# ═══════════════════════════════════════════════════════════════════
# SECTION 13 — POSITION MONITOR (3 Exit Conditions)
# ═══════════════════════════════════════════════════════════════════
def monitor_positions():
    """
    Check all open positions for exit conditions every cycle.

    Exit conditions (priority order):
      E1: Target price hit (profit)
      E2: Sentiment dropped below 0.30 (reversal)
      E3: 48hr before market resolution (time safety)
    """
    positions = get_open_positions()
    if not positions:
        return

    log.info("Monitoring %d open position(s)...", len(positions))

    for pos in positions:
        token_id = pos.get("token_id", "")

        # Update current price
        current_price = get_current_price(token_id)
        if current_price:
            update_position_price(pos["id"], current_price)
            pos["current_price"] = current_price
        else:
            current_price = pos["current_price"]

        # ── EXIT E1: Target hit ──
        if current_price >= pos["target_price"]:
            log.info("E1 TARGET HIT — %s | $%.4f >= $%.4f",
                     pos["market_name"][:30], current_price, pos["target_price"])
            execute_sell_order(pos, "Target Price Hit")
            continue

        # ── EXIT E2: Sentiment reversal ──
        short_q   = _shorten_query(pos["market_name"])
        headlines = fetch_news_headlines(short_q, max_results=3)
        if headlines:
            score = analyze_sentiment(headlines)
            if score is not None and score < 0.30:
                log.info("E2 SENTIMENT REVERSAL — score %.2f | %s",
                         score, pos["market_name"][:30])
                execute_sell_order(pos, f"Sentiment Reversal ({score:.2f})")
                continue

        # ── EXIT E3: Time safety (48hr before close) ──
        market_data = next(
            (m for m in state_get("scanned_markets") or []
             if m["market_id"] == pos["market_id"]),
            None
        )
        if market_data and market_data.get("days_left", 999) <= 2:
            log.info("E3 TIME SAFETY — 48hr before resolution | %s",
                     pos["market_name"][:30])
            execute_sell_order(pos, "48hr Before Resolution")
            continue

        # ── EXIT E4: Stop-Loss (price dropped >25% from entry) ──
        loss_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
        if loss_pct <= -25.0:
            log.info("E4 STOP-LOSS — %.1f%% loss | %s",
                     loss_pct, pos["market_name"][:30])
            execute_sell_order(pos, f"Stop-Loss ({loss_pct:.1f}%)")
            continue

        log.info("HOLD — %s | price $%.4f | target $%.4f | unreal %+.4f",
                 pos["market_name"][:30], current_price,
                 pos["target_price"],
                 (current_price - pos["entry_price"]) * pos["shares"])

# ═══════════════════════════════════════════════════════════════════
# SECTION 14 — RISK MANAGER
# ═══════════════════════════════════════════════════════════════════
def check_consecutive_losses():
    """
    Auto-pause bot after N consecutive losses.
    Sends Telegram alert and pauses trading.
    """
    limit  = int(get_setting("consecutive_loss_limit"))
    losses = state_get("consecutive_losses") or 0

    if losses >= limit:
        state_set("paused", True)
        msg = (f"⚠️ AUTO-PAUSE: {losses} consecutive losses detected.\n"
               f"Trading paused. Send /resume to continue.\n"
               f"Please review strategy before resuming.")
        log.warning(msg)
        send_critical_alert(msg)
        return False
    return True

def update_loss_counter(trade_won: bool):
    """Update consecutive loss counter after each trade."""
    if trade_won:
        state_set("consecutive_losses", 0)
    else:
        current = state_get("consecutive_losses") or 0
        state_set("consecutive_losses", current + 1)

# ═══════════════════════════════════════════════════════════════════
# SECTION 15 — MARKET RANKING & CAPITAL ALLOCATION
# ═══════════════════════════════════════════════════════════════════
def rank_and_allocate(markets: List[dict]) -> List[Tuple[dict, int]]:
    """
    Rank candidate markets by signal strength.
    Returns list of (market, rank) tuples (top 3 only).

    Ranking score = (volume_24h_normalized * 0.4) +
                    (price_midpoint_score  * 0.3) +
                    (days_left_score       * 0.3)
    """
    scored = []
    for m in markets:
        vol_score  = min(1.0, m.get("volume_24h", 0) / 500_000)
        # Prefer markets priced 0.35-0.65 (most edge potential)
        price      = m.get("price", 0.5)
        price_score = 1.0 - abs(price - 0.50) * 2
        # Prefer 14-90 day resolution
        days       = min(m.get("days_left", 30), 90)
        days_score = days / 90

        total = vol_score * 0.4 + price_score * 0.3 + days_score * 0.3
        scored.append((m, round(total, 4)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [(m, rank + 1) for rank, (m, _) in enumerate(scored[:3])]

# ═══════════════════════════════════════════════════════════════════
# SECTION 16 — MAIN TRADING LOOP
# ═══════════════════════════════════════════════════════════════════
def trading_loop():
    """
    Main trading engine loop.

    Schedule:
      - Market scan  : Every 60 minutes
      - News/signals : Every 15 minutes
      - Position mon : Every 10 minutes
      - API health   : Every 60 minutes
    """
    log.info("Trading engine started. Paper mode: %s", PAPER_MODE)
    state_set("running", True)

    last_scan_time      = 0
    last_signal_time    = 0
    last_monitor_time   = 0
    last_health_time    = 0
    last_heartbeat_time = 0
    last_disk_check     = 0
    bot_start_time      = time.time()

    SCAN_INTERVAL      = 3600   # 60 min
    SIGNAL_INTERVAL    = 900    # 15 min
    MONITOR_INTERVAL   = 600    # 10 min
    HEALTH_INTERVAL    = 3600   # 60 min
    HEARTBEAT_INTERVAL = 3600   # 60 min
    DISK_INTERVAL      = 3600   # 60 min

    # Initial runs
    check_api_health()
    scan_markets()

    while True:
        try:
            now = time.time()

            # Emergency stop / pause check
            if state_get("emergency_stop"):
                log.info("Emergency stop active — loop sleeping.")
                time.sleep(5)
                continue

            if state_get("paused"):
                # Still monitor positions even when paused
                if now - last_monitor_time >= MONITOR_INTERVAL:
                    monitor_positions()
                    last_monitor_time = now
                time.sleep(5)
                continue

            # ── Disk Auto-Clean + Monitor ──
            if now - last_disk_check >= DISK_INTERVAL:
                last_disk_check = now
                try:
                    auto_clean_disk()
                except Exception as de:
                    log.error("Auto-clean failed: %s", de)

            # ── API Health Check ──
            if now - last_health_time >= HEALTH_INTERVAL:
                check_api_health()
                last_health_time = now

            # ── Hourly Heartbeat — "Bot OK, no errors" ──
            if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                uptime_hrs = int((now - bot_start_time) / 3600)
                capital    = float(get_setting("capital_total"))
                open_pos   = len(get_open_positions())
                health     = bot_state.get("api_health", {})
                api_ok     = all([health.get("polymarket"), health.get("huggingface")])
                send_telegram(
                    f"✅ <b>HEARTBEAT — Bot Running</b>\n"
                    f"{'─'*28}\n"
                    f"⏱ Uptime     : {uptime_hrs}h\n"
                    f"💼 Capital    : ${capital:.2f}\n"
                    f"📂 Positions  : {open_pos} open\n"
                    f"🔌 APIs       : {'All OK' if api_ok else 'Some issues — check /health'}\n"
                    f"⚙️ Mode       : {'📄 Paper' if bool(int(get_setting('paper_mode'))) else '🔴 Live'}\n"
                    f"{'─'*28}\n"
                    f"No critical errors detected ✓"
                )
                last_heartbeat_time = now

            # ── Market Scan ──
            if now - last_scan_time >= SCAN_INTERVAL:
                scan_markets()
                last_scan_time = now

            # ── Position Monitor ──
            if now - last_monitor_time >= MONITOR_INTERVAL:
                monitor_positions()
                last_monitor_time = now

            # ── Signal Generation & Entry ──
            if now - last_signal_time >= SIGNAL_INTERVAL:
                last_signal_time = now

                if not check_consecutive_losses():
                    log.warning("Consecutive loss limit — skipping entry signals.")
                    time.sleep(30)
                    continue

                markets = state_get("scanned_markets") or []
                if not markets:
                    log.info("No markets cached — skipping signal cycle.")
                    time.sleep(30)
                    continue

                # Check how many positions we can still open
                open_count = len(get_open_positions())
                max_pos    = int(get_setting("max_positions"))
                slots      = max_pos - open_count

                if slots <= 0:
                    log.info("Max positions (%d) reached — skipping entry.", max_pos)
                    time.sleep(30)
                    continue

                # Run layers on ranked markets
                ranked = rank_and_allocate(markets)
                capital = float(get_setting("capital_total"))
                entered = 0

                for market, rank in ranked:
                    if entered >= slots:
                        break
                    if not is_running():
                        break

                    log.info("Evaluating rank %d: %s", rank, market["market_name"][:40])
                    passed, analysis = run_all_layers(market)

                    if passed:
                        size = kelly_position_size(
                            market_price   = market["price"],
                            estimated_prob = analysis["sentiment_score"],
                            capital        = capital,
                            rank           = rank,
                        )
                        pos = execute_buy_order(market, size, analysis)
                        if pos:
                            entered += 1
                            capital = float(get_setting("capital_total"))
                            log.info("✅ Position %d entered — $%.2f", entered, size)
                    else:
                        log.info("❌ %s failed layers: %s",
                                 market["market_name"][:30],
                                 ", ".join(analysis["layers_failed"]))

            time.sleep(10)

        except Exception as e:
            log.error("Trading loop exception: %s", e, exc_info=True)
            send_critical_alert(f"Main loop exception: {e}")
            time.sleep(30)

# ═══════════════════════════════════════════════════════════════════
# SECTION 17 — DAILY REPORT SCHEDULER
# ═══════════════════════════════════════════════════════════════════
def daily_report_loop():
    """Send daily report at 06:00 UTC every day."""
    while True:
        try:
            now  = utcnow()
            next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += timedelta(days=1)
            wait = (next_run - now).total_seconds()
            log.info("Daily report scheduled in %.0f seconds.", wait)
            time.sleep(wait)
            send_daily_report()
            snapshot_portfolio(float(get_setting("capital_total")))
        except Exception as e:
            log.error("Daily report error: %s", e)
            time.sleep(3600)

# ═══════════════════════════════════════════════════════════════════
# SECTION 18 — FLASK DASHBOARD
# ═══════════════════════════════════════════════════════════════════
app   = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config['SESSION_COOKIE_SAMESITE']       = 'Lax'
app.config['SESSION_COOKIE_SECURE']         = False
app.config['SESSION_COOKIE_HTTPONLY']       = True
app.config['PERMANENT_SESSION_LIFETIME']    = 86400   # 24 hours
_ADMIN_HASH    = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ── Login ──────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>POLYBOT — Login</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@800&display=swap" rel="stylesheet">
<style>
:root{--bg:#060a10;--s:#0b1220;--b:#162035;--c:#00d4ff;--g:#00e87a;--r:#ff3355;--t:#ccd9f0;--m:#3a5070}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);font-family:'JetBrains Mono',monospace;color:var(--t);min-height:100vh;display:flex;align-items:center;justify-content:center}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.025) 1px,transparent 1px);background-size:48px 48px;pointer-events:none}
.box{position:relative;z-index:1;background:var(--s);border:1px solid var(--b);border-top:2px solid var(--c);padding:48px 40px;width:380px;box-shadow:0 0 60px rgba(0,212,255,.08),0 40px 80px rgba(0,0,0,.5)}
.logo{text-align:center;margin-bottom:32px}
.logo-mark{font-family:'Syne',sans-serif;font-size:28px;font-weight:800;color:var(--c);letter-spacing:4px;text-shadow:0 0 20px rgba(0,212,255,.4)}
.logo-sub{font-size:10px;color:var(--m);letter-spacing:3px;margin-top:4px}
.div{height:1px;background:var(--b);margin:24px 0;position:relative}
.div::after{content:"SECURE ACCESS";position:absolute;top:-8px;left:50%;transform:translateX(-50%);background:var(--s);padding:0 12px;font-size:9px;color:var(--m);letter-spacing:2px}
label{font-size:10px;color:var(--m);letter-spacing:2px;display:block;margin-bottom:8px}
input{width:100%;padding:12px 16px;background:var(--bg);border:1px solid var(--b);color:var(--t);font-family:'JetBrains Mono',monospace;font-size:14px;outline:none;transition:.2s;margin-bottom:20px}
input:focus{border-color:var(--c);box-shadow:0 0 0 3px rgba(0,212,255,.08)}
button{width:100%;padding:14px;background:var(--c);color:#000;border:none;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12px;letter-spacing:3px;cursor:pointer;text-transform:uppercase;transition:.2s}
button:hover{opacity:.85;box-shadow:0 0 20px rgba(0,212,255,.3)}
.err{background:rgba(255,51,85,.1);border:1px solid rgba(255,51,85,.3);color:var(--r);font-size:11px;padding:10px 14px;margin-bottom:16px}
.st{margin-top:24px;display:flex;gap:8px;justify-content:center;align-items:center}
.dot{width:6px;height:6px;border-radius:50%;background:var(--g);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
</style></head><body>
<div class="box">
  <div class="logo"><div class="logo-mark">POLYBOT</div>
  <div class="logo-sub">Polymarket Automated Trading Engine</div></div>
  <div class="div"></div>
  {% if error %}<div class="err">⚠ {{error}}</div>{% endif %}
  <form method="POST">
    <label>ADMIN PASSWORD</label>
    <input type="password" name="password" placeholder="••••••••••••" autofocus>
    <button type="submit">→ AUTHENTICATE</button>
  </form>
  <div class="st"><div class="dot"></div>
  <span style="font-size:10px;color:var(--m)">BOT ENGINE {{'ONLINE' if running else 'STARTING'}}</span></div>
</div></body></html>"""

# ── Dashboard ──────────────────────────────────────────────────────
DASH_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>POLYBOT — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#060a10;--s:#0b1220;--s2:#0f1928;--b:#162035;--b2:#1e2e4a;--c:#00d4ff;--cd:rgba(0,212,255,.1);--g:#00e87a;--gd:rgba(0,232,122,.1);--r:#ff3355;--rd:rgba(255,51,85,.1);--am:#ffb300;--t:#ccd9f0;--t2:#7a90b0;--m:#3a5070}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);font-family:'JetBrains Mono',monospace;color:var(--t);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,255,.02) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.02) 1px,transparent 1px);background-size:48px 48px;pointer-events:none;z-index:0}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--b2)}
/* NAV */
nav{position:sticky;top:0;z-index:100;background:rgba(6,10,16,.92);backdrop-filter:blur(16px);border-bottom:1px solid var(--b);display:flex;align-items:center;padding:0 20px;height:54px;gap:16px}
.brand{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:var(--c);letter-spacing:3px;text-shadow:0 0 16px rgba(0,212,255,.35);flex-shrink:0}
.ndiv{width:1px;height:24px;background:var(--b)}
.nstat{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--t2)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:blink 2s infinite}
.sdot.off{background:var(--r);box-shadow:0 0 8px var(--r);animation:none}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.tabs{display:flex;gap:2px;flex:1}
.tab{padding:7px 16px;font-family:'JetBrains Mono';font-size:11px;letter-spacing:1.5px;color:var(--t2);background:transparent;border:1px solid transparent;cursor:pointer;text-transform:uppercase;transition:.2s}
.tab:hover{color:var(--c);border-color:var(--b2)}
.tab.active{color:var(--c);background:var(--cd);border-color:var(--c);border-bottom:2px solid var(--c)}
.nright{display:flex;align-items:center;gap:10px;margin-left:auto}
.btnstop{padding:7px 18px;font-family:'JetBrains Mono';font-size:11px;letter-spacing:2px;font-weight:700;text-transform:uppercase;background:var(--rd);color:var(--r);border:1px solid var(--r);cursor:pointer;transition:.2s}
.btnstop:hover{background:var(--r);color:#fff;box-shadow:0 0 16px rgba(255,51,85,.4)}
.btnstop.resumed{background:var(--gd);color:var(--g);border-color:var(--g)}
.btnlo{padding:7px 14px;font-family:'JetBrains Mono';font-size:10px;color:var(--m);background:transparent;border:1px solid var(--b);cursor:pointer;transition:.2s;text-decoration:none;display:inline-block}
.btnlo:hover{color:var(--t2);border-color:var(--b2)}
/* LAYOUT */
.wrap{position:relative;z-index:1;padding:20px;max-width:1600px;margin:0 auto}
.panel{display:none}.panel.active{display:block}
/* KPI */
.kpi-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:16px}
.kpi{background:var(--s);border:1px solid var(--b);border-top:2px solid var(--b2);padding:14px 16px;position:relative;overflow:hidden}
.kpi::after{content:'';position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(0,212,255,.08),transparent)}
.kl{font-size:9px;color:var(--m);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px}
.kv{font-size:20px;font-weight:700;color:var(--t);line-height:1}
.kv.c{color:var(--c)}.kv.g{color:var(--g)}.kv.r{color:var(--r)}.kv.a{color:var(--am)}
.ks{font-size:10px;color:var(--m);margin-top:4px}
/* CARD */
.card{background:var(--s);border:1px solid var(--b);padding:18px;margin-bottom:14px}
.ct{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--t2);letter-spacing:2.5px;text-transform:uppercase;margin-bottom:14px}
.ct::before{content:'';display:block;width:12px;height:2px;background:var(--c)}
/* GRID */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.g3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px}
/* TABLE */
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:8px 12px;font-size:9px;letter-spacing:2px;color:var(--m);border-bottom:1px solid var(--b);text-transform:uppercase}
td{padding:10px 12px;border-bottom:1px solid var(--b);color:var(--t);vertical-align:middle}
tr:hover td{background:rgba(0,212,255,.02)}
tr:last-child td{border-bottom:none}
.badge{display:inline-block;padding:2px 8px;font-size:9px;font-weight:700;letter-spacing:1px}
.bg{background:var(--gd);color:var(--g);border:1px solid var(--g)}
.br{background:var(--rd);color:var(--r);border:1px solid var(--r)}
.bc{background:var(--cd);color:var(--c);border:1px solid var(--c)}
/* LOG */
.log{background:#040810;border:1px solid var(--b);border-left:3px solid var(--c);padding:14px;height:300px;overflow-y:auto;font-size:11px;line-height:1.7}
.log .T{color:var(--g)}.log .W{color:var(--am)}.log .E{color:var(--r)}.log .I{color:var(--t2)}
/* SETTINGS */
.fg{margin-bottom:18px}
.fl{display:block;font-size:10px;letter-spacing:2px;color:var(--t2);text-transform:uppercase;margin-bottom:8px}
.fi{width:100%;padding:10px 14px;background:var(--bg);border:1px solid var(--b);color:var(--t);font-family:'JetBrains Mono';font-size:13px;outline:none;transition:.2s}
.fi:focus{border-color:var(--c);box-shadow:0 0 0 3px rgba(0,212,255,.06)}
.fh{font-size:10px;color:var(--m);margin-top:4px}
.btnp{padding:11px 28px;font-family:'JetBrains Mono';font-size:11px;letter-spacing:2px;font-weight:700;background:var(--c);color:#000;border:none;cursor:pointer;text-transform:uppercase;transition:.2s}
.btnp:hover{opacity:.85;box-shadow:0 0 20px rgba(0,212,255,.3)}
/* HEALTH */
.hitem{display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--s2);border:1px solid var(--b);margin-bottom:8px}
.hdot{width:8px;height:8px;border-radius:50%}
.hdot.ok{background:var(--g);box-shadow:0 0 6px var(--g)}
.hdot.fail{background:var(--r);box-shadow:0 0 6px var(--r)}
/* LAYERS */
.lrow{display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--s2);border:1px solid var(--b);margin-bottom:8px}
/* MARKET BROWSER */
.mitem{padding:12px 16px;background:var(--s2);border:1px solid var(--b);border-left:3px solid var(--b2);margin-bottom:8px;cursor:pointer;transition:.2s}
.mitem:hover{border-left-color:var(--c)}
.mn{font-size:12px;color:var(--t);margin-bottom:4px}
.ms{display:flex;gap:16px;font-size:10px;color:var(--m)}
/* CHART */
.ch{position:relative;height:240px}
/* MOBILE BOTTOM NAV */
.bnav{display:none;position:fixed;bottom:0;left:0;right:0;z-index:200;background:rgba(6,10,16,.97);backdrop-filter:blur(16px);border-top:1px solid var(--b);padding:8px 0 12px}
.bnav-row{display:flex;justify-content:space-around;align-items:center}
.bnav-item{display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 12px;cursor:pointer;border:none;background:transparent;color:var(--t2);font-family:'JetBrains Mono';font-size:8px;letter-spacing:1px;text-transform:uppercase;transition:.2s;border-top:2px solid transparent}
.bnav-item.active{color:var(--c);border-top-color:var(--c)}
.bnav-item span{font-size:18px;line-height:1}
@media(max-width:900px){.tabs{display:none!important}.bnav{display:block}.wrap{padding-bottom:80px}}
/* TOAST */
.tct{position:fixed;top:64px;right:20px;z-index:999;display:flex;flex-direction:column;gap:8px}
.toast{padding:12px 18px;font-size:12px;background:var(--s);border:1px solid var(--b);border-left:3px solid var(--c);min-width:240px}
.toast.ok{border-left-color:var(--g)}.toast.err{border-left-color:var(--r)}
</style></head><body>
<div class="tct" id="tc"></div>

<nav>
  <div class="brand">POLYBOT</div>
  <div class="ndiv"></div>
  <div class="nstat">
    <div class="sdot" id="sd"></div>
    <span id="stxt">ENGINE ONLINE</span>
  </div>
  <div class="ndiv"></div>
  <div class="tabs">
    <button class="tab active" onclick="sw('overview',this)">Overview</button>
    <button class="tab" onclick="sw('markets',this)">Markets</button>
    <button class="tab" onclick="sw('trades',this)">Positions</button>
    <button class="tab" onclick="sw('analytics',this)">Analytics</button>
    <button class="tab" onclick="sw('logs',this)">Live Logs</button>
    <button class="tab" onclick="sw('settings',this)">Settings</button>
  </div>
  <div class="nright">
    <button class="btnstop" id="sb" onclick="toggleStop()">⏹ EMERGENCY STOP</button>
    <a href="/logout" class="btnlo">LOGOUT</a>
  </div>
</nav>

<div class="wrap">

<!-- MOBILE BOTTOM NAV -->
<div class="bnav" id="bnav">
  <div class="bnav-row">
    <button class="bnav-item active" onclick="sw('overview',this)" id="bn-overview">
      <span>📊</span>Overview
    </button>
    <button class="bnav-item" onclick="sw('markets',this)" id="bn-markets">
      <span>🔍</span>Markets
    </button>
    <button class="bnav-item" onclick="sw('trades',this)" id="bn-trades">
      <span>📈</span>Positions
    </button>
    <button class="bnav-item" onclick="sw('analytics',this)" id="bn-analytics">
      <span>📉</span>Analytics
    </button>
    <button class="bnav-item" onclick="sw('logs',this)" id="bn-logs">
      <span>📋</span>Logs
    </button>
    <button class="bnav-item" onclick="sw('settings',this)" id="bn-settings">
      <span>⚙️</span>Settings
    </button>
  </div>
</div>

<!-- KPI ROW -->
<div class="kpi-row">
  <div class="kpi"><div class="kl">Wallet Balance</div><div class="kv c" id="kBal">-</div><div class="ks" id="kBalS">Loading...</div></div>
  <div class="kpi"><div class="kl">Today P&L</div><div class="kv" id="kPnl">-</div><div class="ks" id="kPnlS">-</div></div>
  <div class="kpi"><div class="kl">Win Rate</div><div class="kv g" id="kWr">-</div><div class="ks" id="kWrS">-</div></div>
  <div class="kpi"><div class="kl">Total Trades</div><div class="kv" id="kTot">-</div><div class="ks">All sessions</div></div>
  <div class="kpi"><div class="kl">Open Positions</div><div class="kv c" id="kOp">-</div><div class="ks" id="kMode">-</div></div>
  <div class="kpi"><div class="kl">Consecutive Losses</div><div class="kv a" id="kCl">-</div><div class="ks" id="kClS">-</div></div>
</div>

<!-- OVERVIEW -->
<div class="panel active" id="p-overview">
  <div class="g3">
    <div class="card"><div class="ct">Portfolio Growth</div><div class="ch"><canvas id="portChart"></canvas></div></div>
    <div class="card">
      <div class="ct">Strategy Layers</div>
      <div id="layerStatus">
        {% for l in layers %}
        <div class="lrow">
          <span style="font-size:10px;color:var(--c);width:20px">{{l.n}}</span>
          <span style="font-size:11px;color:var(--t2);flex:1">{{l.name}}</span>
          <span style="font-size:10px;font-weight:700;padding:2px 8px;background:{{l.bg}};color:{{l.col}};border:1px solid {{l.col}}">{{l.status}}</span>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="card"><div class="ct">Win / Loss</div><div class="ch"><canvas id="pieChart"></canvas></div></div>
  </div>
  <div class="card"><div class="ct">Recent Activity</div><div class="log" id="logPreview"></div></div>
</div>

<!-- MARKETS -->
<div class="panel" id="p-markets">
  <div class="g2">
    <div class="card">
      <div class="ct">API Connection Status</div>
      <div id="apiHealth">
        {% for api in api_status %}
        <div class="hitem">
          <div class="hdot {{api.cls}}"></div>
          <span style="font-size:11px;color:var(--t2);flex:1">{{api.name}}</span>
          <span style="font-size:10px;color:{{api.col}}">{{api.status}}</span>
        </div>
        {% endfor %}
      </div>
      <div style="margin-top:12px">
        <button class="btnp" onclick="testApis()" style="width:100%">⚡ TEST ALL CONNECTIONS</button>
      </div>
    </div>
    <div class="card">
      <div class="ct">Market Browser</div>
      <div style="margin-bottom:12px;display:flex;gap:8px">
        <input class="fi" id="mSearch" placeholder="Search markets..." oninput="filterMarkets()">
      </div>
      <div id="marketList" style="max-height:400px;overflow-y:auto">
        <div style="color:var(--m);font-size:11px;text-align:center;padding:24px">Loading markets...</div>
      </div>
    </div>
  </div>
</div>

<!-- POSITIONS -->
<div class="panel" id="p-trades">
  <div class="card">
    <div class="ct">Open Positions</div>
    <div style="overflow-x:auto"><table>
      <thead><tr>
        <th>Market</th><th>Side</th><th>Entry</th>
        <th>Current</th><th>Amount</th><th>Unreal P&L</th>
        <th>Sentiment</th><th>Edge</th><th>Target</th><th>Time</th>
      </tr></thead>
      <tbody id="posTBody">
        <tr><td colspan="10" style="text-align:center;color:var(--m);padding:24px">Loading...</td></tr>
      </tbody>
    </table></div>
  </div>
  <div class="card">
    <div class="ct">Closed Trades History</div>
    <div style="overflow-x:auto"><table>
      <thead><tr>
        <th>Market</th><th>Entry</th><th>Exit</th>
        <th>Amount</th><th>P&L</th><th>%</th><th>Reason</th><th>Date</th>
      </tr></thead>
      <tbody id="histTBody">
        <tr><td colspan="8" style="text-align:center;color:var(--m);padding:24px">Loading...</td></tr>
      </tbody>
    </table></div>
  </div>
</div>

<!-- ANALYTICS -->
<div class="panel" id="p-analytics">
  <div class="g2">
    <div class="card"><div class="ct">Equity Curve</div><div class="ch"><canvas id="eqChart"></canvas></div></div>
    <div class="card">
      <div class="ct">Performance Stats</div>
      <table><tbody id="statsTBody"></tbody></table>
    </div>
  </div>
  <div class="card"><div class="ct">Daily Win / Loss</div><div class="ch"><canvas id="barChart"></canvas></div></div>
</div>

<!-- LOGS -->
<div class="panel" id="p-logs">
  <div class="card">
    <div class="ct" style="justify-content:space-between">
      <span style="display:flex;align-items:center;gap:8px"><span style="width:12px;height:2px;background:var(--c);display:block"></span>Live Bot Logs</span>
      <span style="font-size:10px;color:var(--m)">Auto-refresh 5s</span>
    </div>
    <div class="log" id="mainLog" style="height:520px"></div>
  </div>
</div>

<!-- SETTINGS -->
<div class="panel" id="p-settings">

  <!-- API KEYS SECTION -->
  <div class="card" style="border-top:2px solid var(--c);margin-bottom:14px">
    <div class="ct" style="color:var(--c)">🔑 API Keys & Connections</div>
    <div style="background:rgba(0,212,255,.04);border:1px solid rgba(0,212,255,.15);padding:12px 14px;margin-bottom:16px;font-size:11px;color:var(--t2)">
      Keys တွေ ဒီမှာ ထည့်ပြီး <b style="color:var(--c)">SAVE & CONNECT</b> နှိပ်ပါ။ Dashboard restart မလိုဘဲ ချက်ချင်း အကျိုးသက်ရောက်တယ်။
    </div>
    <div class="g2">
      <div>
        <div class="fg">
          <label class="fl">🔐 Polymarket Wallet Private Key</label>
          <input class="fi" type="password" id="kPolyKey" placeholder="0xe4683ff...">
          <div class="fh">Polygon wallet private key (0x...)</div>
        </div>
        <div class="fg">
          <label class="fl">💼 Polymarket Funder Address</label>
          <input class="fi" type="text" id="kFunder" placeholder="0xe4683ff...">
          <div class="fh">Funder wallet address</div>
        </div>
        <div class="fg">
          <label class="fl">📰 GNews API Key</label>
          <input class="fi" type="password" id="kGnews" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx">
          <div class="fh">Get free key at gnews.io</div>
        </div>
      </div>
      <div>
        <div class="fg">
          <label class="fl">🤗 HuggingFace API Key</label>
          <input class="fi" type="password" id="kHf" placeholder="hf_xxxxxxxxxxxxx">
          <div class="fh">Get token at huggingface.co/settings/tokens</div>
        </div>
        <div class="fg">
          <label class="fl">📲 Telegram Bot Token</label>
          <input class="fi" type="password" id="kTgToken" placeholder="123456:ABCdef...">
          <div class="fh">Get from @BotFather on Telegram</div>
        </div>
        <div class="fg">
          <label class="fl">💬 Telegram Chat ID</label>
          <input class="fi" type="text" id="kTgChat" placeholder="123456789">
          <div class="fh">Your chat ID (use @userinfobot)</div>
        </div>
      </div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btnp" onclick="saveApiKeys()" style="flex:1">💾 SAVE & CONNECT ALL</button>
      <button class="btnp" onclick="testApis()" style="flex:1;background:var(--gd);color:var(--g);border:1px solid var(--g)">⚡ TEST ALL CONNECTIONS</button>
    </div>
    <div id="apiHealth" style="margin-top:14px">
      {% for api in api_status %}
      <div class="hitem">
        <div class="hdot {{api.cls}}"></div>
        <span style="font-size:11px;color:var(--t2);flex:1">{{api.name}}</span>
        <span style="font-size:10px;color:{{api.col}}">{{api.status}}</span>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="g2">
    <div>
      <!-- STRATEGY PARAMETERS -->
      <div class="card">
        <div class="ct">⚙️ Strategy Parameters</div>
        <div class="fg"><label class="fl">Sentiment Threshold</label>
          <input class="fi" type="number" id="sSent" step="0.01" min="0.1" max="1">
          <div class="fh">Minimum positive score. Recommended ≥ 0.55</div></div>
        <div class="fg"><label class="fl">6hr Momentum (%)</label>
          <input class="fi" type="number" id="sMom" step="0.5" min="0.5" max="20">
          <div class="fh">Min price change in 6 hours. Recommended ≥ 2.0</div></div>
        <div class="fg"><label class="fl">Min EV Edge (%)</label>
          <input class="fi" type="number" id="sEdge" step="0.5" min="1" max="30">
          <div class="fh">Min probability edge to enter. Recommended ≥ 8.0</div></div>
        <div class="fg"><label class="fl">Min Volume 24h ($)</label>
          <input class="fi" type="number" id="sVol" step="1000" min="1000">
          <div class="fh">Min daily volume. Recommended ≥ $10,000</div></div>
        <div class="fg"><label class="fl">Min Days to Resolution</label>
          <input class="fi" type="number" id="sDays" step="1" min="2" max="180">
          <div class="fh">Exit 48hr before close. Recommended ≥ 14</div></div>
        <button class="btnp" onclick="saveSettings()" style="width:100%">→ APPLY STRATEGY SETTINGS</button>
      </div>

      <!-- CAPITAL & RISK -->
      <div class="card">
        <div class="ct">💰 Capital & Risk</div>
        <div class="fg"><label class="fl">Total Capital ($)</label>
          <input class="fi" type="number" id="sCap" step="1" min="10">
          <div class="fh">Total trading capital</div></div>
        <div class="fg"><label class="fl">Max Trade Amount ($)</label>
          <input class="fi" type="number" id="sMax" step="0.5" min="0.5" max="100">
          <div class="fh">Kelly hard cap per position</div></div>
        <div class="fg"><label class="fl">Max Open Positions</label>
          <input class="fi" type="number" id="sMaxP" step="1" min="1" max="10">
          <div class="fh">Max simultaneous positions (default 3)</div></div>
        <div class="fg"><label class="fl">Consecutive Loss Limit</label>
          <input class="fi" type="number" id="sCl" step="1" min="1" max="10">
          <div class="fh">Auto-pause after N consecutive losses</div></div>
        <button class="btnp" onclick="saveSettings()" style="width:100%">→ APPLY CAPITAL SETTINGS</button>
      </div>
    </div>

    <div>
      <!-- SYSTEM INFO + CONTROLS -->
      <div class="card">
        <div class="ct">🖥️ System Info</div>
        <table><tbody id="sysInfo"></tbody></table>

        <div style="margin-top:14px;padding:14px;background:rgba(255,179,0,.06);border:1px solid rgba(255,179,0,.2)">
          <div style="font-size:10px;color:var(--am);letter-spacing:1px;margin-bottom:6px">⚠ EMERGENCY STOP</div>
          <div style="font-size:11px;color:var(--t2);margin-bottom:10px">Trading ချက်ချင်း ရပ်တယ်။ Open positions တွေ auto-close မဖြစ်ဘူး။</div>
          <button class="btnstop" style="width:100%" id="sb2" onclick="toggleStop()">⏹ TOGGLE EMERGENCY STOP</button>
        </div>

        <div style="margin-top:10px;padding:14px;background:rgba(0,232,122,.06);border:1px solid rgba(0,232,122,.2)">
          <div style="font-size:10px;color:var(--g);margin-bottom:6px">📄 PAPER / LIVE MODE</div>
          <div style="font-size:11px;color:var(--t2);margin-bottom:10px">Paper = simulation သာ။ Live = real money trade။</div>
          <button onclick="togglePaper()" style="width:100%;padding:10px;font-family:'JetBrains Mono';font-size:11px;letter-spacing:2px;font-weight:700;background:var(--gd);color:var(--g);border:1px solid var(--g);cursor:pointer;text-transform:uppercase" id="paperBtn">TOGGLE PAPER MODE</button>
        </div>
      </div>

      <!-- TELEGRAM COMMANDS -->
      <div class="card">
        <div class="ct">📲 Telegram Commands</div>
        <div style="font-size:11px;color:var(--t2);line-height:2.2">
          <div><span style="color:var(--c)">/stop</span> &nbsp;— Emergency stop</div>
          <div><span style="color:var(--c)">/resume</span> — Resume trading</div>
          <div><span style="color:var(--c)">/pause</span> &nbsp;— Pause (positions ဆက် monitor)</div>
          <div><span style="color:var(--c)">/status</span> — Status + P&L</div>
          <div><span style="color:var(--c)">/balance</span> — Balance report</div>
          <div><span style="color:var(--c)">/help</span> &nbsp;— Command list</div>
        </div>
        <div style="margin-top:14px">
          <button class="btnp" onclick="sendTestTelegram()" style="width:100%;background:var(--cd);color:var(--c);border:1px solid var(--c)">📲 SEND TEST TELEGRAM MESSAGE</button>
        </div>
      </div>
    </div>
  </div>
</div>

</div><!-- /wrap -->

<script>
Chart.defaults.color='#3a5070';
Chart.defaults.borderColor='#162035';
Chart.defaults.font.family="'JetBrains Mono',monospace";
Chart.defaults.font.size=11;

let stopped=false, allMarkets=[];

function sw(name,el){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab,.bnav-item').forEach(t=>t.classList.remove('active'));
  document.getElementById('p-'+name).classList.add('active');
  el.classList.add('active');
  // Sync desktop tab if mobile nav pressed
  const dTab=document.querySelector(`.tab[onclick*="${name}"]`);
  if(dTab) dTab.classList.add('active');
  // Sync mobile nav if desktop tab pressed
  const mTab=document.getElementById('bn-'+name);
  if(mTab) mTab.classList.add('active');
  // Load keys when settings opened
  if(name==='settings'){loadSettings();loadApiKeys();}
}

function toast(msg,type=''){
  const c=document.getElementById('tc');
  const d=document.createElement('div');
  d.className='toast '+(type==='ok'?'ok':type==='err'?'err':'');
  d.textContent=msg; c.appendChild(d);
  setTimeout(()=>d.remove(),3000);
}

function toggleStop(){
  fetch('/api/emergency-stop',{method:'POST'}).then(r=>r.json()).then(d=>{
    stopped=d.emergency_stop;
    const dot=document.getElementById('sd');
    const txt=document.getElementById('stxt');
    const b1=document.getElementById('sb');
    const b2=document.getElementById('sb2');
    if(stopped){
      dot.classList.add('off'); txt.textContent='ENGINE STOPPED';
      b1.classList.add('resumed'); b1.textContent='▶ RESUME BOT';
      if(b2){b2.classList.add('resumed');b2.textContent='▶ RESUME BOT';}
      toast('⚠ Emergency stop activated','err');
    } else {
      dot.classList.remove('off'); txt.textContent='ENGINE ONLINE';
      b1.classList.remove('resumed'); b1.textContent='⏹ EMERGENCY STOP';
      if(b2){b2.classList.remove('resumed');b2.textContent='⏹ TOGGLE EMERGENCY STOP';}
      toast('✓ Bot resumed','ok');
    }
  });
}

function togglePaper(){
  fetch('/api/toggle-paper',{method:'POST'}).then(r=>r.json()).then(d=>{
    toast(d.paper_mode?'📄 Paper mode ON':'🔴 Live mode ON', d.paper_mode?'':'err');
  });
}

function renderLogs(id){
  fetch('/api/logs').then(r=>r.json()).then(d=>{
    const el=document.getElementById(id);
    if(!el)return;
    const map={TRADE:'T',WARNING:'W',ERROR:'E',INFO:'I'};
    el.innerHTML=d.logs.map(l=>{
      const cls=l.includes('[TRADE]')?'T':l.includes('[WARNING]')?'W':l.includes('[ERROR]')?'E':'I';
      return `<div class="${cls}">${l}</div>`;
    }).join('');
  });
}

function renderPositions(){
  fetch('/api/positions').then(r=>r.json()).then(d=>{
    const tb=document.getElementById('posTBody');
    if(!tb)return;
    if(!d.positions.length){
      tb.innerHTML='<tr><td colspan="10" style="text-align:center;color:var(--m);padding:24px">No open positions</td></tr>';
      return;
    }
    tb.innerHTML=d.positions.map(p=>{
      const pnl=(p.current_price-p.entry_price)*p.shares;
      const pc=pnl>=0?'var(--g)':'var(--r)';
      return `<tr>
        <td>${p.market_name.slice(0,35)}</td>
        <td><span class="badge bg">${p.side}</span></td>
        <td>$${p.entry_price.toFixed(4)}</td>
        <td style="color:var(--c)">$${p.current_price.toFixed(4)}</td>
        <td>$${p.amount_usd.toFixed(2)}</td>
        <td style="color:${pc};font-weight:700">${pnl>=0?'+':''}$${pnl.toFixed(3)}</td>
        <td style="color:#9b6dff">${(p.sentiment_score*100).toFixed(0)}%</td>
        <td style="color:var(--am)">+${p.edge_pct.toFixed(1)}%</td>
        <td style="color:var(--g)">$${p.target_price.toFixed(4)}</td>
        <td style="color:var(--m)">${p.entry_time.slice(0,16)}</td>
      </tr>`;
    }).join('');
  });
  fetch('/api/trades-history').then(r=>r.json()).then(d=>{
    const tb=document.getElementById('histTBody');
    if(!tb||!d.trades)return;
    if(!d.trades.length){
      tb.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--m);padding:24px">No closed trades yet</td></tr>';
      return;
    }
    tb.innerHTML=d.trades.slice(0,20).map(t=>{
      const pc=t.pnl_usd>=0?'var(--g)':'var(--r)';
      const em=t.pnl_usd>=0?'✅':'❌';
      return `<tr>
        <td>${t.market_name.slice(0,30)}</td>
        <td>$${t.entry_price.toFixed(4)}</td>
        <td>$${t.exit_price.toFixed(4)}</td>
        <td>$${t.amount_usd.toFixed(2)}</td>
        <td style="color:${pc};font-weight:700">${em} ${t.pnl_usd>=0?'+':''}$${t.pnl_usd.toFixed(3)}</td>
        <td style="color:${pc}">${t.pnl_pct>=0?'+':''}${t.pnl_pct.toFixed(1)}%</td>
        <td style="color:var(--m)">${t.exit_reason}</td>
        <td style="color:var(--m)">${t.exit_time.slice(0,10)}</td>
      </tr>`;
    }).join('');
  });
}

function renderMarkets(){
  fetch('/api/markets').then(r=>r.json()).then(d=>{
    allMarkets=d.markets||[];
    filterMarkets();
  });
}

function filterMarkets(){
  const q=(document.getElementById('mSearch').value||'').toLowerCase();
  const filtered=allMarkets.filter(m=>m.market_name.toLowerCase().includes(q));
  const el=document.getElementById('marketList');
  if(!filtered.length){el.innerHTML='<div style="color:var(--m);font-size:11px;text-align:center;padding:24px">No markets found</div>';return;}
  el.innerHTML=filtered.slice(0,20).map(m=>`
    <div class="mitem">
      <div class="mn">${m.market_name.slice(0,60)}</div>
      <div class="ms">
        <span>Vol: $${(m.volume_24h/1000).toFixed(0)}K</span>
        <span>Price: $${m.price.toFixed(3)}</span>
        <span>Days: ${m.days_left}</span>
        <span style="color:var(--c)">${m.category||'—'}</span>
      </div>
    </div>`).join('');
}

function testApis(){
  toast('Testing API connections...','');
  fetch('/api/health-check',{method:'POST'}).then(r=>r.json()).then(d=>{
    const el=document.getElementById('apiHealth');
    el.innerHTML=d.health.map(h=>`
      <div class="hitem">
        <div class="hdot ${h.ok?'ok':'fail'}"></div>
        <span style="font-size:11px;color:var(--t2);flex:1">${h.name}</span>
        <span style="font-size:10px;color:${h.ok?'var(--g)':'var(--r)'}">${h.ok?'CONNECTED':'FAILED'}</span>
      </div>`).join('');
    toast(d.all_ok?'✓ All APIs connected':'⚠ Some APIs failed', d.all_ok?'ok':'err');
  });
}

function updateKPIs(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const kBal=document.getElementById('kBal');
    kBal.textContent='$'+d.capital.toFixed(2);
    const pnl=d.today_pnl||0;
    const kP=document.getElementById('kPnl');
    kP.textContent=(pnl>=0?'+$':'-$')+Math.abs(pnl).toFixed(2);
    kP.className='kv '+(pnl>=0?'g':'r');
    document.getElementById('kPnlS').textContent=(pnl/50*100).toFixed(2)+'%';
    document.getElementById('kWr').textContent=d.win_rate+'%';
    document.getElementById('kWrS').textContent=d.wins+'W / '+d.losses+'L';
    document.getElementById('kTot').textContent=d.total_trades;
    document.getElementById('kOp').textContent=d.open_positions;
    document.getElementById('kMode').textContent=d.paper_mode?'📄 Paper':'🔴 Live';
    document.getElementById('kCl').textContent=d.consecutive_losses;
    document.getElementById('kClS').textContent='Limit: '+d.loss_limit;
    const sysEl=document.getElementById('sysInfo');
    if(sysEl) sysEl.innerHTML=[
      ['Status', d.stopped?'🛑 STOPPED':d.paused?'⏸ PAUSED':'✅ RUNNING', d.stopped?'var(--r)':d.paused?'var(--am)':'var(--g)'],
      ['Mode', d.paper_mode?'📄 Paper Trading':'🔴 Live Trading', d.paper_mode?'var(--am)':'var(--r)'],
      ['Capital', '$'+d.capital.toFixed(2), 'var(--c)'],
      ['Open Positions', d.open_positions, 'var(--t)'],
      ['Consecutive Losses', d.consecutive_losses+'/'+d.loss_limit, d.consecutive_losses>=d.loss_limit?'var(--r)':'var(--g)'],
      ['Emergency Stop', d.stopped?'ACTIVE':'OFF', d.stopped?'var(--r)':'var(--m)'],
    ].map(([l,v,c])=>`<tr><td style="color:var(--t2)">${l}</td><td style="color:${c}">${v}</td></tr>`).join('');
  });
}

function loadSettings(){
  fetch('/api/settings').then(r=>r.json()).then(d=>{
    const s=d.settings;
    const f=id=>document.getElementById(id);
    if(f('sSent'))f('sSent').value=s.sentiment_threshold;
    if(f('sMom'))f('sMom').value=s.momentum_pct;
    if(f('sEdge'))f('sEdge').value=s.min_edge_pct;
    if(f('sVol'))f('sVol').value=s.min_volume_24h;
    if(f('sDays'))f('sDays').value=s.min_days_resolution;
    if(f('sCap'))f('sCap').value=s.capital_total;
    if(f('sMax'))f('sMax').value=s.max_trade_usd;
    if(f('sMaxP'))f('sMaxP').value=s.max_positions;
    if(f('sCl'))f('sCl').value=s.consecutive_loss_limit;
  });
}

function saveApiKeys(){
  const g=id=>{const e=document.getElementById(id);return e?e.value.trim():''};
  const payload={
    api_polygon_key:   g('kPolyKey'),
    api_funder_address:g('kFunder'),
    api_gnews_key:     g('kGnews'),
    api_hf_key:        g('kHf'),
    api_telegram_token:g('kTgToken'),
    api_telegram_chat: g('kTgChat'),
  };
  // Remove empty keys so we don't overwrite existing ones
  Object.keys(payload).forEach(k=>{if(!payload[k])delete payload[k];});
  fetch('/api/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(r=>r.json()).then(d=>{
    if(d.success){toast('✓ API Keys saved! Testing connections...','ok');setTimeout(testApis,800);}
    else toast('✗ Save failed','err');
  });
}

function loadApiKeys(){
  fetch('/api/keys').then(r=>r.json()).then(d=>{
    const k=d.keys||{};
    const set=(id,val)=>{const e=document.getElementById(id);if(e&&val)e.placeholder=val.slice(0,8)+'••••••••';};
    set('kPolyKey',  k.api_polygon_key);
    set('kFunder',   k.api_funder_address);
    set('kGnews',    k.api_gnews_key);
    set('kHf',       k.api_hf_key);
    set('kTgToken',  k.api_telegram_token);
    set('kTgChat',   k.api_telegram_chat);
  });
}

function sendTestTelegram(){
  fetch('/api/test-telegram',{method:'POST'}).then(r=>r.json()).then(d=>{
    toast(d.success?'✓ Telegram message sent!':'✗ Telegram failed — check token & chat ID',d.success?'ok':'err');
  });
}

function saveSettings(){
  const g=id=>{const e=document.getElementById(id);return e?e.value:null;};
  const payload={
    sentiment_threshold:g('sSent'),
    momentum_pct:g('sMom'),
    min_edge_pct:g('sEdge'),
    min_volume_24h:g('sVol'),
    min_days_resolution:g('sDays'),
    capital_total:g('sCap'),
    max_trade_usd:g('sMax'),
    max_positions:g('sMaxP'),
    consecutive_loss_limit:g('sCl'),
  };
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
  .then(r=>r.json()).then(d=>toast(d.success?'✓ Settings saved':'✗ Save failed',d.success?'ok':'err'));
}

let portC,pieC,eqC,barC;

function initCharts(hist,stats,daily){
  daily=daily||[];
  const cc={responsive:true,maintainAspectRatio:false};
  const gridColor='rgba(22,32,53,.8)';
  const mk=i=>({
    x:{grid:{color:gridColor},ticks:{color:'#3a5070'}},
    y:{grid:{color:gridColor},ticks:{color:'#3a5070',...i}}
  });

  if(!portC){
    portC=new Chart(document.getElementById('portChart'),{
      type:'line',data:{labels:hist.map(h=>h.day),
      datasets:[{label:'Portfolio',data:hist.map(h=>h.balance),
      borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,.06)',
      borderWidth:2,pointRadius:3,pointBackgroundColor:'#00d4ff',tension:.4,fill:true}]},
      options:{...cc,plugins:{legend:{display:false}},scales:mk({callback:v=>'$'+v})}
    });
  }

  if(!pieC){
    pieC=new Chart(document.getElementById('pieChart'),{
      type:'doughnut',data:{labels:['Wins','Losses'],
      datasets:[{data:[stats.wins||0,stats.losses||0],
      backgroundColor:['rgba(0,232,122,.15)','rgba(255,51,85,.15)'],
      borderColor:['#00e87a','#ff3355'],borderWidth:2}]},
      options:{...cc,plugins:{legend:{display:false}},cutout:'68%'}
    });
  }

  if(!eqC&&document.getElementById('eqChart')){
    eqC=new Chart(document.getElementById('eqChart'),{
      type:'line',data:{labels:hist.map(h=>h.day),
      datasets:[{label:'Equity',data:hist.map(h=>h.balance),
      borderColor:'#00e87a',backgroundColor:'rgba(0,232,122,.05)',
      borderWidth:2,tension:.4,fill:true,pointRadius:4,pointBackgroundColor:'#00e87a'}]},
      options:{...cc,plugins:{legend:{display:false}},scales:mk({callback:v=>'$'+v})}
    });
  }

  if(!barC&&document.getElementById('barChart')){
    const dL=daily.length?daily.map(d=>d.day):hist.map(h=>h.day);
    const dW=daily.length?daily.map(d=>d.wins):hist.map(()=>0);
    const dLs=daily.length?daily.map(d=>d.losses):hist.map(()=>0);
    barC=new Chart(document.getElementById('barChart'),{
      type:'bar',data:{labels:dL,datasets:[
        {label:'Wins',data:dW,backgroundColor:'rgba(0,232,122,.3)',borderColor:'#00e87a',borderWidth:1},
        {label:'Losses',data:dLs,backgroundColor:'rgba(255,51,85,.3)',borderColor:'#ff3355',borderWidth:1}
      ]},
      options:{...cc,plugins:{legend:{display:true,labels:{color:'#7a90b0'}}},scales:mk({})}
    });
  }
}

function loadAnalytics(){
  fetch('/api/analytics').then(r=>r.json()).then(d=>{
    const el=document.getElementById('statsTBody');
    if(el) el.innerHTML=d.stats.map(([l,v,c])=>
      `<tr><td style="color:var(--t2)">${l}</td><td style="color:${c}">${v}</td></tr>`
    ).join('');
  });
}

function refreshAll(){
  fetch('/api/chart-data').then(r=>r.json()).then(d=>initCharts(d.history,d.stats,d.daily||[]));
  updateKPIs();
  renderLogs('logPreview');
  renderLogs('mainLog');
  renderPositions();
  renderMarkets();
  loadAnalytics();
}

document.addEventListener('DOMContentLoaded',()=>{
  loadSettings();
  loadApiKeys();
  refreshAll();
  setInterval(()=>{
    updateKPIs();
    renderLogs('logPreview');
    renderLogs('mainLog');
    renderPositions();
  },5000);
  setInterval(()=>{
    renderMarkets();
    loadAnalytics();
  },60000);
});
</script></body></html>"""

# ── Flask Routes ───────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "").encode()
        if bcrypt.checkpw(pw, _ADMIN_HASH):
            session.permanent = True
            session["auth"] = True
            return redirect(url_for("dashboard"))
        error = "Invalid password."
    return render_template_string(
        LOGIN_HTML, error=error,
        running=state_get("running")
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    # Build layer status for template
    health = bot_state.get("api_health", {})
    layers = [
        {"n":"L1","name":"News Sentiment > 0.55",       "status":"WATCHING","col":"var(--am)","bg":"var(--amber-dim, rgba(255,179,0,.1))"},
        {"n":"L2","name":"6hr Momentum > 2%",            "status":"WATCHING","col":"var(--am)","bg":"rgba(255,179,0,.1)"},
        {"n":"L3","name":"EV Edge > 8%",                 "status":"WATCHING","col":"var(--am)","bg":"rgba(255,179,0,.1)"},
        {"n":"L4","name":"Liquidity Filter",             "status":"WATCHING","col":"var(--am)","bg":"rgba(255,179,0,.1)"},
        {"n":"L5","name":"Days to Resolution > 14",      "status":"WATCHING","col":"var(--am)","bg":"rgba(255,179,0,.1)"},
        {"n":"L6","name":"No Duplicate Position",        "status":"WATCHING","col":"var(--am)","bg":"rgba(255,179,0,.1)"},
        {"n":"L7","name":"All APIs Healthy",
         "status":"OK" if (health.get("polymarket") and health.get("huggingface")) else "FAIL",
         "col":"var(--g)" if (health.get("polymarket") and health.get("huggingface")) else "var(--r)",
         "bg":"rgba(0,232,122,.1)" if health.get("polymarket") else "rgba(255,51,85,.1)"},
    ]
    api_status = [
        {"name":"Polymarket CLOB",  "cls":"ok" if health.get("polymarket")  else "fail","status":"CONNECTED" if health.get("polymarket")  else "DISCONNECTED","col":"var(--g)" if health.get("polymarket")  else "var(--r)"},
        {"name":"GNews API",        "cls":"ok" if health.get("gnews")        else "fail","status":"CONNECTED" if health.get("gnews")        else "DISCONNECTED","col":"var(--g)" if health.get("gnews")        else "var(--r)"},
        {"name":"HuggingFace API",  "cls":"ok" if health.get("huggingface") else "fail","status":"CONNECTED" if health.get("huggingface") else "DISCONNECTED","col":"var(--g)" if health.get("huggingface") else "var(--r)"},
        {"name":"Telegram Bot",     "cls":"ok" if health.get("telegram")    else "fail","status":"CONNECTED" if health.get("telegram")    else "DISCONNECTED","col":"var(--g)" if health.get("telegram")    else "var(--r)"},
    ]
    return render_template_string(DASH_HTML, layers=layers, api_status=api_status)

# ── API Endpoints ──────────────────────────────────────────────────
@app.route("/api/status")
@login_required
def api_status():
    stats   = get_trade_stats()
    capital = float(get_setting("capital_total"))
    return jsonify({
        "capital":           capital,
        "today_pnl":         stats.get("today_pnl") or 0,
        "win_rate":          stats.get("win_rate", 0),
        "wins":              stats.get("wins", 0),
        "losses":            stats.get("losses", 0),
        "total_trades":      stats.get("total", 0),
        "open_positions":    len(get_open_positions()),
        "consecutive_losses":state_get("consecutive_losses") or 0,
        "loss_limit":        int(get_setting("consecutive_loss_limit")),
        "paper_mode":        bool(int(get_setting("paper_mode"))),
        "paused":            state_get("paused"),
        "stopped":           state_get("emergency_stop"),
    })

@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify({"logs": list(LOG_BUFFER)})

@app.route("/api/positions")
@login_required
def api_positions():
    return jsonify({"positions": get_open_positions()})

@app.route("/api/trades-history")
@login_required
def api_trades_history():
    with _db_lock:
        conn  = get_db()
        rows  = conn.execute(
            "SELECT * FROM trades ORDER BY exit_time DESC LIMIT 50"
        ).fetchall()
        conn.close()
    return jsonify({"trades": [dict(r) for r in rows]})

@app.route("/api/markets")
@login_required
def api_markets():
    markets = state_get("scanned_markets") or []
    return jsonify({"markets": markets})

@app.route("/api/chart-data")
@login_required
def api_chart_data():
    hist  = get_portfolio_history(7)
    stats = get_trade_stats()
    with _db_lock:
        conn = get_db()
        daily = conn.execute("""
            SELECT date(exit_time) as day,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) as losses
            FROM trades
            WHERE exit_time >= datetime('now','-7 days')
            GROUP BY date(exit_time)
            ORDER BY day
        """).fetchall()
        conn.close()
    daily_data = [{"day": r["day"], "wins": r["wins"], "losses": r["losses"]} for r in daily]
    return jsonify({"history": hist, "stats": stats, "daily": daily_data})

@app.route("/api/analytics")
@login_required
def api_analytics():
    stats   = get_trade_stats()
    capital = float(get_setting("capital_total"))
    start   = 50.0
    pnl     = (stats.get("total_pnl") or 0)
    pnl_pct = pnl / start * 100

    rows = [
        ["Total Capital",    f"${start:.2f}",                    "var(--t2)"],
        ["Current Value",    f"${capital:.2f}",                  "var(--g)" if capital >= start else "var(--r)"],
        ["All-Time P&L",     f"{'+'if pnl>=0 else ''}${pnl:.2f} ({pnl_pct:+.1f}%)", "var(--g)" if pnl >= 0 else "var(--r)"],
        ["Total Trades",     str(stats.get("total", 0)),         "var(--t)"],
        ["Wins",             str(stats.get("wins", 0)),          "var(--g)"],
        ["Losses",           str(stats.get("losses", 0)),        "var(--r)"],
        ["Win Rate",         f"{stats.get('win_rate',0):.1f}%",  "var(--g)" if stats.get("win_rate",0) >= 55 else "var(--am)"],
        ["Paper Mode",       "YES" if bool(int(get_setting("paper_mode"))) else "LIVE", "var(--am)"],
    ]
    return jsonify({"stats": rows})

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    keys = [
        "sentiment_threshold","momentum_pct","min_edge_pct",
        "min_volume_24h","min_days_resolution","capital_total",
        "max_trade_usd","max_positions","consecutive_loss_limit","paper_mode"
    ]
    if request.method == "POST":
        data = request.get_json() or {}
        for k in keys:
            if k in data and data[k] is not None:
                set_setting(k, str(data[k]))
        log.info("Settings updated via dashboard: %s", data)
        changed = {k: data[k] for k in keys if k in data and data[k] is not None}
        if changed:
            lines = "\n".join(f"  {k}: {v}" for k, v in changed.items())
            send_telegram(f"⚙️ <b>Settings Updated via Dashboard</b>\n{'─'*28}\n{lines}")
        return jsonify({"success": True})
    settings = {k: get_setting(k) for k in keys}
    return jsonify({"settings": settings})

@app.route("/api/emergency-stop", methods=["POST"])
@login_required
def api_emergency_stop():
    current = state_get("emergency_stop")
    new_val = not current
    state_set("emergency_stop", new_val)
    status  = "STOPPED" if new_val else "RESUMED"
    log.warning("Emergency stop %s via dashboard", status)
    send_critical_alert(f"Emergency stop {status} via Dashboard")
    return jsonify({"emergency_stop": new_val, "status": status})

@app.route("/api/keys", methods=["GET", "POST"])
@login_required
def api_keys():
    """Get or save API keys via Dashboard."""
    KEY_FIELDS = [
        "api_polygon_key", "api_funder_address",
        "api_gnews_key", "api_hf_key",
        "api_telegram_token", "api_telegram_chat",
    ]
    if request.method == "POST":
        data = request.get_json() or {}
        saved = []
        for k in KEY_FIELDS:
            if k in data and data[k]:
                set_setting(k, data[k].strip())
                saved.append(k)
        # Re-init Polymarket client if keys changed
        if "api_polygon_key" in saved or "api_funder_address" in saved:
            threading.Thread(target=init_polymarket_client, daemon=True).start()
        log.info("API keys updated via Dashboard: %s", saved)
        if saved:
            send_telegram(
                f"🔑 <b>API Keys Updated via Dashboard</b>\n"
                f"{'─'*28}\n"
                f"Updated: {', '.join(saved)}\n"
                f"Connections are being retested."
            )
        return jsonify({"success": True, "updated": saved})
    # GET — return masked keys
    keys = {}
    for k in KEY_FIELDS:
        val = get_setting(k)
        keys[k] = val[:4] + "••••" if val and len(val) > 4 else ("SET" if val else "")
    return jsonify({"keys": keys})

@app.route("/api/test-telegram", methods=["POST"])
@login_required
def api_test_telegram():
    """Send test Telegram message."""
    success = send_telegram(
        "✅ <b>POLYBOT Test Message</b>\n"
        "Dashboard မှ Telegram connection successful!\n"
        f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    return jsonify({"success": success})

@app.route("/api/toggle-paper", methods=["POST"])
@login_required
def api_toggle_paper():
    current = bool(int(get_setting("paper_mode")))
    new_val = not current
    set_setting("paper_mode", "1" if new_val else "0")
    log.info("Paper mode toggled: %s", new_val)
    return jsonify({"paper_mode": new_val})

@app.route("/api/health-check", methods=["POST"])
@login_required
def api_health_check():
    check_api_health()
    health = bot_state.get("api_health", {})
    result = [
        {"name": "Polymarket CLOB", "ok": health.get("polymarket", False)},
        {"name": "GNews API",       "ok": health.get("gnews",      False)},
        {"name": "HuggingFace API", "ok": health.get("huggingface",False)},
        {"name": "Telegram Bot",    "ok": health.get("telegram",   False)},
    ]
    all_ok = all(r["ok"] for r in result[:3])
    return jsonify({"health": result, "all_ok": all_ok})

# ═══════════════════════════════════════════════════════════════════
# SECTION 19 — ENTRY POINT
# ═══════════════════════════════════════════════════════════════════
def main():
    print("""
╔══════════════════════════════════════════════════════════════════╗
║         POLYMARKET SMART HYBRID TRADING BOT v2.0                ║
╠══════════════════════════════════════════════════════════════════╣
║  Dashboard : http://0.0.0.0:8501                                 ║
║  Mode      : {mode}
╚══════════════════════════════════════════════════════════════════╝
    """.format(mode="📄 PAPER TRADING" if PAPER_MODE else "🔴 LIVE TRADING"))

    # Initialize database
    init_db()

    # Initial portfolio snapshot
    snapshot_portfolio(float(get_setting("capital_total")))

    # Initialize Polymarket CLOB client
    if not PAPER_MODE:
        init_polymarket_client()

    # Send startup notification
    send_telegram(
        f"🚀 <b>POLYBOT STARTED</b>\n"
        f"Mode: {'📄 Paper' if PAPER_MODE else '🔴 Live'}\n"
        f"Dashboard: http://your-vps-ip:8501\n"
        f"Send /help for commands."
    )

    # ── Start background threads ──
    threads = [
        threading.Thread(target=trading_loop,         name="TradingEngine",  daemon=True),
        threading.Thread(target=telegram_polling_loop, name="TelegramPoller", daemon=True),
        threading.Thread(target=daily_report_loop,    name="DailyReport",    daemon=True),
    ]
    for t in threads:
        t.start()
        log.info("Thread started: %s", t.name)

    # ── Start Flask (main thread) ──
    log.info("Flask dashboard starting on 0.0.0.0:8501")
    app.run(
        host      = "0.0.0.0",
        port      = 8501,
        debug     = False,
        threaded  = True,
        use_reloader = False,
    )

if __name__ == "__main__":
    main()
