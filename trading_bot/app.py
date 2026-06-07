"""
app.py — Streamlit mobile-friendly dashboard for the Binance Futures bot.

This is the main process: `streamlit run app.py`. On first load it initialises
the database and starts the background threads (trading engine, VPS monitor,
Telegram bot) exactly once via st.session_state guards. Every widget reads from
and writes to SQLite, which is the single source of truth shared with the engine
thread — so a Streamlit rerun never restarts or disturbs trading.
"""

import io
import csv
import json
from datetime import datetime, timezone

import streamlit as st

import database as db
from utils import binance_client as bc
from utils import vps_optimizer, news
from execution import risk_guard, position_manager, orders
from notifications import telegram_bot as tg
from models import train as lgbm

# ---------------------------------------------------------------------------
# Page config + one-time startup
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Futures Bot", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

# Compact, tight mobile layout — smaller metrics, less vertical gap, slim sliders.
st.markdown(
    """
    <style>
      .block-container {padding-top: 0.6rem; padding-bottom: 2rem; max-width: 900px;}
      .stButton button {width: 100%;}
      /* tighten vertical rhythm */
      [data-testid="stVerticalBlock"] {gap: 0.35rem;}
      [data-testid="stHorizontalBlock"] {gap: 0.4rem;}
      [data-testid="column"] {min-width: 0 !important;}
      hr {margin: 0.45rem 0;}
      h1 {font-size: 1.5rem; margin: 0.2rem 0;}
      h2, h3 {margin-top: 0.5rem; margin-bottom: 0.2rem; font-size: 1.05rem;}
      /* compact metrics so the dashboard fits without endless scrolling */
      [data-testid="stMetricValue"] {font-size: 1.05rem; line-height: 1.1;}
      [data-testid="stMetricLabel"] {font-size: 0.72rem;}
      [data-testid="stMetricLabel"] p {font-size: 0.72rem;}
      [data-testid="stMetricDelta"] {font-size: 0.7rem;}
      /* slim sliders + toggles */
      [data-testid="stSlider"] {padding-top: 0; padding-bottom: 0;}
      [data-testid="stSlider"] label p {font-size: 0.8rem;}
      [data-testid="stWidgetLabel"] p {font-size: 0.82rem; margin-bottom: 0;}
      div[data-testid="stExpander"] {margin-bottom: 0.3rem;}
      .stCaption, [data-testid="stCaptionContainer"] p {font-size: 0.72rem;}
    </style>
    """,
    unsafe_allow_html=True,
)

db.init_db()

if "engine_started" not in st.session_state:
    st.session_state["engine_started"] = True
    vps_optimizer.start_vps_monitor()
    lgbm.ensure_model_on_start()
    tg.start_command_bot()
    import engine
    engine.start_engine()


# ---------------------------------------------------------------------------
# Auto-saving widget helpers
# ---------------------------------------------------------------------------
def _init(ss_key, value):
    if ss_key not in st.session_state:
        st.session_state[ss_key] = value


def bool_toggle(label, key, default=False):
    ss = f"w_{key}"
    _init(ss, db.get_bool(key, default))
    st.toggle(label, key=ss,
              on_change=lambda: db.save_setting(key, "1" if st.session_state[ss] else "0"))
    return st.session_state[ss]


def slider(label, key, lo, hi, step, default, is_int=False):
    ss = f"w_{key}"
    cur = db.get_int(key, int(default)) if is_int else db.get_float(key, float(default))
    _init(ss, cur)
    st.slider(label, lo, hi, key=ss, step=step,
              on_change=lambda: db.save_setting(key, st.session_state[ss]))
    return st.session_state[ss]


def number(label, key, default, is_int=False):
    ss = f"w_{key}"
    cur = db.get_int(key, int(default)) if is_int else db.get_float(key, float(default))
    _init(ss, cur)
    st.number_input(label, key=ss,
                    on_change=lambda: db.save_setting(key, st.session_state[ss]))
    return st.session_state[ss]


def text(label, key, password=False, default=""):
    ss = f"w_{key}"
    _init(ss, db.get_setting(key, default))
    st.text_input(label, key=ss, type="password" if password else "default",
                  on_change=lambda: db.save_setting(key, st.session_state[ss]))
    return st.session_state[ss]


def select(label, key, options, default):
    ss = f"w_{key}"
    cur = db.get_setting(key, default)
    _init(ss, cur if cur in options else default)
    st.selectbox(label, options, key=ss,
                 on_change=lambda: db.save_setting(key, st.session_state[ss]))
    return st.session_state[ss]


def tf_buttons(label, key, options, default):
    """Row of timeframe buttons; the selected one is highlighted."""
    st.caption(label)
    cur = db.get_setting(key, default)
    cols = st.columns(len(options))
    for i, opt in enumerate(options):
        if cols[i].button(("● " + opt) if opt == cur else opt, key=f"tf_{key}_{opt}"):
            db.save_setting(key, opt)
            st.rerun()
    return db.get_setting(key, default)


# ---------------------------------------------------------------------------
# Live data helpers
# ---------------------------------------------------------------------------
def _global_api_mode():
    return "real" if not db.get_bool("paper_trading_mode", True) else "test"


# The UI reads ONLY the engine's DB snapshot — it never calls Binance directly,
# so the dashboard stays instant regardless of network/API latency.
@st.cache_data(ttl=5)
def _snapshot_prices():
    try:
        return json.loads(db.get_setting("live_prices", "{}"))
    except Exception:  # noqa: BLE001
        return {}


@st.cache_data(ttl=5)
def _snapshot_market():
    try:
        return json.loads(db.get_setting("live_market", "{}"))
    except Exception:  # noqa: BLE001
        return {}


def live_equity(api_mode=None):
    """Return (equity, error_or_None) from the engine's DB snapshot."""
    eq = db.get_float("last_equity", 0.0)
    if eq > 0:
        return eq, None
    if not db.get_setting("snapshot_ts", ""):
        return 0.0, "Engine starting… snapshot pending (refresh in ~30s)"
    return 0.0, "Equity 0 — add Binance API keys in ⚙️ Settings"


def live_price(symbol, api_mode=None):
    return float(_snapshot_prices().get(symbol, 0.0) or 0.0)


def _conn_badge(mode):
    """Persistent 🟢/🔴 connection status badge for an API mode ('test'/'live')."""
    ok = db.get_setting(f"conn_{mode}_ok", "")
    msg = db.get_setting(f"conn_{mode}_msg", "")
    if ok == "1":
        st.success(f"🟢 Connected — {msg}")
    elif ok == "0":
        st.error(f"🔴 Not connected — {msg}")
    else:
        st.info("⚪ Not tested yet — enter keys and press Test/Save")


def binance_connected():
    """True if the engine's latest equity read succeeded."""
    return db.get_setting("binance_conn", "") == "1"


def _persist_api_fields():
    """Force-save the API key/secret fields from the widgets to the DB.

    On mobile the on_change save can miss the latest keystrokes if a button is
    tapped before the field loses focus, so we persist explicitly on click."""
    for k in ("binance_testnet_api", "binance_testnet_secret",
              "binance_live_api", "binance_live_secret"):
        v = st.session_state.get(f"w_{k}")
        if v is not None:
            db.save_setting(k, v)


# ===========================================================================
# TAB 1 — DASHBOARD
# ===========================================================================
def tab_dashboard():
    api_mode = _global_api_mode()
    paper_mode_on = db.get_bool("paper_trading_mode", True)
    starting = db.get_float("starting_balance", 0.0) or 5000.0
    # In paper mode health/equity track the simulated paper wallet; in real mode
    # they track the live wallet balance from the engine snapshot.
    if paper_mode_on:
        equity = db.paper_balance()
    else:
        equity, _err = live_equity(api_mode)
    health = risk_guard.health_ratio(equity, starting)
    color, zone = risk_guard.health_zone(health)
    pnl = risk_guard.today_pnl()
    pnl_pct = (pnl / equity * 100.0) if equity else 0.0

    # Section 1 — Account Health
    st.subheader("📊 Account Health")

    def _fmt_bal(v):
        if v in ("", None):
            return "— (no keys)"
        if v == "ERR":
            return "🔴 error"
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return "—"

    test_bal = db.get_setting("last_equity_test", "")
    live_bal = db.get_setting("last_equity_live", "")
    paper = db.get_bool("paper_trading_mode", True)

    b1, b2 = st.columns(2)
    if paper:
        # The simulated paper wallet that actually moves with wins/losses.
        paper_bal = db.paper_balance()
        realized = paper_bal - starting
        b1.metric("📒 Paper Balance", f"${paper_bal:,.2f}", f"{realized:+,.2f} all-time")
        b2.metric("Today PnL", f"${pnl:,.2f}", f"{pnl_pct:+.2f}%")
        st.caption(f"📒 Paper wallet = ${starting:,.0f} start + realized PnL. It rises/falls "
                   f"with each closed trade (testnet wallet stays {_fmt_bal(test_bal)}).")
    else:
        b1.metric("💰 Live Balance", _fmt_bal(live_bal))
        b2.metric("Today PnL", f"${pnl:,.2f}", f"{pnl_pct:+.2f}%")

    dd = max(0.0, 100.0 - health)
    emoji = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}[color]
    st.progress(min(1.0, max(0.0, health / 100.0)),
                text=f"{emoji} Health {health:.0f}% — {zone}  |  Drawdown {dd:.1f}%")
    if paper and not test_bal:
        st.caption("Add Binance Testnet API keys in ⚙️ Settings to fetch live prices.")

    # Section 2 — API Mode Status
    st.subheader("🔀 API Mode")
    paper = db.get_bool("paper_trading_mode", True)
    cols = st.columns([2, 1])
    cols[0].info(f"Current: {'🧪 PAPER / TEST' if paper else '💰 REAL API'}")
    if cols[1].button("Switch Mode"):
        st.session_state["confirm_real"] = not paper
    if st.session_state.get("confirm_real") and paper:
        st.warning("⚠️ Switch to REAL API? Real funds will be used.")
        if st.button("✅ Confirm REAL"):
            db.save_setting("paper_trading_mode", "0")
            st.session_state["confirm_real"] = False
            st.rerun()
    elif not paper and st.session_state.get("confirm_real") is False:
        pass
    if not paper and st.button("↩ Back to PAPER"):
        db.save_setting("paper_trading_mode", "1")
        st.rerun()

    # Section 3 — Pair Selection
    st.subheader("🎯 Pair Selection (Max 10)")
    all_pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
                 "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "MATICUSDT", "DOTUSDT",
                 "LTCUSDT", "TRXUSDT", "ATOMUSDT"]
    selected = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
    grid = st.columns(4)
    for i, p in enumerate(all_pairs):
        on = p in selected
        if grid[i % 4].button(("✅ " if on else "") + p, key=f"pair_{p}"):
            if on:
                selected.remove(p)
            elif len(selected) < 10:
                selected.append(p)
            else:
                st.toast("Max 10 pairs selected")
            db.save_setting("selected_pairs", ",".join(selected))
            st.rerun()
    st.caption(f"Selected {len(selected)}/10")

    # Section 4 — Engine Status
    st.subheader("⚙️ Engine Status")
    model = lgbm.get_model()
    sc = st.columns(2)
    sc[0].write(f"⚡ Scalping: {'🟢 Running' if db.get_bool('scalping_bot_on') else '🟡 Stopped'} "
                f"[{db.get_setting('scalping_api_mode','test').upper()}]")
    sc[0].write(f"📈 Swing: {'🟢 Running' if db.get_bool('swing_bot_on') else '🟡 Stopped'} "
                f"[{db.get_setting('swing_api_mode','test').upper()}]")
    sc[1].write(f"🤖 LightGBM: {'🟢 Active' if model else '🔴 Not Trained'}")
    sc[1].write(f"📰 Sentiment: {'🟢 Connected' if db.get_setting('hf_token') else '🔴 Error'}")

    # Section 5 — Live Positions
    st.subheader("📍 Live Positions")
    positions = db.get_open_positions()
    if positions:
        rows = []
        for p in positions:
            price = live_price(p["symbol"], _global_api_mode())
            amt, pct = position_manager.unrealized_pnl(p, price) if price else (0, 0)
            rows.append({
                "Pair": p["symbol"], "Mode": "Paper" if p["paper_mode"] else "Real",
                "Side": p["side"], "Entry": p["entry_price"], "Current": round(price, 4),
                "PnL$": round(amt, 2), "PnL%": round(pct, 2),
                "Engine": p["strategy"], "Trail": "✅" if p["trailing_active"] else "—",
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No open positions.")

    # Section 6 — Recent Signals
    st.subheader("🛰️ Recent Signals")
    sigs = db.get_recent_signals(10)
    if sigs:
        st.dataframe(
            [{"Time": s["timestamp"][11:], "Pair": s["pair"], "Strategies": s["strategies"],
              "AI": round(s["ai_score"] or 0, 2), "News": round(s["news_score"] or 0, 2),
              "Action": s["action"]} for s in sigs],
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("No signals yet.")

    # Section 7 — Performance Metrics
    st.subheader("📈 Performance Metrics")
    _performance_metrics()

    # Section 8 — Pair Performance Ranking
    st.subheader("🏆 Pair Ranking")
    _pair_ranking()

    # Section 9 — Best Trading Hours
    st.subheader("🕐 Best Trading Hours (UTC)")
    _best_hours()

    # Section 10 — Export
    st.subheader("📥 Export")
    csv_bytes = _trades_csv()
    st.download_button("📥 Download Trade Report CSV", csv_bytes,
                       file_name="trade_report.csv", mime="text/csv")


def _performance_metrics():
    trades = db.get_trades()
    total = len(trades)
    wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("net_pnl") or 0) <= 0]
    net = sum(float(t.get("net_pnl") or 0) for t in trades)
    gross_win = sum(float(t["net_pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["net_pnl"]) for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else 0.0
    wr = (len(wins) / total * 100) if total else 0.0
    best = max((float(t["net_pnl"]) for t in trades), default=0.0)
    worst = min((float(t["net_pnl"]) for t in trades), default=0.0)

    a, b, c = st.columns(3)
    a.metric("Total Trades", total)
    b.metric("Win Rate", f"{wr:.1f}%")
    c.metric("Profit Factor", f"{pf:.2f}")
    d, e, f = st.columns(3)
    d.metric("Wins / Losses", f"{len(wins)} / {len(losses)}")
    e.metric("Net PnL", f"${net:,.2f}")
    f.metric("Best / Worst", f"${best:,.2f} / ${worst:,.2f}")


def _pair_ranking():
    trades = db.get_trades()
    by_pair = {}
    for t in trades:
        p = t["pair"]
        d = by_pair.setdefault(p, {"n": 0, "w": 0, "net": 0.0})
        d["n"] += 1
        d["w"] += 1 if (t.get("net_pnl") or 0) > 0 else 0
        d["net"] += float(t.get("net_pnl") or 0)
    rows = [{"Pair": p, "Trades": d["n"], "WinRate": f"{d['w']/d['n']*100:.0f}%",
             "Net PnL": round(d["net"], 2)} for p, d in by_pair.items()]
    rows.sort(key=lambda r: r["Net PnL"], reverse=True)
    for i, r in enumerate(rows, 1):
        r_ = {"Rank": i, **r}
        rows[i - 1] = r_
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No closed trades yet.")


def _best_hours():
    trades = db.get_trades()
    hours = {}
    for t in trades:
        ts = t.get("exit_timestamp") or t.get("timestamp") or ""
        try:
            h = int(ts[11:13])
        except Exception:  # noqa: BLE001
            continue
        d = hours.setdefault(h, {"n": 0, "w": 0})
        d["n"] += 1
        d["w"] += 1 if (t.get("net_pnl") or 0) > 0 else 0
    if hours:
        rows = [{"Hour": f"{h:02d}:00", "Trades": d["n"], "WinRate": f"{d['w']/d['n']*100:.0f}%"}
                for h, d in sorted(hours.items())]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("Not enough data.")


def _trades_csv():
    trades = db.get_trades()
    buf = io.StringIO()
    if trades:
        writer = csv.DictWriter(buf, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    return buf.getvalue().encode()


# ===========================================================================
# TAB 2 / 3 — Shared engine UI
# ===========================================================================
def engine_tab(strategy, title, entry_opts, confirm_opts, trend_opts, swing=False):
    st.subheader(f"{title} — Mode & Control")

    # Section 1 — Mode & Control
    select("API Mode", f"{strategy}_api_mode", ["test", "real"],
           db.get_setting(f"{strategy}_api_mode", "test"))
    cols = st.columns(3)
    if cols[0].button("▶ START", key=f"{strategy}_start"):
        db.save_setting(f"{strategy}_bot_on", "1")
        db.save_setting("emergency_stop", "0")
        # Train the model lazily on first real start if AI hybrid is enabled.
        if db.get_bool(f"{strategy}_hybrid_on") and lgbm.get_model() is None and not lgbm.is_training():
            lgbm.train_in_background()
        st.rerun()
    if cols[1].button("⏸ PAUSE", key=f"{strategy}_pause"):
        db.save_setting(f"{strategy}_bot_on", "0")
        st.rerun()
    if cols[2].button("⏹ STOP ALL", key=f"{strategy}_stopall"):
        db.save_setting(f"{strategy}_bot_on", "0")
        _close_all_for(strategy)
        st.rerun()
    running = db.get_bool(f"{strategy}_bot_on")
    st.info(f"Status: {'🟢 Running' if running else '🟡 Stopped'}")
    if st.button("🧪 Place Test Trade (paper)", key=f"{strategy}_testtrade"):
        st.toast(_place_test_trade(strategy))
        st.rerun()
    st.caption("Test Trade opens a small PAPER position now so you can watch the "
               "full lifecycle (entry → TP/SL/trail → close). For verification only.")

    # Section 2 — Timeframe
    st.divider()
    st.subheader("⏱️ Timeframe")
    auto_tf = bool_toggle("🤖 Auto Timeframe (bot decides)", f"{strategy}_auto_tf", True)
    if auto_tf:
        preset = "Entry 5m · Confirm 15m · Trend 1h" if strategy == "scalping" \
                 else "Entry 4h · Confirm 1d · Trend 3d"
        st.caption(f"Bot auto → {preset}")
    else:
        tf_buttons("Entry TF", f"{strategy}_timeframe", entry_opts, db.get_setting(f"{strategy}_timeframe"))
        tf_buttons("Confirm TF", f"{strategy}_confirm_tf", confirm_opts, db.get_setting(f"{strategy}_confirm_tf"))
        tf_buttons("Trend TF", f"{strategy}_trend_tf", trend_opts, db.get_setting(f"{strategy}_trend_tf"))
    bool_toggle("MTF Filter", f"{strategy}_mtf_filter", True)

    # Section 3 — Strategy Mix
    st.divider()
    st.subheader("🧩 Strategy Mix")
    bool_toggle("Trend Following", f"{strategy}_trend_on", True)
    bool_toggle("Mean Reversion", f"{strategy}_reversion_on", True)
    bool_toggle("Breakout", f"{strategy}_breakout_on", True)
    st.markdown("---")
    hybrid_on = bool_toggle("🤖 AI Hybrid (LightGBM aggregates all enabled)", f"{strategy}_hybrid_on", True)
    if hybrid_on:
        slider("AI Threshold (lower = more trades)", f"{strategy}_ai_threshold", 0.50, 0.95, 0.01,
               db.get_float(f"{strategy}_ai_threshold", 0.75))
    st.markdown("---")
    news_on = bool_toggle("📰 News Filter", f"{strategy}_news_on", False)
    if news_on:
        slider("GNews Weight", f"{strategy}_gnews_weight", 0.1, 0.5, 0.05,
               db.get_float(f"{strategy}_gnews_weight", 0.3))
        slider("HF Min Score", f"{strategy}_hf_min_score", 0.50, 0.95, 0.01,
               db.get_float(f"{strategy}_hf_min_score", 0.60))
        st.caption(f"Cache: 30min (auto) | Today: {news.gnews_requests_today()}/100 requests")
        st.caption(f"Last sentiment: {db.get_setting('last_sentiment_result', '—')}")

    # Section 4 — Market Context
    st.divider()
    st.subheader("📡 Market Context")
    funding_on = bool_toggle("Use Funding/OI in Signal", f"{strategy}_funding_filter", False)
    if funding_on:
        slider("Funding/OI Weight", f"{strategy}_funding_weight", 0.10, 0.30, 0.05,
               db.get_float(f"{strategy}_funding_weight", 0.20))
    _market_context_display(strategy)

    # Section 5 — Risk Management
    st.divider()
    st.subheader("🛡️ Risk Management")
    auto = bool_toggle("Auto Risk Adjust", f"{strategy}_auto_risk", True)
    api_mode = db.get_setting(f"{strategy}_api_mode", "test")
    equity, _ = live_equity(api_mode if api_mode == "real" else "test")
    health = risk_guard.health_ratio(equity, db.get_float("starting_balance", equity or 1))
    mult = risk_guard.health_multiplier(health)
    if auto:
        # Auto → sliders hidden; bot sizes from balance + win-rate + health.
        rec_lev = risk_guard.recommended_leverage(equity)
        rec_risk = risk_guard.recommended_risk(strategy)
        eff_lev = max(1, int(round(rec_lev * mult)))
        eff_risk = rec_risk * mult
        st.caption(f"🤖 Auto from balance ${equity:,.0f} + win-rate history + "
                   f"health {health:.0f}% (×{mult:.2f}). No manual input needed.")
        st.write(f"Effective Leverage: **{eff_lev}x** | Effective Risk: **{eff_risk:.2f}%**")
    else:
        st.warning("⚠️ Manual Override Active — bot guidance below")
        base_lev = slider("Base Leverage (x)", f"{strategy}_base_leverage", 1, 20, 1,
                          db.get_int(f"{strategy}_base_leverage", 5), is_int=True)
        base_risk = slider("Base Risk %", f"{strategy}_base_risk_pct", 0.1, 10.0, 0.1,
                           db.get_float(f"{strategy}_base_risk_pct", 1.0))
        cap = db.get_float("lev_risk_hard_cap_pct", 10.0)
        rec_lev = risk_guard.recommended_leverage(equity)
        rec_risk = risk_guard.recommended_risk(strategy)
        wr, n = db.winrate(strategy)
        hist = f"win rate {wr:.0f}% / {n} trades" if (n and wr is not None) else "no history yet"
        st.info(f"💡 Recommended: **{rec_lev}x** leverage · **{rec_risk:.1f}%** risk "
                f"(balance ${equity:,.0f}, {hist})")
        if base_lev > rec_lev:
            st.warning(f"⚠️ {base_lev}x leverage is high for your balance — recommended ≤ {rec_lev}x")
        if base_risk > rec_risk * 1.5:
            st.warning(f"⚠️ {base_risk:.1f}% risk is high — recommended ~{rec_risk:.1f}%")
        if base_lev * base_risk > cap:
            st.error(f"❌ BLOCKED: Lev×Risk {base_lev*base_risk:.1f}% > hard cap {cap:.0f}%")
        else:
            st.success(f"✅ Lev×Risk {base_lev*base_risk:.1f}% ≤ hard cap {cap:.0f}%")

    # Section 6 — Take Profit / Stop Loss
    st.divider()
    st.subheader("🎯 Take Profit / Stop Loss")
    auto_tpsl = bool_toggle("🤖 Auto TP/SL (ATR-based)", f"{strategy}_auto_tpsl", True)
    if auto_tpsl:
        st.caption("🤖 Auto sets TP1/TP2/TP3 & SL from market volatility (ATR), "
                   "with partial closes at 50/30/20%.")
    else:
        rec_sl = 0.8 if strategy == "scalping" else 2.5
        rec_tp = rec_sl * 2
        tp_pct = slider("TP %", f"{strategy}_tp_pct", 0.2, 20.0, 0.1,
                        db.get_float(f"{strategy}_tp_pct", 1.5 if strategy == "scalping" else 4.0))
        sl_pct = slider("SL %", f"{strategy}_sl_pct", 0.1, 10.0, 0.1, db.get_float(f"{strategy}_sl_pct"))
        net_tp = tp_pct - 0.08
        net_sl = sl_pct + 0.08
        rr = (net_tp / net_sl) if net_sl else 0.0
        st.info(f"💡 Recommended: TP **{rec_tp:.1f}%** · SL **{rec_sl:.1f}%** (R:R 1:2)")
        if rr < 1.5:
            st.warning(f"⚠️ R:R 1:{rr:.2f} is low — set TP ≥ {sl_pct*1.5:.1f}% to reach 1:1.5")
        st.caption(f"After 0.08% fee → Net TP {net_tp:.2f}% / Net SL {net_sl:.2f}% | R:R 1:{rr:.2f}")
    bool_toggle("Auto Break-Even on TP1", f"{strategy}_auto_be", True)

    # Trailing stop — distance is auto (ATR) when Auto TP/SL is on, manual slider otherwise.
    trail_on = bool_toggle("📉 Trailing SL", f"{strategy}_trail_auto", False)
    if trail_on:
        if auto_tpsl:
            st.caption("🤖 Auto trailing distance (adapts to ATR / volatility).")
        else:
            slider("Trailing distance %", f"{strategy}_trail_pct", 0.2, 5.0, 0.1,
                   db.get_float(f"{strategy}_trail_pct", 1.5 if strategy == "swing" else 0.5))

    # Swing extras
    if swing:
        slider("Max Hold Days", "swing_max_hold_days", 1, 30, 1,
               db.get_int("swing_max_hold_days", 7), is_int=True)

    # Section 8 — Session Filter
    st.divider()
    st.subheader("🌍 Session Filter")
    bool_toggle("Session Filter", f"{strategy}_session_filter", False)
    bool_toggle("London 08:00-12:00 UTC", f"{strategy}_london_on", True)
    bool_toggle("New York 13:00-17:00 UTC", f"{strategy}_ny_on", True)
    bool_toggle("Asia 00:00-04:00 UTC", f"{strategy}_asia_on", True)
    bool_toggle("Weekend Trading Off", f"{strategy}_weekend_off", False)

    # Section 9 — Correlation Filter
    st.divider()
    st.subheader("🔗 Correlation Filter")
    corr_on = bool_toggle("Correlation Filter", f"{strategy}_corr_filter", True)
    if corr_on:
        slider("Max same-direction trades", f"{strategy}_max_corr_trades", 1, 5, 1,
               db.get_int(f"{strategy}_max_corr_trades", 2), is_int=True)

    # Section 10 — Live Trades
    st.divider()
    st.subheader("📋 Live Trades")
    _live_engine_trades(strategy, swing)

    # Section 11 — Today Stats
    st.divider()
    st.subheader("📅 Today Stats")
    _today_stats(strategy)


def _market_context_display(strategy):
    market = _snapshot_market()
    sym = market.get("symbol")
    if not sym:
        st.caption("Market context loads once the engine has run a cycle.")
        return
    fr = float(market.get("funding", 0.0))
    oi = float(market.get("oi", 0.0))
    c = st.columns(2)
    c[0].write(f"Funding ({sym}): {fr*100:.4f}% {'🟢 Bullish' if fr<=0 else '🔴 Bearish'}")
    c[1].write(f"OI change: {oi:+.2f}% {'↑' if oi>=0 else '↓'}")


def _live_engine_trades(strategy, swing):
    positions = db.get_open_positions(strategy=strategy)
    if not positions:
        st.caption("No open positions.")
        return
    for p in positions:
        price = live_price(p["symbol"], _global_api_mode())
        amt, pct = position_manager.unrealized_pnl(p, price) if price else (0, 0)
        with st.container(border=True):
            top = st.columns([3, 1])
            top[0].write(f"**{p['symbol']}** {p['side']} | Entry {p['entry_price']} | "
                         f"Cur {price:.4f} | PnL {amt:+.2f} ({pct:+.2f}%)")
            top[1].write(f"TP1 {'✅' if p['tp1_closed'] else '⏳'} "
                         f"TP2 {'✅' if p['tp2_closed'] else '⏳'}")
            if swing:
                hours, hold = position_manager._hold_duration(p)
                max_days = db.get_int("swing_max_hold_days", 7)
                st.caption(f"Held {hold} (max {max_days}d)"
                           + ("  ⚠️ approaching max" if hours >= max_days * 24 * 0.8 else ""))
                if p["trailing_active"]:
                    st.caption(f"Trail SL: {p.get('trail_sl_price') or '—'}")
            btns = st.columns(2)
            if btns[0].button("Close", key=f"close_{strategy}_{p['id']}"):
                _close_one(p)
                st.rerun()
            label = "Deactivate Trail" if p["trailing_active"] else "Activate Trail"
            if btns[1].button(label, key=f"trail_{strategy}_{p['id']}"):
                db.update_position(p["id"], {"trailing_active": 0 if p["trailing_active"] else 1})
                st.rerun()


def _today_stats(strategy):
    trades = [t for t in db.get_today_trades() if t["strategy"] == strategy]
    wins = [t for t in trades if (t.get("net_pnl") or 0) > 0]
    fees = sum(float(t.get("fees_paid") or 0) for t in trades)
    net = sum(float(t.get("net_pnl") or 0) for t in trades)
    wr = (len(wins) / len(trades) * 100) if trades else 0.0
    c = st.columns(3)
    c[0].metric("Trades", len(trades))
    c[1].metric("Win Rate", f"{wr:.0f}%")
    c[2].metric("Net PnL", f"${net:,.2f}")
    d = st.columns(3)
    d[0].metric("Wins", len(wins))
    d[1].metric("Losses", len(trades) - len(wins))
    d[2].metric("Fees", f"${fees:,.2f}")


def _close_one(pos):
    api_mode = "real" if db.get_setting(f"{pos['strategy']}_api_mode") == "real" else "test"
    price = live_price(pos["symbol"], api_mode) or pos["entry_price"]
    position_manager._close_full(pos, price, "Manual", api_mode, tg)


def _close_all_for(strategy):
    for p in db.get_open_positions(strategy=strategy):
        _close_one(p)


def _place_test_trade(strategy):
    """Open one small PAPER BUY position on the first selected pair (for testing
    the execution + monitoring pipeline, independent of signal generation)."""
    pairs = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
    if not pairs:
        return "Select a pair first (Dashboard)"
    sym = pairs[0]
    api_mode = db.get_setting(f"{strategy}_api_mode", "test")
    try:
        price = bc.get_price(sym, api_mode)
    except Exception as e:  # noqa: BLE001
        return f"Price error: {e}"
    d = price * 0.005  # ~0.5% as a pseudo-ATR for SL/TP spacing
    signal = {"signal": "BUY", "entry": price, "sl": price - d * 1.5,
              "tp1": price + d * 2, "tp2": price + d * 4, "tp3": price + d * 5, "atr": d}
    equity = db.get_float("last_equity_test", 0) or db.get_float("last_equity", 0) or 5000.0
    pid = orders.execute_order(
        sym, strategy, signal, equity=equity, multiplier=1.0, api_mode=api_mode,
        paper_mode=True, funding_rate=0.0, open_interest=0.0, session="manual",
        lgbm_score=0.0, news_score=0.0, health=100.0,
    )
    if pid:
        pos = db.get_position(pid)
        if pos:
            tg.notify_trade_open(pos)
        return f"✅ Test paper trade opened: {sym} BUY @ {price:.2f}"
    return "Blocked — check Lev×Risk hard cap / min qty"


# ===========================================================================
# TAB 4 — SETTINGS
# ===========================================================================
def tab_settings():
    st.subheader("⚙️ Settings")

    with st.expander("🔌 1. API Configuration", expanded=False):
        st.markdown("**🧪 Testnet**")
        text("Testnet API Key", "binance_testnet_api", password=True)
        text("Testnet Secret", "binance_testnet_secret", password=True)
        _conn_badge("test")
        if st.button("🔌 Test / Save Testnet"):
            _persist_api_fields()
            bc.reset_clients()
            ok, msg = bc.test_connection("test")
            db.save_setting("conn_test_ok", "1" if ok else "0")
            db.save_setting("conn_test_msg", msg)
            st.rerun()
        st.markdown("**💰 Live API**")
        text("Live API Key", "binance_live_api", password=True)
        text("Live Secret", "binance_live_secret", password=True)
        _conn_badge("live")
        if st.button("🔌 Test / Save Live"):
            _persist_api_fields()
            bc.reset_clients()
            ok, msg = bc.test_connection("real")
            db.save_setting("conn_live_ok", "1" if ok else "0")
            db.save_setting("conn_live_msg", msg)
            st.rerun()
        if st.button("💾 Save All API Keys"):
            _persist_api_fields()
            bc.reset_clients()
            for mode, ok_key, msg_key in (("test", "conn_test_ok", "conn_test_msg"),
                                          ("real", "conn_live_ok", "conn_live_msg")):
                if bc.has_credentials(mode):
                    ok, msg = bc.test_connection(mode)
                    db.save_setting(ok_key, "1" if ok else "0")
                    db.save_setting(msg_key, msg)
            tg.send_config_report()
            st.rerun()

    with st.expander("📰 2. News & AI APIs", expanded=False):
        text("GNews API Key", "gnews_api", password=True)
        st.caption(f"Today: {news.gnews_requests_today()}/100 requests | Cache 30min")
        text("HuggingFace Token", "hf_token", password=True)
        st.caption("Model: ProsusAI/finbert via API (cloud call, no local model)")
        st.caption(f"HF this month: {news.hf_requests_month()}/30000 | "
                   f"Status: {'🟢' if db.get_setting('hf_token') else '🔴'}")

    with st.expander("🤖 3. LightGBM Model", expanded=False):
        select("Retrain Schedule", "lgbm_retrain_schedule", ["daily", "weekly", "manual"],
               db.get_setting("lgbm_retrain_schedule", "weekly"))
        select("Training Period", "lgbm_train_period", ["1m", "3m", "6m", "1y"],
               db.get_setting("lgbm_train_period", "6m"))
        st.write(f"Last Trained: {db.get_setting('lgbm_last_trained', '—')}")
        st.write(f"Accuracy: {db.get_setting('lgbm_accuracy', '0')}")
        if lgbm.is_training():
            st.info("Training in progress…")
        if st.button("🔄 RETRAIN NOW"):
            lgbm.train_in_background()
            st.toast("Training started in background")
        st.caption("Features: RSI, EMA ratios, BB position/width, Stoch K, CCI, ADX, "
                   "MACD hist, Supertrend, ATR ratio, Volume ratio, Funding, OI%, "
                   "Sentiment, Candle/Wick ratios, Trend/Reversion/Breakout signals")
        st.caption("Labels: BUY=2 (>+1% in 3 candles), HOLD=1, SELL=0 (<-1%)")

    with st.expander("🚦 4. Global Risk Limits", expanded=False):
        slider("Daily Loss Limit %", "daily_loss_limit_pct", 1.0, 20.0, 0.5,
               db.get_float("daily_loss_limit_pct", 10.0))
        slider("Max Drawdown Pause %", "max_drawdown_pause_pct", 10.0, 50.0, 1.0,
               db.get_float("max_drawdown_pause_pct", 25.0))
        slider("Max Concurrent Trades", "max_concurrent_trades", 1, 10, 1,
               db.get_int("max_concurrent_trades", 5), is_int=True)
        slider("Lev×Risk Hard Cap %", "lev_risk_hard_cap_pct", 5.0, 20.0, 0.5,
               db.get_float("lev_risk_hard_cap_pct", 10.0))

    with st.expander("💵 5. Starting Balance Reference", expanded=False):
        st.write(f"Current reference: ${db.get_float('starting_balance', 0):,.2f}")
        st.caption("Health Ratio is based on this value.")
        new_bal = st.number_input("Update Reference Balance", value=db.get_float("starting_balance", 0.0))
        if st.button("📝 Update Reference Balance"):
            db.save_setting("starting_balance", f"{new_bal:.2f}")
            st.rerun()

    with st.expander("📱 6. Telegram", expanded=False):
        text("Bot Token", "telegram_token", password=True)
        text("Chat ID", "telegram_chat_id", password=True)
        bool_toggle("Notify Trade Open (entry)", "notify_trade_open", True)
        bool_toggle("Notify Trade Close", "notify_trade_close", True)
        bool_toggle("Notify Daily Report (00:00 UTC)", "notify_daily_report", True)
        bool_toggle("Notify Risk Alert", "notify_risk_alert", True)
        bool_toggle("Notify Engine Stop", "notify_engine_stop", True)
        st.markdown("---")
        bool_toggle("🔄 Periodic Status Update", "notify_status_on", True)
        slider("Status interval (hours)", "notify_status_interval_hr", 1.0, 24.0, 1.0,
               db.get_float("notify_status_interval_hr", 4.0))
        cols_tg = st.columns(2)
        if cols_tg[0].button("📤 Test Telegram"):
            if tg.test_telegram():
                st.success("Sent ✅")
            else:
                st.error("Failed — check token/chat id")
        if cols_tg[1].button("📊 Send Status Now"):
            if db.get_setting("telegram_token"):
                tg.notify_status()
                st.success("Status sent ✅")
            else:
                st.error("Set token first")

    with st.expander("🧹 7. VPS Optimizer", expanded=False):
        bool_toggle("Auto Clean", "vps_auto_clean_on", True)
        slider("RAM Threshold %", "vps_ram_threshold_pct", 50.0, 90.0, 1.0,
               db.get_float("vps_ram_threshold_pct", 80.0))
        st.write(f"Current RAM: {vps_optimizer.current_ram_pct():.1f}%")
        st.caption(f"Last clean: {db.get_setting('vps_last_clean', '—')}")
        if st.button("🧽 Clean Now"):
            ram = vps_optimizer.force_clean("manual")
            st.toast(f"Cleaned → RAM {ram:.1f}%")

    with st.expander("🌑 8. News Blackout Mode", expanded=False):
        bool_toggle("Blackout Mode", "blackout_on", False)
        slider("Volume Spike x", "blackout_volume_spike_x", 2.0, 10.0, 0.5,
               db.get_float("blackout_volume_spike_x", 5.0))
        slider("ATR Expansion x", "blackout_atr_expand_x", 1.5, 5.0, 0.5,
               db.get_float("blackout_atr_expand_x", 3.0))
        slider("Freeze Before (min)", "blackout_before_min", 5, 60, 5,
               db.get_int("blackout_before_min", 15), is_int=True)
        slider("Freeze After (min)", "blackout_after_min", 5, 60, 5,
               db.get_int("blackout_after_min", 15), is_int=True)
        select("Action", "blackout_action", ["no_entry", "close_all"],
               db.get_setting("blackout_action", "no_entry"))
        active = db.get_bool("blackout_active", False)
        if active:
            st.error("🔴 BLACKOUT ACTIVE")
        else:
            st.success("🟢 Clear")
        if active and st.button("Clear Blackout"):
            db.save_setting("blackout_active", "0")
            st.rerun()

    with st.expander("📈 9. Performance Optimizer", expanded=False):
        bool_toggle("Win Streak Bonus", "win_streak_bonus_on", False)
        slider("Win streak count", "streak_win_count", 2, 5, 1,
               db.get_int("streak_win_count", 3), is_int=True)
        slider("Bonus per win %", "streak_bonus_pct", 0.05, 0.5, 0.05,
               db.get_float("streak_bonus_pct", 0.1))
        slider("Loss streak count", "streak_loss_count", 2, 5, 1,
               db.get_int("streak_loss_count", 3), is_int=True)
        slider("Risk cut per loss %", "streak_cut_pct", 0.1, 1.0, 0.1,
               db.get_float("streak_cut_pct", 0.5))
        st.caption(f"Current streak adj: {db.get_float('streak_risk_adj', 0):+.2f}% | "
                   f"Win streak {db.get_int('win_streak')} | Loss streak {db.get_int('loss_streak')}")

    with st.expander("🔒 11. Login Password", expanded=False):
        st.caption("Set the dashboard login password. Leave empty to disable login.")
        text("Admin Password", "admin_password", password=True)
        if st.button("🔓 Log out"):
            st.session_state["authed"] = False
            st.rerun()

    with st.expander("🧰 10. System Actions", expanded=False):
        c = st.columns(2)
        if c[0].button("🗑️ Clear Paper Trade History"):
            db.clear_paper_trades()
            st.toast("Paper history cleared")
        c[1].download_button("📊 Export Trade Log CSV", _trades_csv(),
                             file_name="trade_log.csv", mime="text/csv")
        if c[0].button("🔄 Restart All Engines"):
            db.save_setting("emergency_stop", "0")
            db.save_setting("scalping_bot_on", "1")
            db.save_setting("swing_bot_on", "1")
            st.rerun()
        if c[1].button("⛔ EMERGENCY STOP ALL"):
            db.save_setting("emergency_stop", "1")
            db.save_setting("scalping_bot_on", "0")
            db.save_setting("swing_bot_on", "0")
            for sym in [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]:
                for mode in ("test", "real"):
                    try:
                        orders.cancel_all_orders(sym, mode)
                    except Exception:  # noqa: BLE001
                        pass
            tg.notify_engine_stop("Emergency stop from dashboard")
            st.rerun()

    st.divider()
    if st.button("💾 SAVE ALL SETTINGS", type="primary"):
        tg.notify_settings_saved()
        st.success("All settings saved ✅ (Telegram notified if configured)")


# ===========================================================================
# Main navigation
# ===========================================================================
def _check_login():
    """Password gate. Returns True if access is allowed (or login disabled)."""
    pw = db.get_setting("admin_password", "")
    if not pw:
        return True
    if st.session_state.get("authed"):
        return True
    st.title("🔒 Login")
    st.caption("Enter the admin password to access the dashboard.")
    entered = st.text_input("Password", type="password", key="login_pw")
    if st.button("Login"):
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("❌ Wrong password")
    return False


def main():
    if not _check_login():
        return
    st.title("📈 Binance Futures Bot")
    cols = st.columns([1, 3])
    if cols[0].button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()
    conn = "🟢 Binance Connected" if binance_connected() else "🔴 Binance Not Connected"
    cols[1].caption(f"UTC {datetime.now(timezone.utc).strftime('%H:%M:%S')} | "
                    f"{'🧪 PAPER' if db.get_bool('paper_trading_mode', True) else '💰 REAL'} | {conn}")

    t1, t2, t3, t4 = st.tabs(["📊 Dashboard", "⚡ Scalping", "📈 Swing", "⚙️ Settings"])
    with t1:
        tab_dashboard()
    with t2:
        engine_tab("scalping", "⚡ Scalping Engine",
                   ["1m", "3m", "5m", "15m"], ["5m", "15m", "30m"], ["15m", "1h", "4h"])
    with t3:
        engine_tab("swing", "📈 Swing Engine",
                   ["1h", "4h", "1d"], ["4h", "1d"], ["1d", "3d", "1w"], swing=True)
    with t4:
        tab_settings()


# Streamlit executes this script top-to-bottom on every rerun, so we call
# main() directly rather than guarding on __main__.
main()
