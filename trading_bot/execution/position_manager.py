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

from datetime import datetime, timezone, timedelta

import database as db
from utils import binance_client as bc
from execution import orders, risk_guard

TAKER_FEE = 0.0004

# Entry-timeframe -> minutes, used to size the post-SL re-entry cooldown.
# NOTE: every timeframe an engine can run MUST be here. A missing key fell back
# to the 15-minute default, so a 12h swing trade re-entered every 15 min inside
# the same (unchanged) candle — one pair stopped out 11x in a row (-$9). 8h/12h
# were the gaps.
_TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60,
               "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
               "1d": 1440, "3d": 4320, "1w": 10080}


def _tf_to_minutes(tf):
    """Entry candle length in minutes. Falls back to PARSING the string (e.g.
    '12h' -> 720) instead of a fixed 15 so a timeframe missing from the table can
    never again shrink the cooldown to 15 min and cause re-entry spirals."""
    if tf in _TF_MINUTES:
        return _TF_MINUTES[tf]
    try:
        return int(tf[:-1]) * {"m": 1, "h": 60, "d": 1440, "w": 10080}[tf[-1]]
    except Exception:  # noqa: BLE001
        return 15


def _set_sl_cooldown(pos):
    """After a stop loss, block re-entry on this symbol+strategy for roughly one
    entry candle so the unchanged signal does not immediately re-fire."""
    tf = pos.get("timeframe", "5m")
    mins = max(_tf_to_minutes(tf), 15)  # 15-minute floor for fast scalps
    until = datetime.now(timezone.utc) + timedelta(minutes=mins)
    db.save_setting(f"cooldown_{pos['strategy']}_{pos['symbol']}",
                    until.strftime("%Y-%m-%d %H:%M:%S"))


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
    # Paper realism: stop-market exits (SL / Trail / MaxDays) fill WORSE than the
    # trigger price on a real exchange, while TP limit orders fill at the exact
    # price. Apply adverse slippage to paper stop-market fills so paper PnL
    # mirrors real conditions instead of idealised exact-price exits. Without
    # this, paper looks profitable while real loses on the slippage difference.
    if pos.get("paper_mode") and reason in ("SL", "Trail", "MaxDays"):
        slip = db.get_float("paper_slippage_pct", 0.05) / 100.0
        exit_price = exit_price * (1 - slip * d)  # always worse for the position
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
        # default 1 (paper), not 0 — a missing flag must not mislabel a paper
        # trade as real (that also made clear_paper_trades miss them).
        "paper_mode": pos.get("paper_mode", 1),
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
    # Re-entry cooldown after EVERY close — one entry candle, win OR loss.
    # Exempting winners let a WINNING phantom re-fire every scan: ETC re-entered
    # ~250x in a night, compounding a fake +$790. A genuine trend is still
    # re-enterable on the NEXT candle, which is all a swing/scalp needs; nothing
    # legitimate needs to re-open the same pair 48 seconds after closing.
    try:
        _set_sl_cooldown(pos)
    except Exception:  # noqa: BLE001
        pass
    # Self-learning: pair the entry features with the win/loss outcome.
    feats = pos.get("entry_features")
    if feats:
        try:
            db.add_learning(pos["strategy"], pos["symbol"], feats, net > 0, net,
                            paper_mode=bool(pos.get("paper_mode", 1)))
            _maybe_train_win_model()
        except Exception:  # noqa: BLE001
            pass
    if notifier:
        try:
            notifier.notify_trade_close(trade)
        except Exception:  # noqa: BLE001
            pass


def _real_close(pos, qty, api_mode):
    """Market-close `qty` (real mode). Returns True on success, False on failure."""
    try:
        orders.market_close(pos["symbol"], pos["side"], qty, api_mode)
        return True
    except Exception as e:  # noqa: BLE001
        db.log_event("CLOSE_ERROR", f"{pos['symbol']}: {e}")
        return False


def _close_full(pos, exit_price, reason, api_mode, notifier=None):
    """Close the remaining fraction and remove the active position."""
    qty = float(pos["entry_qty"]) * _remaining_fraction(pos)
    if not pos.get("paper_mode"):
        # Truncate to the symbol's LOT_SIZE step — a round()'d fraction is almost
        # never a valid multiple, so Binance would reject the close.
        try:
            qty = bc.truncate_qty(qty, bc.get_filters(pos["symbol"], api_mode)["stepSize"])
        except Exception:  # noqa: BLE001
            qty = round(qty, 8)
        # Close FIRST; only cancel the protective SL/TP and drop the position row
        # if the close actually SUCCEEDED. Cancelling the stops before a close
        # that then fails would leave a NAKED real position with unbounded risk.
        if qty > 0 and not _real_close(pos, qty, api_mode):
            db.log_event("CLOSE_FAILED",
                         f"{pos['symbol']} real close failed — position + stops kept, retry next scan")
            return
        try:
            orders.cancel_all_orders(pos["symbol"], api_mode)
        except Exception:  # noqa: BLE001
            pass
    else:
        qty = round(qty, 8)
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
    # A position keeps the data source it was opened with (paper -> testnet).
    api_mode = "test" if pos.get("paper_mode") else "real"
    side = pos["side"]
    d = _dir(side)

    try:
        price = bc.get_price(pos["symbol"], api_mode)
    except Exception as e:  # noqa: BLE001
        db.log_event("PRICE_ERROR", f"{pos['symbol']}: {e}")
        return

    entry = float(pos["entry_price"])
    partial = db.get_bool(f"{strat}_partial_tp", True)
    auto_be = db.auto_flag(f"{strat}_auto_be", True)

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
                # Lock part of the TP1 move as profit on the runner instead of a
                # pure break-even stop. With pure BE, any retrace closed the
                # remaining 50% at $0, so a winner that only tagged TP1 booked
                # less than a full-SL loss (inverted risk:reward). Locking 30%
                # of the TP1 distance keeps the runner net-positive.
                lock_frac = db.get_float(f"{strat}_be_lock_frac", 0.30)
                be_price = entry + (float(pos["tp1"]) - entry) * lock_frac
                updates["sl_price"] = be_price
            db.update_position(pos["id"], updates)
            pos["tp1_closed"] = 1
            pos["sl_price"] = updates.get("sl_price", pos["sl_price"])

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
        if db.auto_flag(f"{strat}_auto_tpsl", True):
            atr = float(pos.get("atr_at_entry") or 0.0)
            trail_pct = max(0.3, atr / entry * 100.0) if (atr > 0 and entry > 0) \
                else db.get_float(f"{strat}_trail_pct", 1.5)
        else:
            trail_pct = db.get_float(f"{strat}_trail_pct", 1.5)
        cur_trail = pos.get("trail_sl_price") or 0.0
        # Arm the trail only after the trade is up by 2x the trail distance, then
        # trail 1x behind. This locks in ~1x trail_pct of REAL profit instead of
        # closing winners at break-even (the old 1x trigger trailed 1x behind, so
        # the stop sat at entry and tiny wobbles closed winners for ~$0).
        arm_pct = trail_pct * 2.0
        if d > 0:
            profit_trigger = entry * (1 + arm_pct / 100.0)
            if price >= profit_trigger:
                new_trail = price * (1 - trail_pct / 100.0)
                if new_trail > cur_trail:
                    db.update_position(pos["id"], {"trail_sl_price": new_trail})
                    cur_trail = new_trail
            if cur_trail > 0 and price <= cur_trail:
                _close_full(pos, cur_trail, "Trail", api_mode, notifier)
                return
        else:
            profit_trigger = entry * (1 - arm_pct / 100.0)
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
        max_days = 7 if db.auto_flag("swing_auto_maxhold", True) else db.get_int("swing_max_hold_days", 7)
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
