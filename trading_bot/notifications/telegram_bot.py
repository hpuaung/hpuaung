"""
notifications/telegram_bot.py — Telegram notifications + command bot.

Outbound notifications are sent over the plain HTTP Bot API with `requests`, so
they work from any (sync) thread without touching an event loop. The interactive
command bot (/start /dashboard /status /stop) runs python-telegram-bot's async
Application inside its own thread with a private asyncio loop.
"""

import time
import asyncio
import threading

import requests

import database as db

API_BASE = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# Low-level send (sync)
# ---------------------------------------------------------------------------
def send_message(text, chat_id=None):
    token = db.get_setting("telegram_token", "")
    chat_id = chat_id or db.get_setting("telegram_chat_id", "")
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            API_BASE.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:  # noqa: BLE001
        db.log_event("TELEGRAM_ERROR", str(e))
        return False


def test_telegram():
    ok = send_message("✅ <b>Trading Bot</b> — Telegram test message OK")
    return ok


def send_config_report():
    """Sent after saving API keys: a short config summary."""
    pairs = db.get_setting("selected_pairs", "")
    mode = "PAPER" if db.get_bool("paper_trading_mode", True) else "REAL"
    msg = (
        "⚙️ <b>Config Saved</b>\n"
        f"Mode: {mode}\n"
        f"Pairs: {pairs}\n"
        f"Scalping: {'ON' if db.get_bool('scalping_bot_on') else 'OFF'}\n"
        f"Swing: {'ON' if db.get_bool('swing_bot_on') else 'OFF'}"
    )
    return send_message(msg)


# ---------------------------------------------------------------------------
# Notification helpers (respect notify_* toggles)
# ---------------------------------------------------------------------------
def notify_trade_open(pos):
    if not db.get_bool("notify_trade_open", True):
        return
    engine = "⚡" if pos["strategy"] == "scalping" else "📈"
    mode = "Paper" if pos.get("paper_mode") else "Real"
    msg = (
        "🟢 <b>TRADE OPENED</b>\n"
        f"{engine} {pos['symbol']} {pos['side']}\n"
        f"Entry: {pos['entry_price']}\n"
        f"Size: {pos['entry_qty']} (Lev: {pos['leverage']}x)\n"
        f"TP1/2/3: {pos['tp1']} / {pos['tp2']} / {pos['tp3']}\n"
        f"SL: {pos['sl_price']}\n"
        f"Engine: {pos['strategy'].title()} | Mode: {mode}\n"
        f"AI: {pos.get('lgbm_score', 0):.2f} | News: {pos.get('news_score', 0):+.2f}"
    )
    send_message(msg)


def notify_trade_close(trade):
    if not db.get_bool("notify_trade_close", True):
        return
    emoji = "🟢" if trade["net_pnl"] >= 0 else "🔴"
    msg = (
        f"{emoji} <b>TRADE CLOSED</b>\n"
        f"{trade['pair']} {trade['side']}\n"
        f"Entry: {trade['entry_price']} → Exit: {trade['exit_price']}\n"
        f"Gross: {trade['pnl_amount']:.2f} | Fees: {trade['fees_paid']:.2f}\n"
        f"Net PnL: {trade['net_pnl']:.2f} ({trade['pnl_percent']:.2f}%)\n"
        f"Reason: {trade['close_reason']}"
    )
    send_message(msg)


def notify_risk_alert(health, reason, action=""):
    if not db.get_bool("notify_risk_alert", True):
        return
    msg = (
        "⚠️ <b>RISK ALERT</b>\n"
        f"Health: {health:.0f}%\n"
        f"Reason: {reason}\n"
        f"Action: {action}"
    )
    send_message(msg)


def notify_engine_stop(reason=""):
    if not db.get_bool("notify_engine_stop", True):
        return
    send_message(f"⛔ <b>ENGINE STOP</b>\n{reason}")


def notify_daily_report(report_text):
    if not db.get_bool("notify_daily_report", True):
        return
    send_message(report_text)


def build_status_text():
    """A dashboard-like status snapshot (reads the DB only, no Binance calls)."""
    from execution import risk_guard
    paper = db.get_bool("paper_trading_mode", True)
    conn = "🟢" if db.get_setting("binance_conn", "") == "1" else "🔴"

    def _fb(v):
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return "—"

    eq = db.get_float("last_equity", 0.0)
    starting = db.get_float("starting_balance", eq or 1)
    health = risk_guard.health_ratio(eq, starting)
    _, zone = risk_guard.health_zone(health)
    pnl = risk_guard.today_pnl()
    trades = db.get_today_trades()
    wins = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
    wr = (wins / len(trades) * 100) if trades else 0.0
    positions = db.get_open_positions()
    lgbm_ok = "🟢" if db.get_setting("lgbm_last_trained", "") else "🔴"

    lines = [
        "📊 <b>BOT STATUS</b>",
        f"⏱ {db.utcnow_str()} UTC",
        f"{conn} Binance | {'🧪 PAPER' if paper else '💰 REAL'}",
        f"💰 Test: {_fb(db.get_setting('last_equity_test',''))} | "
        f"Live: {_fb(db.get_setting('last_equity_live',''))}",
        f"🏥 Health: {health:.0f}% {zone}",
        f"📈 Today PnL: {pnl:+.2f} | Trades {len(trades)} | WR {wr:.0f}%",
        "──────────────",
        f"⚡ Scalping: {'🟢 Running' if db.get_bool('scalping_bot_on') else '🔴 Stopped'}",
        f"📈 Swing: {'🟢 Running' if db.get_bool('swing_bot_on') else '🔴 Stopped'}",
        f"🤖 LightGBM: {lgbm_ok}",
        f"📍 Open positions: {len(positions)}",
    ]
    for p in positions[:10]:
        lines.append(f"  {p['symbol']} {p['side']} @ {p['entry_price']}")
    return "\n".join(lines)


def notify_status():
    """Send the periodic status snapshot to Telegram."""
    send_message(build_status_text())


def notify_settings_saved():
    if not db.get_setting("telegram_token", ""):
        return
    send_message("⚙️ <b>Settings saved</b>\n" + build_status_text())


# ---------------------------------------------------------------------------
# Interactive command bot (async, own thread)
# ---------------------------------------------------------------------------
_pending_stop = {"ts": 0.0}


def _build_dashboard_text():
    from execution import risk_guard
    paper = db.get_bool("paper_trading_mode", True)
    api_mode = "real" if not paper else "test"
    try:
        from utils import binance_client as bc
        equity = bc.get_equity(api_mode)
    except Exception:  # noqa: BLE001
        equity = 0.0
    starting = db.get_float("starting_balance", equity)
    health = risk_guard.health_ratio(equity, starting)
    _, zone = risk_guard.health_zone(health)
    pnl = risk_guard.today_pnl()
    positions = db.get_open_positions()

    lines = [
        f"💰 Balance: ${equity:,.2f}",
        f"📈 Today PnL: {pnl:+.2f}",
        f"🏥 Health: {health:.0f}% [{zone}]",
        "──────────────────",
        f"⚡ Scalping: {'🟢' if db.get_bool('scalping_bot_on') else '🔴'}",
        f"📈 Swing: {'🟢' if db.get_bool('swing_bot_on') else '🔴'}",
        "──────────────────",
        f"Open Positions: {len(positions)}",
    ]
    for p in positions[:10]:
        lines.append(f"  {p['symbol']} {p['side']} @ {p['entry_price']}")
    return "\n".join(lines)


def run_command_bot(stop_event=None):
    """Run the async command bot. Intended as a thread target."""
    token = db.get_setting("telegram_token", "")
    if not token:
        return

    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    except Exception as e:  # noqa: BLE001
        db.log_event("TELEGRAM_ERROR", f"ptb import failed: {e}")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def start(update: Update, ctx):
        await update.message.reply_text(
            "🤖 Trading Bot online.\nCommands: /dashboard /status /stop"
        )

    async def dashboard(update: Update, ctx):
        await update.message.reply_text(build_status_text(), parse_mode="HTML")

    async def status(update: Update, ctx):
        await update.message.reply_text(build_status_text(), parse_mode="HTML")

    async def stop_cmd(update: Update, ctx):
        _pending_stop["ts"] = time.time()
        await update.message.reply_text(
            "⛔ Emergency Stop requested. Reply YES to confirm (10s)"
        )

    async def text_handler(update: Update, ctx):
        txt = (update.message.text or "").strip().upper()
        if txt == "YES" and (time.time() - _pending_stop["ts"]) <= 10:
            _pending_stop["ts"] = 0.0
            db.save_setting("emergency_stop", "1")
            db.save_setting("scalping_bot_on", "0")
            db.save_setting("swing_bot_on", "0")
            await update.message.reply_text("✅ All engines stopped. Positions closing.")
        elif txt == "YES":
            await update.message.reply_text("❌ Stop cancelled (timeout)")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    try:
        app.run_polling(close_loop=False, stop_signals=None)
    except Exception as e:  # noqa: BLE001
        db.log_event("TELEGRAM_ERROR", f"polling failed: {e}")


def start_command_bot():
    """Spawn the command bot thread once (idempotent)."""
    if getattr(start_command_bot, "_started", False):
        return
    if not db.get_setting("telegram_token", ""):
        return
    t = threading.Thread(target=run_command_bot, daemon=True, name="telegram-bot")
    t.start()
    start_command_bot._started = True
