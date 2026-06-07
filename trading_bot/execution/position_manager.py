"""
execution/position_manager.py — monitors open positions every cycle.

For each open position it: fetches the current price, evaluates partial TPs, the
stop loss, the trailing stop and (for swing) the max-hold limit. Each closed
fraction is written to the `trades` table as its own row, so partial PnL is
recorded accurately. When the last fraction closes, the active_positions row is
removed.

Real positions also have protective STOP_MARKET / TAKE_PROFIT_MARKET orders on
the exchange (placed at entry); when this monitor decides to close, it cancels
the symbol's remaining orders first to avoid duplicate fills.
"""

from datetime import datetime, timezone

import database as db
from utils import binance_client as bc
from execution import orders, risk_guard

TAKER_FEE = 0.0004


def _maybe_train_win_model():
    """Retrain the win predictor every 10 new closed trades, once there are
    enough samples to learn from."""
    n = db.learning_count()
    if n >= 30 and n % 10 == 0:
        from models import train as _t
        _t.train_win_model_in_background()


def _api_mode(strategy):
    return "real" if db.get_setting(f"{strategy}_api_mode") == "real" else "test"


def _dir(side):
    return 1 if side == "BUY" else -1


def _remaining_fraction(pos):
    frac = 1.0
    strat = pos["strategy"]
    if pos["tp1_closed"]:
        frac -= db.get_float(f"{strat}_tp1_close_pct", 50) / 100.0
    if pos["tp2_closed"]:
        frac -= db.get_float(f"{strat}_tp2_close_pct", 30) / 100.0
    return max(0.0, frac)


def _hold_duration(pos):
    try:
        start = datetime.strptime(pos["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - start
        hours = delta.total_seconds() / 3600.0
        return hours, f"{hours:.1f}h"
    except Exception:  # noqa: BLE001
        return 0.0, "0h"


def _record_close(pos, qty, exit_price, reason, notifier=None):
    """Write a (partial) close to the trades table and update the streak."""
    entry = float(pos["entry_price"])
    side = pos["side"]
    d = _dir(side)
    gross = (exit_price - entry) * qty * d
    fees = (entry * qty + exit_price * qty) * TAKER_FEE
    net = gross - fees
    lev = pos.get("leverage") or 1
    pnl_pct = (exit_price - entry) / max(entry, 1e-9) * 100.0 * d * lev
    hours, hold_str = _hold_duration(pos)

    trade = {
        "timestamp": db.utcnow_str(),
        "strategy": pos["strategy"],
        "pair": pos["symbol"],
        "side": side,
        "entry_price": entry,
        "exit_price": exit_price,
        "qty": qty,
        "leverage": lev,
        "pnl_amount": gross,
        "pnl_percent": pnl_pct,
        "fees_paid": fees,
        "net_pnl": net,
        "close_reason": reason,
        "paper_mode": pos.get("paper_mode", 0),
        "status": "closed",
        "hold_duration": hold_str,
        "lgbm_score": pos.get("lgbm_score"),
        "news_score": pos.get("news_score"),
        "session": pos.get("session"),
        "entry_timestamp": pos["timestamp"],
        "exit_timestamp": db.utcnow_str(),
    }
    db.insert_trade(trade)
    risk_guard.update_streak(net)
    # Self-learning: pair the entry features with the win/loss outcome.
    feats = pos.get("entry_features")
    if feats:
        try:
            db.add_learning(pos["strategy"], pos["symbol"], feats, net > 0, net)
            _maybe_train_win_model()
        except Exception:  # noqa: BLE001
            pass
    if notifier:
        try:
            notifier.notify_trade_close(trade)
        except Exception:  # noqa: BLE001
            pass


def _real_close(pos, qty, api_mode):
    """Cancel remaining protective orders and market-close `qty` (real mode)."""
    try:
        orders.market_close(pos["symbol"], pos["side"], qty, api_mode)
    except Exception as e:  # noqa: BLE001
        db.log_event("CLOSE_ERROR", f"{pos['symbol']}: {e}")


def _close_full(pos, exit_price, reason, api_mode, notifier=None):
    """Close the remaining fraction and remove the active position."""
    qty = round(float(pos["entry_qty"]) * _remaining_fraction(pos), 8)
    if not pos.get("paper_mode"):
        try:
            orders.cancel_all_orders(pos["symbol"], api_mode)
        except Exception:  # noqa: BLE001
            pass
        if qty > 0:
            _real_close(pos, qty, api_mode)
    if qty > 0:
        _record_close(pos, qty, exit_price, reason, notifier)
    db.update_position(pos["id"], {"status": "closed"})
    db.delete_position(pos["id"])


def _close_partial(pos, tp_level, exit_price, close_pct, api_mode, notifier=None):
    qty = round(float(pos["entry_qty"]) * (close_pct / 100.0), 8)
    if qty <= 0:
        return
    if not pos.get("paper_mode"):
        _real_close(pos, qty, api_mode)
    _record_close(pos, qty, exit_price, f"TP{tp_level}", notifier)


def process_position(pos, notifier=None):
    """Evaluate a single open position against the live price."""
    strat = pos["strategy"]
    api_mode = _api_mode(strat)
    side = pos["side"]
    d = _dir(side)

    try:
        price = bc.get_price(pos["symbol"], api_mode)
    except Exception as e:  # noqa: BLE001
        db.log_event("PRICE_ERROR", f"{pos['symbol']}: {e}")
        return

    entry = float(pos["entry_price"])
    partial = db.get_bool(f"{strat}_partial_tp", True)
    auto_be = db.get_bool(f"{strat}_auto_be", True)

    # --- Partial take profits ---
    if partial:
        # TP1
        if not pos["tp1_closed"] and (
            (d > 0 and price >= pos["tp1"]) or (d < 0 and price <= pos["tp1"])
        ):
            _close_partial(pos, 1, pos["tp1"], db.get_float(f"{strat}_tp1_close_pct", 50),
                           api_mode, notifier)
            updates = {"tp1_closed": 1}
            if auto_be:
                updates["sl_price"] = entry
            db.update_position(pos["id"], updates)
            pos["tp1_closed"] = 1
            pos["sl_price"] = entry if auto_be else pos["sl_price"]

        # TP2
        if not pos["tp2_closed"] and (
            (d > 0 and price >= pos["tp2"]) or (d < 0 and price <= pos["tp2"])
        ):
            _close_partial(pos, 2, pos["tp2"], db.get_float(f"{strat}_tp2_close_pct", 30),
                           api_mode, notifier)
            db.update_position(pos["id"], {"tp2_closed": 1})
            pos["tp2_closed"] = 1

        # TP3 -> close remainder
        if (d > 0 and price >= pos["tp3"]) or (d < 0 and price <= pos["tp3"]):
            _close_full(pos, pos["tp3"], "TP3", api_mode, notifier)
            return
    else:
        # Single TP closes everything.
        if (d > 0 and price >= pos["tp1"]) or (d < 0 and price <= pos["tp1"]):
            _close_full(pos, pos["tp1"], "TP", api_mode, notifier)
            return

    # --- Stop loss ---
    sl = float(pos["sl_price"])
    if (d > 0 and price <= sl) or (d < 0 and price >= sl):
        _close_full(pos, sl, "SL", api_mode, notifier)
        return

    # --- Trailing stop ---
    if pos.get("trailing_active"):
        # Auto TP/SL mode → trailing distance adapts to volatility (ATR at
        # entry); manual mode → use the user's distance slider.
        if db.get_bool(f"{strat}_auto_tpsl", True):
            atr = float(pos.get("atr_at_entry") or 0.0)
            trail_pct = max(0.3, atr / entry * 100.0) if (atr > 0 and entry > 0) \
                else db.get_float(f"{strat}_trail_pct", 1.5)
        else:
            trail_pct = db.get_float(f"{strat}_trail_pct", 1.5)
        cur_trail = pos.get("trail_sl_price") or 0.0
        if d > 0:
            # Only start trailing once the trade is in profit by trail_pct, so a
            # fresh position is not whipsawed out for a tiny loss right after
            # entry. Until then the fixed SL protects the downside.
            profit_trigger = entry * (1 + trail_pct / 100.0)
            if price >= profit_trigger:
                new_trail = price * (1 - trail_pct / 100.0)
                if new_trail > cur_trail:
                    db.update_position(pos["id"], {"trail_sl_price": new_trail})
                    cur_trail = new_trail
            if cur_trail > 0 and price <= cur_trail:
                _close_full(pos, cur_trail, "Trail", api_mode, notifier)
                return
        else:
            profit_trigger = entry * (1 - trail_pct / 100.0)
            if price <= profit_trigger:
                new_trail = price * (1 + trail_pct / 100.0)
                if cur_trail == 0 or new_trail < cur_trail:
                    db.update_position(pos["id"], {"trail_sl_price": new_trail})
                    cur_trail = new_trail
            if cur_trail > 0 and price >= cur_trail:
                _close_full(pos, cur_trail, "Trail", api_mode, notifier)
                return

    # --- Max hold days (swing only) ---
    if strat == "swing":
        max_days = 7 if db.get_bool("swing_auto_maxhold", True) else db.get_int("swing_max_hold_days", 7)
        hours, _ = _hold_duration(pos)
        if hours >= max_days * 24:
            _close_full(pos, price, "MaxDays", api_mode, notifier)
            return


def monitor_all(notifier=None):
    """Process every open position once."""
    for pos in db.get_open_positions():
        try:
            process_position(pos, notifier)
        except Exception as e:  # noqa: BLE001
            db.log_event("MONITOR_ERROR", f"pos {pos.get('id')}: {e}")


def unrealized_pnl(pos, price):
    """Helper for the UI: current unrealized PnL (amount, percent)."""
    entry = float(pos["entry_price"])
    qty = float(pos["entry_qty"])
    d = _dir(pos["side"])
    amount = (price - entry) * qty * d
    lev = pos.get("leverage") or 1
    pct = (price - entry) / max(entry, 1e-9) * 100.0 * d * lev
    return amount, pct
