"""
engine.py — Thread 1: the main trading loop.

Started exactly once (guarded by a module flag + st.session_state on the UI
side). Every cycle it re-reads ALL settings from SQLite, scans each selected
pair for both engines, applies the signal aggregator + guards, executes
qualifying signals, then monitors all open positions. SQLite is the only shared
state, so a Streamlit rerun never disturbs this loop.
"""

import time
import json
import threading
from datetime import datetime, timezone

import database as db
from utils import binance_client as bc
from utils import indicators, news
from strategies import trend, reversion, breakout, ai_hybrid
from execution import risk_guard, orders, position_manager
from notifications import telegram_bot as tg

CYCLE_SECONDS = 30

_engine_thread = None
_engine_lock = threading.Lock()
_last_daily_report = {"date": ""}
_last_status = {"ts": 0.0}


# ---------------------------------------------------------------------------
# Aggregator for a single pair + engine
# ---------------------------------------------------------------------------
def _effective_tfs(strategy):
    """Return (entry, confirm, trend) timeframes. When Auto Timeframe is on the
    bot uses sensible presets so the user never has to tune them."""
    if db.get_bool(f"{strategy}_auto_tf", True):
        if strategy == "scalping":
            return "5m", "15m", "1h"
        return "4h", "1d", "3d"
    return (db.get_setting(f"{strategy}_timeframe", "5m"),
            db.get_setting(f"{strategy}_confirm_tf", "15m"),
            db.get_setting(f"{strategy}_trend_tf", "1h"))


def _gather_indicator_frames(symbol, strategy, api_mode):
    entry_tf, confirm_tf, trend_tf = _effective_tfs(strategy)

    # Drop the still-forming last candle so signals are evaluated on CLOSED
    # candles only. Otherwise the live candle wobbles indicators across their
    # thresholds and the signal flickers between fire/NONE, so the 30s engine
    # scan keeps missing it. On a closed candle the signal stays stable for the
    # whole candle period, giving the engine many scans to act on it.
    def _closed(df):
        return df.iloc[:-1] if (df is not None and len(df) > 1) else df

    df_entry = indicators.compute_indicators(_closed(bc.get_ohlcv(symbol, entry_tf, api_mode=api_mode)))
    df_confirm = df_trend = None
    if db.get_bool(f"{strategy}_mtf_filter", True):
        df_confirm = indicators.compute_indicators(_closed(bc.get_ohlcv(symbol, confirm_tf, api_mode=api_mode)))
        df_trend = indicators.compute_indicators(_closed(bc.get_ohlcv(symbol, trend_tf, api_mode=api_mode)))
    return df_entry, df_confirm, df_trend


def _average_levels(signals):
    """Average entry/sl/tp across agreeing signals."""
    n = len(signals)
    keys = ["entry", "sl", "tp1", "tp2", "tp3"]
    return {k: sum(float(s[k]) for s in signals) / n for k in keys}


def aggregate_signal(symbol, strategy, df_entry, df_confirm, df_trend,
                     funding_rate, oi_change, news_score):
    mtf = db.get_bool(f"{strategy}_mtf_filter", True)
    hybrid_on = db.get_bool(f"{strategy}_hybrid_on", True)

    if hybrid_on:
        # AI Hybrid aggregates the base strategies internally, so evaluate ALL
        # three regardless of their individual on/off toggles (those toggles only
        # gate the non-hybrid consensus mode). This is what makes "AI Hybrid
        # only" work even when Trend/Reversion/Breakout are toggled off.
        trend_res = trend.run(df_entry, df_confirm, df_trend, mtf)
        rev_res = reversion.run(df_entry, df_confirm, df_trend, mtf)
        brk_res = breakout.run(df_entry, df_confirm, df_trend, mtf)
        triggered = "+".join(
            name for name, res in [("Trend", trend_res), ("Reversion", rev_res),
                                   ("Breakout", brk_res)]
            if res and res["signal"] != "NONE"
        )
        if db.get_bool(f"{strategy}_auto_threshold", True):
            ai_threshold = risk_guard.recommended_threshold(strategy)
        else:
            ai_threshold = db.get_float(f"{strategy}_ai_threshold", 0.75)
        final = ai_hybrid.run(
            df_entry, trend_res, rev_res, brk_res,
            funding_rate=funding_rate, oi_change_pct=oi_change,
            news_score=news_score, ai_threshold=ai_threshold,
        )
        return final, (triggered or "AI"), float(final.get("lgbm_score", 0.0))

    # --- Non-hybrid consensus mode: only the toggled-on strategies ---
    trend_on = db.get_bool(f"{strategy}_trend_on", True)
    reversion_on = db.get_bool(f"{strategy}_reversion_on", True)
    breakout_on = db.get_bool(f"{strategy}_breakout_on", True)
    trend_res = trend.run(df_entry, df_confirm, df_trend, mtf) if trend_on else None
    rev_res = reversion.run(df_entry, df_confirm, df_trend, mtf) if reversion_on else None
    brk_res = breakout.run(df_entry, df_confirm, df_trend, mtf) if breakout_on else None

    enabled = [s for s in (trend_res, rev_res, brk_res) if s is not None]
    if not enabled:
        return {"signal": "NONE"}, "", 0.0

    triggered = "+".join(
        name for name, res in [("Trend", trend_res), ("Reversion", rev_res),
                               ("Breakout", brk_res)]
        if res and res["signal"] != "NONE"
    )
    non_none = [s for s in enabled if s["signal"] != "NONE"]
    buy_count = sum(1 for s in non_none if s["signal"] == "BUY")
    sell_count = sum(1 for s in non_none if s["signal"] == "SELL")

    # Majority direction wins (unanimity was effectively impossible since trend
    # and reversion fire under opposite conditions).
    if buy_count > sell_count and buy_count > 0:
        lv = _average_levels([s for s in non_none if s["signal"] == "BUY"])
        return {"signal": "BUY", **lv}, triggered, 0.0
    if sell_count > buy_count and sell_count > 0:
        lv = _average_levels([s for s in non_none if s["signal"] == "SELL"])
        return {"signal": "SELL", **lv}, triggered, 0.0
    return {"signal": "NONE"}, triggered, 0.0


# ---------------------------------------------------------------------------
# News filter
# ---------------------------------------------------------------------------
def _apply_news_filter(symbol, strategy, signal):
    if not db.get_bool(f"{strategy}_news_on", False) or signal["signal"] == "NONE":
        return True, 0.0
    cache_min = db.get_int(f"{strategy}_gnews_cache_min", 30)
    hf_min = db.get_float(f"{strategy}_hf_min_score", 0.60)
    score = news.get_sentiment(symbol, cache_min=cache_min)
    if signal["signal"] == "BUY" and score < hf_min:
        return False, score
    if signal["signal"] == "SELL" and score > -hf_min:
        return False, score
    return True, score


# ---------------------------------------------------------------------------
# Per-pair processing
# ---------------------------------------------------------------------------
def _engine_mode(strategy):
    """Per-engine trading mode: 'paper' (simulated on testnet data) or 'real'
    (live orders). Lets Scalping and Swing run different modes at the same time."""
    return "real" if db.get_setting(f"{strategy}_mode", "paper") == "real" else "paper"


def process_pair(symbol, strategy, equity, multiplier, paper_mode, health, api_mode):
    # Skip if we already hold a position for this symbol+strategy.
    for p in db.get_open_positions(strategy=strategy):
        if p["symbol"] == symbol:
            return

    # Post-SL cooldown: after a stop loss the entry candle's signal is unchanged,
    # so re-entering immediately just repeats the same losing trade (this caused
    # the same SOL swing SELL to fire 5x in a row). Block re-entry on this
    # symbol+strategy until the cooldown (one entry-candle long) expires.
    if db.get_bool("sl_cooldown_on", True):
        cd = db.get_setting(f"cooldown_{strategy}_{symbol}", "")
        if cd:
            try:
                until = datetime.strptime(cd, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < until:
                    return
            except Exception:  # noqa: BLE001
                pass

    try:
        df_entry, df_confirm, df_trend = _gather_indicator_frames(symbol, strategy, api_mode)
    except Exception as e:  # noqa: BLE001
        db.log_event("DATA_ERROR", f"{symbol} {strategy}: {e}")
        return

    if not indicators.has_enough(df_entry):
        return

    # Blackout evaluation on the entry frame.
    risk_guard.check_blackout(df_entry)

    # Market context.
    funding_rate = oi_change = 0.0
    if db.get_bool(f"{strategy}_funding_filter", False):
        try:
            funding_rate = bc.get_funding_rate(symbol, api_mode)
            oi_change = bc.get_oi_change_pct(symbol, api_mode)
        except Exception:  # noqa: BLE001
            pass

    signal, triggered, lgbm_score = aggregate_signal(
        symbol, strategy, df_entry, df_confirm, df_trend,
        funding_rate, oi_change, 0.0,
    )

    # News filter.
    news_ok, news_score = _apply_news_filter(symbol, strategy, signal)

    # Log the scan for the dashboard.
    db.log_signal(symbol, triggered or strategy, lgbm_score, news_score,
                  signal["signal"] if news_ok else "NEWS_SKIP")

    if signal["signal"] == "NONE" or not news_ok:
        return

    # Guards (in order). health guard returns (ok, reason, multiplier).
    # Use real_starting_balance when not paper mode so drawdown is measured
    # against the live account's actual starting capital, not the paper one.
    _start_bal = (db.get_float("real_starting_balance", equity) or equity
                  if not paper_mode else db.get_float("starting_balance", equity) or equity)
    guards = [
        risk_guard.apply_health_guard(equity, _start_bal, strategy)[:2],
        risk_guard.apply_daily_loss_guard(equity),
        risk_guard.apply_blackout_guard(),
        risk_guard.apply_correlation_guard(signal["signal"], strategy),
        risk_guard.apply_session_filter(strategy),
        risk_guard.apply_concurrency_guard(),
    ]
    for ok, reason in guards:
        if not ok:
            db.log_event("GUARD_BLOCK", f"{symbol} {strategy}: {reason}")
            return

    # Attach ATR for AI sizing fidelity.
    signal["atr"] = indicators.safe(df_entry.get("atr"))

    # Manual TP/SL override: when Auto TP/SL is off, use the single TP%/SL%
    # sliders (one target) instead of the strategy's structure/ATR levels.
    if not db.get_bool(f"{strategy}_auto_tpsl", True):
        entry = float(signal["entry"])
        tp_pct = db.get_float(f"{strategy}_tp_pct", 1.5)
        sl_pct = db.get_float(f"{strategy}_sl_pct", 0.8)
        if signal["signal"] == "BUY":
            tp = entry * (1 + tp_pct / 100.0)
            signal["sl"] = entry * (1 - sl_pct / 100.0)
        else:
            tp = entry * (1 - tp_pct / 100.0)
            signal["sl"] = entry * (1 + sl_pct / 100.0)
        signal["tp1"] = signal["tp2"] = signal["tp3"] = tp

    # -----------------------------------------------------------------------
    # Adaptive self-learning entry filters
    # The bot learns from its own closed-trade history (real trades only).
    # Each filter only activates once there are enough trades in that context
    # to be statistically meaningful — before then it stays out of the way.
    # -----------------------------------------------------------------------

    # 1. Pair win rate (all directions): if this pair has been a chronic loser
    #    across 15+ real trades, skip until the pattern improves.
    wr_pair, n_pair = db.winrate(strategy, symbol, paper_mode=0)
    if n_pair >= 15 and wr_pair is not None and wr_pair < 40.0:
        db.log_event("WINRATE_SKIP", f"{symbol} {strategy} pair_wr={wr_pair:.0f}% over {n_pair} trades")
        db.log_signal(symbol, triggered or strategy, lgbm_score, news_score, "LOWWR_SKIP")
        return

    # 2. Direction-specific win rate: if e.g. SELL on SOLUSDT has lost 10/10
    #    times, stop entering that direction on this pair until history improves.
    if db.get_bool(f"{strategy}_dir_filter", True):
        wr_dir, n_dir = db.winrate(strategy, symbol,
                                   side=signal["signal"], paper_mode=0)
        if n_dir >= 10 and wr_dir is not None and wr_dir < 35.0:
            db.log_event("DIRWR_SKIP",
                         f"{symbol} {strategy} {signal['signal']} dir_wr={wr_dir:.0f}% over {n_dir} trades")
            db.log_signal(symbol, triggered or strategy, lgbm_score, news_score, "DIRWR_SKIP")
            return

    # 3. Hour-of-day win rate: if this UTC hour has historically lost across
    #    10+ real trades for this strategy, wait for a better hour.
    if db.get_bool(f"{strategy}_hour_filter", True):
        cur_hour = datetime.now(timezone.utc).hour
        wr_hour, n_hour = db.winrate_hour(cur_hour, strategy=strategy)
        if n_hour >= 10 and wr_hour is not None and wr_hour < 35.0:
            db.log_event("HOURWR_SKIP",
                         f"{symbol} {strategy} UTC_hour={cur_hour} hour_wr={wr_hour:.0f}% over {n_hour} trades")
            db.log_signal(symbol, triggered or strategy, lgbm_score, news_score, "HOURWR_SKIP")
            return

    # 4. Session × pair win rate: e.g. ETHUSDT is consistently losing during
    #    the Asian session — skip that combination specifically.
    if db.get_bool(f"{strategy}_session_pair_filter", True):
        cur_session = _current_session()
        wr_sess, n_sess = db.winrate_session_pair(cur_session, symbol, strategy=strategy)
        if n_sess >= 8 and wr_sess is not None and wr_sess < 35.0:
            db.log_event("SESSWR_SKIP",
                         f"{symbol} {strategy} session={cur_session} sess_wr={wr_sess:.0f}% over {n_sess} trades")
            db.log_signal(symbol, triggered or strategy, lgbm_score, news_score, "SESSWR_SKIP")
            return

    # Self-learning: capture the market features of THIS entry so the outcome
    # can be learned from when the trade closes.
    entry_features = indicators.build_features(
        df_entry, funding_rate=funding_rate, oi_change_pct=oi_change, sentiment_score=news_score)

    # Win predictor: once a model has been trained on past wins/losses, only take
    # setups it predicts are likely to WIN (learned from the bot's own history).
    # Requires >= 50 training samples to activate — prevents a model trained on
    # too few trades from blocking all entries with garbage probabilities.
    from models.train import get_win_model
    wm = get_win_model()
    _win_samples = db.get_int("win_model_samples", 0)
    if wm is not None and _win_samples >= 50 and db.get_bool(f"{strategy}_win_filter", True):
        try:
            win_prob = float(wm.predict_proba([entry_features])[0][1])
            min_wp = db.get_float("win_filter_min", 0.45)
            if win_prob < min_wp:
                db.log_event("WINPRED_SKIP", f"{symbol} {strategy} win_prob={win_prob:.2f} < {min_wp}")
                db.log_signal(symbol, triggered or strategy, lgbm_score, win_prob, "WINPRED_SKIP")
                return
        except Exception:  # noqa: BLE001
            pass

    session = _current_session()

    pos_id = orders.execute_order(
        symbol, strategy, signal,
        equity=equity, multiplier=multiplier, api_mode=api_mode,
        paper_mode=paper_mode, funding_rate=funding_rate, open_interest=oi_change,
        session=session, lgbm_score=lgbm_score, news_score=news_score, health=health,
        entry_features=entry_features,
    )
    if pos_id:
        pos = db.get_position(pos_id)
        if pos:
            tg.notify_trade_open(pos)


def _current_session():
    hour = datetime.now(timezone.utc).hour
    if 8 <= hour < 12:
        return "london"
    if 13 <= hour < 17:
        return "ny"
    if 0 <= hour < 4:
        return "asia"
    return "off"


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------
def _maybe_daily_report():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == 0 and _last_daily_report["date"] != today:
        _last_daily_report["date"] = today
        trades = db.get_today_trades()
        wins = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
        net = sum(float(t.get("net_pnl") or 0) for t in trades)
        wr = (wins / len(trades) * 100) if trades else 0.0
        report = (
            "📊 <b>DAILY REPORT</b>\n"
            f"Trades: {len(trades)} | Win rate: {wr:.0f}%\n"
            f"Net PnL: {net:+.2f}"
        )
        tg.notify_daily_report(report)


# ---------------------------------------------------------------------------
# Health-based emergency handling
# ---------------------------------------------------------------------------
def _maybe_status():
    """Send a status snapshot to Telegram every N hours (if enabled)."""
    if not db.get_bool("notify_status_on", True):
        return
    interval = db.get_float("notify_status_interval_hr", 4.0) * 3600.0
    now = time.time()
    if now - _last_status["ts"] >= interval:
        _last_status["ts"] = now
        try:
            tg.notify_status()
        except Exception as e:  # noqa: BLE001
            db.log_event("STATUS_ERROR", str(e))


def _enforce_health(equity):
    starting = db.get_float("starting_balance", equity)
    health = risk_guard.health_ratio(equity, starting)
    if health < 25:
        if db.get_bool("scalping_bot_on") or db.get_bool("swing_bot_on"):
            db.save_setting("scalping_bot_on", "0")
            db.save_setting("swing_bot_on", "0")
            tg.notify_risk_alert(health, "Health < 25%", "ALL ENGINES STOPPED")
            # Cancel real orders for all selected pairs.
            for sym in _selected_pairs():
                for mode in ("test", "real"):
                    try:
                        orders.cancel_all_orders(sym, mode)
                    except Exception:  # noqa: BLE001
                        pass
    return health


def _selected_pairs():
    raw = db.get_setting("selected_pairs", "")
    return [p.strip() for p in raw.split(",") if p.strip()][:10]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def engine_loop(stop_event=None):
    db.log_event("ENGINE", "trading loop started")
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            _run_cycle()
        except Exception as e:  # noqa: BLE001 - loop must survive everything
            db.log_event("ENGINE_ERROR", str(e))
        time.sleep(CYCLE_SECONDS)


def _run_cycle():
    # Determine the api mode for the equity read. Prefer 'real' only if a real
    # engine is configured AND live credentials exist; otherwise fall back to
    # 'test' so a missing live secret can't spam EQUITY_ERROR or shrink sizing.
    real_engine = (_engine_mode("scalping") == "real" or _engine_mode("swing") == "real")
    if real_engine and bc.has_credentials("real"):
        equity_mode = "real"
    elif bc.has_credentials("test"):
        equity_mode = "test"
    else:
        equity_mode = "real" if real_engine else "test"

    # Emergency stop short-circuits everything except position monitoring.
    if db.get_bool("emergency_stop", False):
        position_manager.monitor_all(tg)
        _update_snapshot(db.get_float("last_equity", 0.0), equity_mode, _selected_pairs())
        return

    try:
        real_equity = bc.get_equity(equity_mode)
        db.save_setting("binance_conn", "1")
        db.save_setting("binance_conn_msg", f"Connected ({equity_mode})")
    except Exception as e:  # noqa: BLE001
        db.log_event("EQUITY_ERROR", str(e))
        db.save_setting("binance_conn", "0")
        db.save_setting("binance_conn_msg", f"Not connected: {e}")
        real_equity = db.get_float("starting_balance", 0.0)

    # Seed the starting capital once from the real wallet.
    if db.get_float("starting_balance", 0.0) <= 0 and real_equity > 0:
        db.save_setting("starting_balance", f"{real_equity:.2f}")

    paper_eq = db.paper_balance()
    any_real = (_engine_mode("scalping") == "real" or _engine_mode("swing") == "real")
    # Health/drawdown guard uses the real wallet when any engine trades live,
    # otherwise the simulated paper wallet.
    primary_equity = real_equity if any_real else paper_eq

    health = _enforce_health(primary_equity)
    multiplier = risk_guard.health_multiplier(health)

    if multiplier > 0:
        pairs = _selected_pairs()
        for strategy in ("scalping", "swing"):
            if not db.get_bool(f"{strategy}_bot_on", False):
                continue
            # Per-engine mode: paper -> testnet data + simulated; real -> live.
            paper_mode = _engine_mode(strategy) == "paper"
            api_mode = "test" if paper_mode else "real"
            eng_equity = paper_eq if paper_mode else real_equity
            for symbol in pairs:
                try:
                    process_pair(symbol, strategy, eng_equity, multiplier, paper_mode, health, api_mode)
                except Exception as e:  # noqa: BLE001
                    db.log_event("PAIR_ERROR", f"{symbol} {strategy}: {e}")

    # Always monitor open positions and check the daily report + status push.
    position_manager.monitor_all(tg)
    _maybe_daily_report()
    _maybe_status()

    # Publish a snapshot to the DB so the UI never has to call Binance itself.
    _update_snapshot(primary_equity, equity_mode, _selected_pairs())


def _update_snapshot(equity, equity_mode, pairs):
    """
    Write equity + live prices + first-pair market context into the settings
    table. The Streamlit UI reads ONLY these (no direct Binance calls), which
    keeps the dashboard instant regardless of network/API latency.
    """
    try:
        db.save_setting("last_equity", f"{equity:.4f}")
        db.save_setting("snapshot_ts", db.utcnow_str())

        # Read BOTH balances (test + live) for the dashboard, when keys exist.
        for mode, store_key in (("test", "last_equity_test"), ("real", "last_equity_live")):
            if bc.has_credentials(mode):
                try:
                    db.save_setting(store_key, f"{bc.get_equity(mode):.4f}")
                except Exception:  # noqa: BLE001
                    db.save_setting(store_key, "ERR")
            else:
                db.save_setting(store_key, "")

        # Prices for every open position (+ first selected pair for context).
        syms = {p["symbol"] for p in db.get_open_positions()}
        if pairs:
            syms.add(pairs[0])
        prices = {}
        for s in syms:
            try:
                prices[s] = bc.get_price(s, equity_mode)
            except Exception:  # noqa: BLE001
                pass
        db.save_setting("live_prices", json.dumps(prices))

        # Market context (funding / OI) for the first selected pair, best-effort.
        if pairs:
            try:
                fr = bc.get_funding_rate(pairs[0], equity_mode)
                oi = bc.get_oi_change_pct(pairs[0], equity_mode)
                db.save_setting("live_market",
                                json.dumps({"symbol": pairs[0], "funding": fr, "oi": oi}))
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        db.log_event("SNAPSHOT_ERROR", str(e))


def start_engine():
    """Start the engine thread exactly once for this process."""
    global _engine_thread
    with _engine_lock:
        if _engine_thread is not None and _engine_thread.is_alive():
            return
        _engine_thread = threading.Thread(target=engine_loop, daemon=True, name="trading-engine")
        _engine_thread.start()
