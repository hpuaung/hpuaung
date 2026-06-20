"""
execution/orders.py — equity-based position sizing + Futures order placement.

Handles both paper and real modes. In paper mode the position is simulated in
the DB only; in real mode it sets leverage, fires a MARKET entry, a STOP_MARKET
stop loss and one or more TAKE_PROFIT_MARKET targets (partial TP aware).
"""

from utils import binance_client as bc
from utils.rate_limiter import rate_limited, with_retry
from execution import risk_guard
import database as db

TAKER_FEE = 0.0004  # 0.04% per side


def _sizing(symbol, strategy, signal, equity, multiplier, api_mode):
    """
    Compute effective leverage/risk and the order quantity. Returns a dict with
    sizing details or {"blocked": reason}.
    """
    auto_risk = db.auto_flag(f"{strategy}_auto_risk", True)

    if auto_risk:
        # Fully automatic: base sized from balance + win-rate history, then
        # scaled by the health multiplier (the user sets nothing).
        base_lev = risk_guard.recommended_leverage(equity)
        base_risk = risk_guard.recommended_risk(strategy)
        eff_lev = max(1, int(round(base_lev * multiplier)))
        eff_risk = base_risk * multiplier
    else:
        base_lev = db.get_int(f"{strategy}_base_leverage", 5)
        base_risk = db.get_float(f"{strategy}_base_risk_pct", 1.0)
        eff_lev = base_lev
        eff_risk = base_risk

    # Win/loss streak adjustment (added to the risk %).
    eff_risk = max(0.1, eff_risk + risk_guard.streak_risk_adjustment())

    # Hard cap guard (always active).
    ok, reason = risk_guard.apply_hard_cap_guard(eff_lev, eff_risk)
    if not ok:
        return {"blocked": reason}

    filters = bc.get_filters(symbol, api_mode)
    entry = float(signal["entry"])
    sl = float(signal["sl"])
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return {"blocked": "SL distance is zero"}

    risk_amount = equity * eff_risk / 100.0
    raw_qty = risk_amount / sl_distance
    qty = bc.truncate_qty(raw_qty, filters["stepSize"])

    if qty < filters["minQty"]:
        return {"blocked": f"qty {qty} < minQty {filters['minQty']}"}
    if qty > filters["maxQty"]:
        qty = filters["maxQty"]

    return {
        "eff_lev": eff_lev,
        "eff_risk": eff_risk,
        "qty": qty,
        "entry": bc.round_price(entry, filters["tickSize"]),
        "sl": bc.round_price(sl, filters["tickSize"]),
        "tp1": bc.round_price(float(signal["tp1"]), filters["tickSize"]),
        "tp2": bc.round_price(float(signal["tp2"]), filters["tickSize"]),
        "tp3": bc.round_price(float(signal["tp3"]), filters["tickSize"]),
        "filters": filters,
    }


@with_retry()
@rate_limited(weight=1)
def _set_leverage(client, symbol, leverage):
    return client.futures_change_leverage(symbol=symbol, leverage=leverage)


@with_retry()
@rate_limited(weight=1)
def _market_entry(client, symbol, side, qty):
    return client.futures_create_order(
        symbol=symbol, side=side, type="MARKET", quantity=qty
    )


@with_retry()
@rate_limited(weight=1)
def _stop_market(client, symbol, close_side, stop_price):
    return client.futures_create_order(
        symbol=symbol, side=close_side, type="STOP_MARKET",
        stopPrice=stop_price, closePosition=True,
    )


@with_retry()
@rate_limited(weight=1)
def _take_profit(client, symbol, close_side, stop_price, qty=None, close_all=False):
    params = dict(symbol=symbol, side=close_side, type="TAKE_PROFIT_MARKET",
                  stopPrice=stop_price)
    if close_all:
        params["closePosition"] = True
    else:
        params["quantity"] = qty
        params["reduceOnly"] = True
    return client.futures_create_order(**params)


def execute_order(symbol, strategy, signal, *, equity, multiplier, api_mode,
                  paper_mode, funding_rate=0.0, open_interest=0.0, session="",
                  lgbm_score=0.0, news_score=0.0, health=100.0, entry_features=None):
    """
    Place (or simulate) an order from a final signal. Returns the new
    active_positions row id, or None if blocked/failed.
    """
    side = signal["signal"]  # BUY or SELL
    if side not in ("BUY", "SELL"):
        return None

    sized = _sizing(symbol, strategy, signal, equity, multiplier, api_mode)
    if "blocked" in sized:
        db.log_event("ORDER_BLOCKED", f"{symbol} {strategy}: {sized['blocked']}")
        return None

    qty = sized["qty"]
    entry = sized["entry"]
    fees_estimated = entry * qty * TAKER_FEE * 2  # round trip estimate

    # Partial TP only applies in Auto TP/SL mode; manual mode is a single target.
    partial_tp = db.get_bool(f"{strategy}_partial_tp", True) and db.auto_flag(f"{strategy}_auto_tpsl", True)
    trail_auto = 1 if db.auto_flag(f"{strategy}_trail_auto", False) else 0
    tf = db.get_setting(f"{strategy}_timeframe", "5m")
    atr_at_entry = float(signal.get("atr", 0.0)) if signal.get("atr") else 0.0

    position = {
        "symbol": symbol,
        "strategy": strategy,
        "side": side,
        "entry_price": entry,
        "entry_qty": qty,
        "sl_price": sized["sl"],
        "tp1": sized["tp1"],
        "tp2": sized["tp2"],
        "tp3": sized["tp3"],
        "status": "open",
        "timestamp": db.utcnow_str(),
        "leverage": sized["eff_lev"],
        "timeframe": tf,
        "atr_at_entry": atr_at_entry,
        "trailing_active": trail_auto,
        "entry_features": (__import__("json").dumps([float(x) for x in entry_features])
                           if entry_features is not None else None),
        "paper_mode": 1 if paper_mode else 0,
        "order_id": "",
        "health_at_entry": health,
        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "session": session,
        "lgbm_score": lgbm_score,
        "news_score": news_score,
        "effective_leverage": sized["eff_lev"],
        "effective_risk_pct": sized["eff_risk"],
        "fees_estimated": fees_estimated,
    }

    # --- Paper mode: simulate only ---
    if paper_mode:
        # Paper realism: a real market entry fills WORSE than the signal price
        # (BUY higher, SELL lower) due to spread + slippage. Apply the same
        # adverse slippage to the paper entry so paper PnL is not over-optimistic.
        slip = db.get_float("paper_slippage_pct", 0.05) / 100.0
        d = 1 if side == "BUY" else -1
        filled = bc.round_price(entry * (1 + slip * d), sized["filters"]["tickSize"])
        position["entry_price"] = filled
        position["fees_estimated"] = filled * qty * TAKER_FEE * 2
        pos_id = db.insert_position(position)
        db.log_event("PAPER_OPEN", f"{symbol} {side} qty={qty} @ {filled} (signal {entry})")
        return pos_id

    # --- Real mode ---
    # Insert the position record BEFORE placing exchange orders so that if the
    # DB write fails, no funds move. The inverse (orders first, DB after) leaves
    # live positions invisible to the bot if the DB write crashes.
    try:
        client = bc.get_client(api_mode)
        _set_leverage(client, symbol, sized["eff_lev"])
        close_side = "SELL" if side == "BUY" else "BUY"

        pos_id = db.insert_position(position)
    except Exception as e:  # noqa: BLE001
        db.log_event("ORDER_ERROR", f"{symbol} {strategy} pre-order setup: {e}")
        return None

    try:
        entry_order = _market_entry(client, symbol, side, qty)
        db.update_position(pos_id, {"order_id": str(entry_order.get("orderId", ""))})

        _stop_market(client, symbol, close_side, sized["sl"])

        if partial_tp:
            c1 = db.get_float(f"{strategy}_tp1_close_pct", 50) / 100.0
            c2 = db.get_float(f"{strategy}_tp2_close_pct", 30) / 100.0
            q1 = bc.truncate_qty(qty * c1, sized["filters"]["stepSize"])
            q2 = bc.truncate_qty(qty * c2, sized["filters"]["stepSize"])
            if q1 >= sized["filters"]["minQty"]:
                _take_profit(client, symbol, close_side, sized["tp1"], qty=q1)
            if q2 >= sized["filters"]["minQty"]:
                _take_profit(client, symbol, close_side, sized["tp2"], qty=q2)
            # Remaining quantity closes at TP3.
            _take_profit(client, symbol, close_side, sized["tp3"], close_all=True)
        else:
            _take_profit(client, symbol, close_side, sized["tp1"], close_all=True)

        db.log_event("REAL_OPEN", f"{symbol} {side} qty={qty} @ {entry} pos_id={pos_id}")
        return pos_id
    except Exception as e:  # noqa: BLE001
        # Entry may or may not have filled — position is recorded so the user
        # can reconcile manually against Binance. Log with pos_id for tracing.
        db.log_event("ORDER_ERROR_PARTIAL",
                     f"{symbol} {strategy} pos_id={pos_id}: {e} — check Binance dashboard")
        return pos_id


@with_retry(max_retries=3, base_delay=3)
@rate_limited(weight=1)
def market_close(symbol, side, qty, api_mode):
    """Close (part of) a real position with a reduce-only MARKET order."""
    client = bc.get_client(api_mode)
    close_side = "SELL" if side == "BUY" else "BUY"
    return client.futures_create_order(
        symbol=symbol, side=close_side, type="MARKET",
        quantity=qty, reduceOnly=True,
    )


@with_retry(max_retries=3, base_delay=3)
@rate_limited(weight=1)
def cancel_all_orders(symbol, api_mode):
    client = bc.get_client(api_mode)
    return client.futures_cancel_all_open_orders(symbol=symbol)
