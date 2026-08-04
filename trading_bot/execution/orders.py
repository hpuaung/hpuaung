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

    # Auto mode self-scales risk DOWN to stay within the mandatory Lev x Risk cap,
    # instead of letting apply_hard_cap_guard block every entry once a winning
    # account's recommended risk pushes the product over the cap. Manual mode
    # still hits the guard so the user is told to lower their own inputs.
    if auto_risk:
        _cap = db.get_float("lev_risk_hard_cap_pct", 10.0)
        if eff_lev > 0 and eff_lev * eff_risk > _cap:
            eff_risk = max(0.1, _cap / eff_lev)

    # Hard cap guard (always active).
    ok, reason = risk_guard.apply_hard_cap_guard(eff_lev, eff_risk)
    if not ok:
        return {"blocked": reason}

    filters = bc.get_filters(symbol, api_mode)

    # Enter at the LIVE price, not the signal's candle-close. On higher timeframes
    # that close can be HOURS old; opening there while the market has already moved
    # made the position instant-close (0.0h) and re-fire every scan — a phantom
    # spiral that "won" 250x on ETC / "lost" 11x on AAVE and faked a +$790 balance.
    # Shift SL/TP by the same offset so the SL distance and R:R are preserved
    # exactly and the trade can never instant-close.
    sig_entry = float(signal["entry"])
    sl = float(signal["sl"])
    tp1 = float(signal["tp1"]); tp2 = float(signal["tp2"]); tp3 = float(signal["tp3"])
    sig_sl_dist = abs(sig_entry - sl)
    try:
        live = bc.get_price(symbol, api_mode)
    except Exception:  # noqa: BLE001
        live = 0.0
    # No live price -> do NOT fall back to the stale candle close: that recreates
    # the instant-close phantom the live-shift was written to kill. Skip instead.
    if not live or live <= 0:
        return {"blocked": "no live price for entry"}
    # If the market has already moved more than one SL-distance away from the
    # signal's candle close, the move happened without us — chasing a dead thesis.
    # Skip rather than enter at an arbitrary shifted price.
    if sig_sl_dist > 0 and abs(live - sig_entry) > sig_sl_dist:
        return {"blocked": f"live moved >1R from signal ({sig_entry}->{live}), stale skip"}
    off = live - sig_entry
    entry = live
    sl += off; tp1 += off; tp2 += off; tp3 += off

    # Reject signals whose SL/TP1 landed on the WRONG side of entry — e.g. trend's
    # ema50 SL when price is on the far side of ema50, or a breakout TP1 that sits
    # below entry on a large break. The backtest's _valid() skips these; live must
    # too, or they instant-close/-stop the moment they open.
    _side = signal["signal"]
    if _side == "BUY" and not (sl < entry < tp1):
        return {"blocked": f"BUY sl/tp1 wrong side (sl={sl:.6g} e={entry:.6g} tp={tp1:.6g})"}
    if _side == "SELL" and not (tp1 < entry < sl):
        return {"blocked": f"SELL sl/tp1 wrong side (tp={tp1:.6g} e={entry:.6g} sl={sl:.6g})"}

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
    # Binance rejects sub-minNotional orders (qty*price below ~$5). Block here so
    # a real entry never silently fails (and a paper entry never simulates a fill
    # a real account could not actually place).
    if entry * qty < filters.get("minNotional", 5.0):
        return {"blocked": f"notional {entry*qty:.2f} < min {filters.get('minNotional', 5.0)}"}

    return {
        "eff_lev": eff_lev,
        "eff_risk": eff_risk,
        "qty": qty,
        "entry": bc.round_price(entry, filters["tickSize"]),
        "sl": bc.round_price(sl, filters["tickSize"]),
        "tp1": bc.round_price(tp1, filters["tickSize"]),
        "tp2": bc.round_price(tp2, filters["tickSize"]),
        "tp3": bc.round_price(tp3, filters["tickSize"]),
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


def _num(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


@with_retry(max_retries=2, base_delay=1)
@rate_limited(weight=1)
def _get_order(client, symbol, order_id):
    """Read back a placed order to get its real fill (avgPrice / executedQty)."""
    return client.futures_get_order(symbol=symbol, orderId=order_id)


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
        # Read the ACTUAL fill. A create-order ack can come back before the fill
        # is reflected, so re-read the order once if avgPrice isn't populated, and
        # size the SL off what ACTUALLY filled (executedQty), not the request.
        _fill = _num(entry_order.get("avgPrice"))
        _filled_qty = _num(entry_order.get("executedQty"))
        if _fill <= 0 and entry_order.get("orderId"):
            try:
                _chk = _get_order(client, symbol, entry_order["orderId"])
                _fill = _num(_chk.get("avgPrice")) or _fill
                _filled_qty = _num(_chk.get("executedQty")) or _filled_qty
            except Exception:  # noqa: BLE001
                pass
        if _filled_qty > 0:
            qty = _filled_qty                       # authoritative filled size
        _upd = {"order_id": str(entry_order.get("orderId", ""))}
        if _fill > 0:
            _upd["entry_price"] = _fill
        if qty > 0:
            _upd["entry_qty"] = qty
            _upd["fees_estimated"] = (_fill or entry) * qty * TAKER_FEE * 2
        db.update_position(pos_id, _upd)

        # SL is MANDATORY. A filled entry with no resting stop is unbounded risk
        # if the process dies before the software monitor can act. If the stop
        # cannot be placed even after retries, FLATTEN the entry immediately
        # instead of returning a naked position.
        try:
            _stop_market(client, symbol, close_side, sized["sl"])
        except Exception as _sl_err:  # noqa: BLE001
            db.log_event("SL_FAILED_FLATTEN",
                         f"{symbol} {strategy}: stop placement failed ({_sl_err}) — flattening entry")
            try:
                market_close(symbol, side, qty, api_mode)
            except Exception as _fe:  # noqa: BLE001
                db.log_event("FLATTEN_FAILED",
                             f"{symbol} {strategy}: could NOT flatten after SL failure ({_fe}) "
                             "— NAKED POSITION, manual action needed on Binance")
            db.update_position(pos_id, {"status": "closed"})
            db.delete_position(pos_id)
            return None

        # TP1/2/3 + trailing are managed by the SOFTWARE monitor only — we do NOT
        # place resting TP orders on the exchange. Otherwise the exchange and the
        # monitor both act on the same position: an exchange TP filling between
        # scans caused double-closes and desynced/fabricated PnL. The exchange SL
        # above stays as a dead-man's switch; process_position reconciles if it
        # fills between scans.
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
