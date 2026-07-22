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
import os
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
      /* Move the tab bar to a fixed mobile-style bottom navigation. */
      [data-testid="stTabs"] [data-baseweb="tab-list"] {
          position: fixed; bottom: 0; left: 0; right: 0;
          background: #ffffff; border-top: 1px solid #ddd;
          z-index: 1000; justify-content: space-around;
          padding: 2px 0; box-shadow: 0 -2px 6px rgba(0,0,0,0.06);
      }
      [data-testid="stTabs"] [data-baseweb="tab"] {padding: 6px 4px;}
      .block-container {padding-bottom: 4.5rem !important;}
    </style>
    """,
    unsafe_allow_html=True,
)

db.init_db()

# Run the engine + background services inside the dashboard process ONLY when an
# external engine process is not handling them. Set ENGINE_EXTERNAL=1 in the
# dashboard's systemd unit when running run_engine.py as a separate service, so
# the UI stays responsive (the engine no longer competes with it for the GIL).
if os.environ.get("ENGINE_EXTERNAL") != "1" and "engine_started" not in st.session_state:
    st.session_state["engine_started"] = True
    vps_optimizer.start_vps_monitor()
    lgbm.ensure_model_on_start()
    tg.start_command_bot()
    import engine
    engine.start_engine()


# ---------------------------------------------------------------------------
# Auto-saving widget helpers
#
# Normally each widget saves immediately on change (one rerun per change). On a
# small VPS, where the dashboard shares a process with the trading engine, that
# makes the UI feel like it is constantly "running/connecting". Wrapping a group
# of widgets in `with settings_form(...)` batches them: nothing reruns until the
# Apply button is pressed, so you can adjust everything first, then apply once.
# ---------------------------------------------------------------------------
from contextlib import contextmanager

_FORM = None  # active settings_form field collector, or None for live-save mode


def _reg(key, kind):
    if _FORM is not None:
        _FORM.append((key, kind))


@contextmanager
def settings_form(name, label="💾 Apply changes"):
    """Group widgets so they only save + rerun once, on the Apply button."""
    global _FORM
    with st.form(name):
        _FORM = []
        try:
            yield
        finally:
            fields, _FORM = _FORM, None
        if st.form_submit_button(label):
            for key, kind in fields:
                v = st.session_state.get(f"w_{key}")
                db.save_setting(key, ("1" if v else "0") if kind == "bool" else v)
            st.success("✅ Saved")
            st.rerun()


def _init(ss_key, value):
    if ss_key not in st.session_state:
        st.session_state[ss_key] = value


def bool_toggle(label, key, default=False):
    ss = f"w_{key}"
    _init(ss, db.get_bool(key, default))
    if _FORM is None:
        st.toggle(label, key=ss,
                  on_change=lambda: db.save_setting(key, "1" if st.session_state[ss] else "0"))
    else:
        st.toggle(label, key=ss)
        _reg(key, "bool")
    return st.session_state[ss]


def slider(label, key, lo, hi, step, default, is_int=False):
    ss = f"w_{key}"
    cur = db.get_int(key, int(default)) if is_int else db.get_float(key, float(default))
    _init(ss, cur)
    if _FORM is None:
        st.slider(label, lo, hi, key=ss, step=step,
                  on_change=lambda: db.save_setting(key, st.session_state[ss]))
    else:
        st.slider(label, lo, hi, key=ss, step=step)
        _reg(key, "num")
    return st.session_state[ss]


def number(label, key, default, is_int=False):
    ss = f"w_{key}"
    cur = db.get_int(key, int(default)) if is_int else db.get_float(key, float(default))
    _init(ss, cur)
    if _FORM is None:
        st.number_input(label, key=ss,
                        on_change=lambda: db.save_setting(key, st.session_state[ss]))
    else:
        st.number_input(label, key=ss)
        _reg(key, "num")
    return st.session_state[ss]


def text(label, key, password=False, default="", show_saved=False):
    ss = f"w_{key}"
    _init(ss, db.get_setting(key, default))
    if _FORM is None:
        st.text_input(label, key=ss, type="password" if password else "default",
                      on_change=lambda: db.save_setting(key, st.session_state[ss]))
    else:
        st.text_input(label, key=ss, type="password" if password else "default")
        _reg(key, "text")
    if show_saved:
        cur = db.get_setting(key, "")
        if cur:
            st.caption(f"✅ Saved ({len(cur)} chars, hidden for security)")
        else:
            st.caption("— not set yet")
    return st.session_state[ss]


def select(label, key, options, default):
    ss = f"w_{key}"
    cur = db.get_setting(key, default)
    _init(ss, cur if cur in options else default)
    if _FORM is None:
        st.selectbox(label, options, key=ss,
                     on_change=lambda: db.save_setting(key, st.session_state[ss]))
    else:
        st.selectbox(label, options, key=ss)
        _reg(key, "text")
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
    creds_mode = "test" if mode == "test" else "real"
    if not bc.has_credentials(creds_mode):
        st.info("⚪ No keys entered. (Paper mode does NOT need them — only add keys "
                "to see your real wallet balance or to trade for real.)")
        return
    ok = db.get_setting(f"conn_{mode}_ok", "")
    msg = db.get_setting(f"conn_{mode}_msg", "")
    if ok == "1":
        st.success(f"🟢 Connected — {msg}")
    elif ok == "0":
        st.error(f"🔴 Not connected — {msg}")
    else:
        st.info("⚪ Keys present — press Test/Save to verify")


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
        if v:  # only save a non-empty field — never erase a saved key with a blank box
            db.save_setting(k, v)


# ===========================================================================
# TAB 1 — DASHBOARD
# ===========================================================================
def tab_dashboard():
    scalp_mode = db.get_setting("scalping_mode", "paper")
    swing_mode = db.get_setting("swing_mode", "paper")
    paper_bal = db.paper_balance()

    scalp_real = (scalp_mode == "real")
    swing_real = (swing_mode == "real")
    any_real = scalp_real or swing_real
    all_real = scalp_real and swing_real
    mixed = any_real and not all_real   # one engine real, one paper

    # Health guard always uses real equity when any real engine is active
    # (real money is at risk — that's what matters for the daily loss / drawdown guards).
    _live_str = db.get_setting("last_equity_live", "")
    try:
        real_bal = float(_live_str) if _live_str and _live_str != "ERR" else None
    except (TypeError, ValueError):
        real_bal = None

    primary_bal = real_bal if any_real and real_bal else paper_bal

    # Use separate starting balances for real vs paper so drawdown is correct.
    if any_real and real_bal:
        _real_start = db.get_float("real_starting_balance", 0.0)
        if not _real_start:
            # First time in real mode — auto-set to current real balance (0% drawdown).
            _real_start = real_bal
            db.save_setting("real_starting_balance", f"{real_bal:.2f}")
        starting = _real_start
    else:
        starting = db.get_float("starting_balance", 0.0) or 5000.0

    health = risk_guard.health_ratio(primary_bal, starting)
    color, zone = risk_guard.health_zone(health)
    pnl = risk_guard.today_pnl()
    pnl_pct = (pnl / primary_bal * 100.0) if primary_bal else 0.0

    def _fmt_bal(v):
        if v in ("", None):
            return "— (no key)"
        if v == "ERR":
            return "🔴 error"
        try:
            return f"${float(v):,.2f}"
        except (TypeError, ValueError):
            return "—"

    test_bal = db.get_setting("last_equity_test", "")
    live_bal = db.get_setting("last_equity_live", "")

    # Section 1 — Account Health
    st.subheader("📊 Account Health")

    if mixed:
        # Mixed mode: show real balance + paper balance side by side.
        # Health is based on real (since real money is what the guard protects).
        ma, mb, mc = st.columns(3)
        _rb = f"${real_bal:,.2f}" if real_bal else "— (no key)"
        _rb_delta = f"{real_bal - starting:+,.2f} vs start" if real_bal else ""
        ma.metric("💰 Real Balance", _rb, _rb_delta)
        mb.metric("📒 Paper Balance", f"${paper_bal:,.2f}", f"{paper_bal - starting:+,.2f} all-time")
        mc.metric("Today PnL", f"${pnl:,.2f}", f"{pnl_pct:+.2f}%")
        # Which engine runs which mode
        scalp_lbl = "⚡ Scalp=REAL" if scalp_real else "⚡ Scalp=Paper"
        swing_lbl = "📈 Swing=REAL" if swing_real else "📈 Swing=Paper"
        st.caption(f"Mixed mode: {scalp_lbl} | {swing_lbl} — Health based on Real balance")
    else:
        b1, b2 = st.columns(2)
        if any_real:
            b1.metric("💰 Real Balance", f"${primary_bal:,.2f}",
                      f"{primary_bal - starting:+,.2f} vs start")
        else:
            b1.metric("📒 Paper Balance", f"${paper_bal:,.2f}",
                      f"{paper_bal - starting:+,.2f} all-time")
        b2.metric("Today PnL", f"${pnl:,.2f}", f"{pnl_pct:+.2f}%")

    # Real wallet balances are shown ONLY when the matching API keys exist.
    wallet = []
    if bc.has_credentials("test"):
        wallet.append(("🧪 Test Balance", _fmt_bal(test_bal)))
    if bc.has_credentials("real"):
        wallet.append(("💰 Real Balance", _fmt_bal(live_bal)))
    if wallet:
        wc = st.columns(len(wallet))
        for i, (lab, val) in enumerate(wallet):
            wc[i].metric(lab, val)
    else:
        st.caption("ℹ️ Add Binance API keys (Settings) to see your Test/Real wallet balances. "
                   "Paper trading needs no keys.")

    dd = max(0.0, 100.0 - health)
    emoji = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}[color]
    mode_label = "Mixed→Real" if mixed else ("Real" if any_real else "Paper")
    st.progress(min(1.0, max(0.0, health / 100.0)),
                text=f"{emoji} Health {health:.0f}% ({mode_label}) — {zone}  |  Drawdown {dd:.1f}%")

    # Section 2 — Per-engine trade mode (independent Paper/Real)
    st.subheader("🔁 Trade Mode (per engine)")
    st.info(f"Current:  ⚡ Scalp = **{scalp_mode.upper()}**  |  📈 Swing = **{swing_mode.upper()}**")
    st.caption("Each engine runs its own mode — e.g. Swing on Paper while Scalp is Real. "
               "Change it in the ⚡ Scalping / 📈 Swing tab.")

    # Section 3 — Pair Selection (collapsed; click to expand)
    with st.expander("🎯 Pair Selection (breakout-1d broad universe)", expanded=False):
        _pair_selection()

    # Section 4 — Engine Status
    st.subheader("⚙️ Engine Status")
    # Only load the model (imports the heavy lightgbm lib) when the AI model is
    # enabled; it's OFF by config, so skip it to keep the dashboard light.
    ai_on = db.get_bool("ai_model_on", False)
    model = lgbm.get_model() if ai_on else None
    sc = st.columns(2)
    sc[0].write(f"⚡ Scalping: {'🟢 Running' if db.get_bool('scalping_bot_on') else '🟡 Stopped'} "
                f"[{db.get_setting('scalping_mode','paper').upper()}]")
    sc[0].write(f"📈 Swing: {'🟢 Running' if db.get_bool('swing_bot_on') else '🟡 Stopped'} "
                f"[{db.get_setting('swing_mode','paper').upper()}]")
    sc[1].write(f"🤖 LightGBM: {'🟢 Active' if model else ('🔴 Not Trained' if ai_on else '⚪ Off')}")
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

    # Section 7 — Performance Metrics
    st.subheader("📈 Performance Metrics")
    _trade_filter = st.radio("Show trades:", ["Paper only", "Real only", "All"],
                             horizontal=True, index=0, key="rank_filter")
    _pm_filter = (False if _trade_filter == "Real only"
                  else True if _trade_filter == "Paper only" else None)
    _performance_metrics(_pm_filter)

    # Sections 8-11 — collapsed by default; click to expand each.
    with st.expander("🏆 Pair Ranking", expanded=False):
        _pair_ranking(_pm_filter)

    with st.expander("🕐 Best Trading Hours (UTC)", expanded=False):
        _best_hours(_pm_filter)

    with st.expander("📅 Trade History (by date)", expanded=False):
        _history_calendar()

    with st.expander("📥 Export", expanded=False):
        csv_bytes = _trades_csv(_pm_filter)
        st.download_button("📥 Download Trade Report CSV", csv_bytes,
                           file_name="trade_report.csv", mime="text/csv")


def _pair_selection():
    """Checkbox grid + Save for the traded pair universe (shown in an expander)."""
    all_pairs = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                 "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT",
                 "DOTUSDT", "TRXUSDT", "ATOMUSDT", "UNIUSDT", "ETCUSDT",
                 "XLMUSDT", "BCHUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
                 "OPUSDT", "FILUSDT", "INJUSDT", "SUIUSDT", "AAVEUSDT",
                 "ALGOUSDT", "ICPUSDT", "VETUSDT", "HBARUSDT", "GRTUSDT",
                 "SANDUSDT", "MANAUSDT", "AXSUSDT", "EOSUSDT", "THETAUSDT",
                 "XTZUSDT", "CRVUSDT", "DYDXUSDT"]
    selected = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
    grid = st.columns(2)
    checked = {}
    for i, p in enumerate(all_pairs):
        checked[p] = grid[i % 2].checkbox(p, value=(p in selected), key=f"pairchk_{p}")
    new_sel = [p for p in all_pairs if checked[p]]
    st.caption(f"Ticked {len(new_sel)}/38")
    if st.button("💾 Save Pairs", type="primary"):
        if len(new_sel) > 40:
            st.error("⚠️ Max 40 pairs — untick some.")
        elif not new_sel:
            st.error("Pick at least 1 pair.")
        else:
            db.save_setting("selected_pairs", ",".join(new_sel))
            tg.send_message("🎯 <b>Pairs updated</b>\n" + ", ".join(new_sel))
            st.success(f"Saved {len(new_sel)} pairs ✅ (Telegram notified)")
            st.rerun()


def _history_calendar():
    """Calendar-style history: a per-day P&L rollup plus a date picker that shows
    that day's closed trades. Today's numbers live in the per-engine 'Today Stats'
    which auto-resets at 00:00 UTC; this is the archive of past days."""
    daily = db.get_daily_pnl(30)
    if not daily:
        st.caption("No closed trades yet — history fills in as trades close.")
        return
    # per-day rollup table (newest first)
    rollup = [{
        "Date": d["d"],
        "Trades": d["n"],
        "Win%": f"{(100*d['wins']//d['n']) if d['n'] else 0}%",
        "Net PnL": f"${d['net']:+.2f}",
    } for d in daily]
    st.caption("Daily P&L (last 30 trading days) — each day is a fresh tally.")
    st.dataframe(rollup, use_container_width=True, hide_index=True)

    # date picker -> that day's trades
    dates = [d["d"] for d in daily]
    pick = st.selectbox("📆 View a specific day's trades", dates, index=0, key="hist_date")
    day_trades = db.get_trades_on_date(pick)
    if not day_trades:
        st.caption("No trades on that day.")
        return
    net = sum(float(t.get("net_pnl") or 0) for t in day_trades)
    wins = sum(1 for t in day_trades if float(t.get("net_pnl") or 0) > 0)
    st.write(f"**{pick}** — {len(day_trades)} trades · {100*wins//len(day_trades)}% win "
             f"· Net **${net:+.2f}**")
    rows = [{
        "Pair": t.get("symbol"),
        "Engine": t.get("strategy"),
        "Side": t.get("side"),
        "Net": f"${float(t.get('net_pnl') or 0):+.2f}",
        "Reason": t.get("close_reason"),
        "Closed": (t.get("exit_timestamp") or "")[11:19],
    } for t in day_trades]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _learning_progress():
    """Show how much trade data the bot has gathered toward its adaptive
    win-rate learning (10 trades = threshold adapts, 15 = risk + pair filter)."""
    for strat in ("scalping", "swing"):
        wr, n = db.winrate(strat)
        wr_txt = f"{wr:.0f}% win" if (n and wr is not None) else "—"
        if n >= 15:
            stage = "✅ Fully adaptive (risk + pair filter active)"
        elif n >= 10:
            stage = "🟡 Threshold adapting (need 15 for risk/pair learning)"
        else:
            stage = "🔵 Collecting data (need 10 to start adapting)"
        st.write(f"{'⚡' if strat=='scalping' else '📈'} **{strat.title()}**: "
                 f"{n} trades · {wr_txt} — {stage}")
        st.progress(min(1.0, n / 15.0))
    # Self-learning win predictor (learns which entry conditions actually win).
    n_learn = db.learning_count()
    wm = lgbm.get_win_model()
    if wm is not None:
        st.success(f"🎯 Win Predictor ACTIVE — trained on {db.get_setting('win_model_samples','?')} "
                   f"entries (train acc {db.get_setting('win_model_acc','?')}). The bot now skips "
                   f"setups it predicts will lose.")
    else:
        st.info(f"🎯 Win Predictor: collecting entries ({n_learn}/30). Once 30 closed trades exist, "
                f"the bot trains a model on your wins/losses and only takes high-win-probability "
                f"setups — it then re-trains every 10 trades.")
    st.caption("More trades = smarter bot. Let it run on paper for ~2–4 weeks (≥50–100 "
               "trades) before judging; switch to REAL only after consistent positive PnL.")


def _performance_metrics(paper_mode=None):
    trades = db.get_trades(paper_mode=paper_mode)
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


def _pair_ranking(paper_mode=None):
    trades = db.get_trades(paper_mode=paper_mode)
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


def _best_hours(paper_mode=None):
    trades = db.get_trades(paper_mode=paper_mode)
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


def _trades_csv(paper_mode=None):
    trades = db.get_trades(paper_mode=paper_mode)
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

    # Section 1 — Mode & Control (per-engine paper/real — independent of the other)
    mode = select("🔁 Mode", f"{strategy}_mode", ["paper", "real"],
                  db.get_setting(f"{strategy}_mode", "paper"))
    if mode == "real":
        if not bc.has_credentials("real"):
            st.error("⚠️ REAL mode needs Live API keys (Settings → API). Trades will be blocked.")
        else:
            st.warning("💰 REAL mode — this engine trades with actual funds.")
    else:
        st.caption("🧪 PAPER — simulated on testnet data (no real money).")
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
    mtf_on = db.get_bool(f"{strategy}_mtf_filter", True)
    if auto_tf:
        preset = "Entry 5m · Confirm 15m · Trend 1h" if strategy == "scalping" \
                 else "Entry 1d · Confirm 3d · Trend 1w"
        st.caption(f"Bot auto → {preset}")
    else:
        tf_buttons("Entry TF", f"{strategy}_timeframe", entry_opts, db.get_setting(f"{strategy}_timeframe"))
        # Confirm/Trend TF only matter when the MTF filter is on — hide otherwise.
        if mtf_on:
            tf_buttons("Confirm TF", f"{strategy}_confirm_tf", confirm_opts, db.get_setting(f"{strategy}_confirm_tf"))
            tf_buttons("Trend TF", f"{strategy}_trend_tf", trend_opts, db.get_setting(f"{strategy}_trend_tf"))
    bool_toggle("MTF Filter", f"{strategy}_mtf_filter", True)

    # Section 3 — Strategy (each engine runs its ONE proven strategy; the losing
    # strategies + AI Hybrid are removed to keep the tab focused).
    st.divider()
    st.subheader("🧩 Strategy")
    if strategy == "scalping":
        rev_on = bool_toggle("🔄 Mean Reversion", "scalping_reversion_on", True)
        if rev_on:
            st.markdown("**🔄 Reversion tuning** (backtest: RSI<20 + R:R 1:3 = best)")
            ext = slider("RSI extreme gate — 0 = off · lower = stricter/fewer/better",
                         "reversion_rsi_extreme", 0, 40, 1,
                         db.get_int("reversion_rsi_extreme", 0), is_int=True)
            if ext > 0:
                st.caption(f"🎯 Only fires when RSI ≤ {ext} (buy) / ≥ {100-ext} (sell). "
                           f"20 = edge (PF~1.3, fewer) · 25 = more trades (PF~1.1) · 0 = all.")
            slider("Reversion R:R (TP = R × SL) — 0 = native TP",
                   "reversion_fixed_rr", 0.0, 4.0, 0.5,
                   db.get_float("reversion_fixed_rr", 0.0))
    else:  # swing = breakout on the daily chart (the validated edge)
        bool_toggle("🚀 Breakout", "swing_breakout_on", True)
        st.caption("Swing trades 1d breakout only — the walk-forward + OOS validated edge.")
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

    # Swing extras — Max Hold Days (force-close a swing trade after N days)
    if swing:
        auto_mh = bool_toggle("🤖 Auto Max Hold Days", "swing_auto_maxhold", True)
        if auto_mh:
            st.caption("🤖 Auto → force-close swing trades after **7 days** (so a trade never "
                       "stays open forever).")
        else:
            slider("Max Hold Days", "swing_max_hold_days", 1, 30, 1,
                   db.get_int("swing_max_hold_days", 7), is_int=True)

    # Section 10 — Live Trades
    st.divider()
    st.subheader("📋 Live Trades")
    _live_engine_trades(strategy, swing)

    # Section 11 — Today Stats
    st.divider()
    st.subheader("📅 Today Stats")
    _today_stats(strategy)

    # Save & notify — settings already auto-save on change; this confirms + pings Telegram.
    st.divider()
    if st.button(f"💾 Save {strategy.title()} Settings", key=f"{strategy}_savenotify", type="primary"):
        tg.send_message(f"⚙️ <b>{strategy.title()} settings saved</b>\n" + tg.build_status_text())
        st.success("Saved ✅ (settings apply live to the running bot; Telegram notified)")


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
    st.caption("↻ Resets automatically at 00:00 UTC (~6:30 AM MMT) each day. "
               "Past days are archived in Dashboard → 📅 Trade History.")


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

    # --- Swing Plan selector (swing slot only; scalping runs on its own) -----
    import swing_plans
    st.markdown("### 📈 Swing Plan  (Scalping runs separately — see its tab)")
    active = swing_plans.active_swing_plan()
    if active:
        st.success(f"🔒 **Swing = Plan {active} ({swing_plans.PLAN_NAMES[active]}).** "
                   f"{swing_plans.PLAN_DESC[active]}. Walk-forward robust.")
    else:
        st.warning("✏️ **Swing = custom** — not matching any validated plan. "
                   "Pick one below to snap to a validated swing config.")
    _plan_labels = {n: f"Plan {n} — {swing_plans.PLAN_NAMES[n]}: {swing_plans.PLAN_DESC[n]}"
                    for n in (1, 2, 3)}
    _pick = st.radio(
        "Pick your SWING strategy (this only sets the swing engine):",
        [1, 2, 3], format_func=lambda n: _plan_labels[n],
        index=((active - 1) if active in (1, 2, 3) else 0),
        key="swing_plan_pick")
    if st.button(f"✅ Apply Swing Plan {_pick}", type="primary", key="apply_swing_plan"):
        swing_plans.apply_swing_plan(_pick)
        st.success(f"✅ Swing set to Plan {_pick} ({swing_plans.PLAN_NAMES[_pick]}). "
                   "Takes effect on the next engine scan — no restart needed.")
        st.rerun()
    st.caption("All three swing plans are walk-forward robust (edge held across 3 "
               "eras, newest PF≥1.2). This selector changes ONLY the swing engine. "
               "The Scalping engine (reversion RSI<20, 1:3) runs independently on "
               "its own slot and is unaffected — tune it on the Scalping tab.")
    st.divider()

    with st.expander("🔌 1. API Configuration", expanded=False):
        st.markdown("**🧪 Testnet**")
        text("Testnet API Key", "binance_testnet_api", password=True, show_saved=True)
        text("Testnet Secret", "binance_testnet_secret", password=True, show_saved=True)
        _conn_badge("test")
        if st.button("🔌 Test / Save Testnet"):
            _persist_api_fields()
            bc.reset_clients()
            ok, msg = bc.test_connection("test")
            db.save_setting("conn_test_ok", "1" if ok else "0")
            db.save_setting("conn_test_msg", msg)
            st.rerun()
        st.markdown("**💰 Live API**")
        text("Live API Key", "binance_live_api", password=True, show_saved=True)
        text("Live Secret", "binance_live_secret", password=True, show_saved=True)
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
        text("GNews API Key", "gnews_api", password=True, show_saved=True)
        st.caption(f"Today: {news.gnews_requests_today()}/100 requests | Cache 30min")
        text("HuggingFace Token", "hf_token", password=True, show_saved=True)
        st.caption("Model: ProsusAI/finbert via API (cloud call, no local model)")
        hf_ok = bool(db.get_setting("hf_token"))
        st.caption(f"HF this month: {news.hf_requests_month()}/30000 | "
                   f"Status: {'🟢 connected' if hf_ok else '🔴 no token'}")
        if st.button("💾 Save News/AI Keys"):
            tg.send_message("📰 <b>News/AI keys saved</b> "
                            f"(GNews {'✅' if db.get_setting('gnews_api') else '—'}, "
                            f"HF {'✅' if hf_ok else '—'})")
            st.success("Saved ✅ (Telegram notified)")

        st.markdown("---")
        st.markdown("**🤖 Claude AI Monitor** (4-hour auto analysis)")
        _claude_key_saved = db.get_setting("claude_api_key", "")
        _claude_status = "🟢 Connected" if _claude_key_saved.startswith("sk-ant-") else "🔴 Not set"
        st.caption(f"Status: {_claude_status} | Auto-report every 4h via cron")
        _claude_key_input = st.text_input(
            "Claude API Key (sk-ant-...)",
            value="",
            type="password",
            placeholder="sk-ant-api03-...",
            key="claude_api_key_input",
        )
        if st.button("💾 Save Claude API Key"):
            if _claude_key_input.startswith("sk-ant-"):
                db.save_setting("claude_api_key", _claude_key_input)
                st.success("Claude API key saved ✅ — AI monitor will use Claude Haiku")
            elif _claude_key_input == "":
                st.warning("Key မထည့်ဘဲ မသိမ်းပါဘူး")
            else:
                st.error("Key format မမှန်ဘူး — sk-ant- နဲ့ စရမယ်")
        if _claude_key_saved.startswith("sk-ant-"):
            if st.button("🗑 Remove Claude Key (use rule-based)"):
                db.save_setting("claude_api_key", "")
                st.success("Removed — monitor will fall back to rule-based analysis")
                st.rerun()

    with st.expander("🚦 4. Global Risk Limits", expanded=False):
        g_auto = db.get_bool("auto_pilot", False) or bool_toggle(
            "🤖 Auto Global Risk (bot-managed limits)", "global_auto_risk", False)
        if g_auto:
            st.caption("🤖 Auto → Max Concurrent scales with balance "
                       "(<$100→2, <$1k→4, <$10k→6, else 8); Min R:R adapts to "
                       "win-rate (losing→2.5, even→2.0, winning→1.8); Min TP "
                       "floor 0.4%. Other limits below stay as safe guard rails.")
        st.caption("Adjust everything, then press **Apply** once (avoids the "
                   "dashboard reloading on every change).")
        with settings_form("global_risk_form"):
            slider("Daily Loss Limit %", "daily_loss_limit_pct", 1.0, 20.0, 0.5,
                   db.get_float("daily_loss_limit_pct", 10.0))
            slider("Max Drawdown Pause %", "max_drawdown_pause_pct", 10.0, 50.0, 1.0,
                   db.get_float("max_drawdown_pause_pct", 25.0))
            if not g_auto:
                slider("Max Concurrent Trades", "max_concurrent_trades", 1, 40, 1,
                       db.get_int("max_concurrent_trades", 5), is_int=True)
            slider("Lev×Risk Hard Cap %", "lev_risk_hard_cap_pct", 5.0, 20.0, 0.5,
                   db.get_float("lev_risk_hard_cap_pct", 10.0))
            if not g_auto:
                # min 0.2 so the breakout-1d config (tight SL, R:R sometimes < 1)
                # is representable — a 1.0 floor here silently blocks breakout entries.
                slider("Min Risk:Reward Ratio", "min_rr_ratio", 0.2, 3.0, 0.1,
                       db.get_float("min_rr_ratio", 0.3))
                st.caption("Skip entries where TP1 distance < this × SL distance. "
                           "Keep LOW (0.3) — breakout's SL sits at the broken level "
                           "(tight), so a high floor blocks every breakout entry.")
                slider("Min TP Distance % (fee floor)", "min_tp_pct", 0.0, 2.0, 0.05,
                       db.get_float("min_tp_pct", 0.4))
                st.caption("Skip entries whose TP1 target is closer than this %. "
                           "Round-trip fee+slippage ≈ 0.13%, so a 0.4% floor keeps "
                           "wins well above costs and filters fee-eaten micro-scalps.")
            bool_toggle("ATR-based Stop Loss (Auto mode)", "atr_sl_enabled", True)
            slider("ATR SL Multiplier", "atr_sl_mult", 0.5, 4.0, 0.1,
                   db.get_float("atr_sl_mult", 1.5))
            st.caption("In Auto mode, set SL = entry ∓ (this × ATR) instead of the "
                       "strategy's structural SL. Scales risk to volatility — avoids "
                       "the far EMA50 stops that caused oversized losses. Lower = "
                       "tighter, higher = wider.")
            slider("Paper Slippage % (realism)", "paper_slippage_pct", 0.0, 0.30, 0.01,
                   db.get_float("paper_slippage_pct", 0.05))
            st.caption("Paper trades simulate real entry + stop-market slippage so paper "
                       "results mirror REAL conditions (fees already included). 0.05% ≈ "
                       "typical liquid-pair slippage. Set 0 for idealised fills.")

    with st.expander("💵 5. Starting Balance / Reset Paper Account", expanded=False):
        st.write(f"Current paper start capital: **${db.get_float('starting_balance', 0):,.2f}**")
        st.caption("Paper Balance & Health are based on this. Set it to the amount "
                   "you plan to start REAL with (e.g. $50–$100) so paper mirrors reality.")
        new_bal = st.number_input("Starting capital ($)", min_value=10.0, max_value=1000000.0,
                                  value=float(db.get_float("starting_balance", 100.0) or 100.0), step=10.0)
        cset = st.columns(2)
        if cset[0].button("📝 Set Capital"):
            db.save_setting("starting_balance", f"{new_bal:.2f}")
            st.rerun()
        if cset[1].button("🔄 Reset Paper Account"):
            db.clear_paper_trades()
            db.save_setting("starting_balance", f"{new_bal:.2f}")
            st.success(f"Paper account reset to ${new_bal:,.0f}, history cleared.")
            st.rerun()

        st.divider()
        _cur_real_start = db.get_float("real_starting_balance", 0.0)
        st.write(f"💰 Real account start capital: **${_cur_real_start:,.2f}**" if _cur_real_start else
                 "💰 Real account start capital: **not set**")
        st.caption("Used to calculate drawdown and Health % when running in REAL mode. "
                   "Set this once to your actual Binance balance when you first go live.")
        new_real_bal = st.number_input("Real starting capital ($)", min_value=1.0, max_value=1000000.0,
                                       value=float(_cur_real_start or 63.99), step=1.0,
                                       key="real_start_input")
        if st.button("💰 Set Real Starting Capital"):
            db.save_setting("real_starting_balance", f"{new_real_bal:.2f}")
            st.success(f"Real starting balance set to ${new_real_bal:,.2f}. Health % reset.")
            st.rerun()

    with st.expander("📱 6. Telegram", expanded=False):
        text("Bot Token", "telegram_token", password=True, show_saved=True)
        text("Chat ID", "telegram_chat_id", password=True, show_saved=True)
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

    with st.expander("💾 12. Backup & Restore (move to a new VPS/phone)", expanded=False):
        st.caption("Everything the bot 'knows' lives in trading_bot.db (settings, API keys, "
                   "all trade history / win-rate memory) and lgbm_model.pkl (the trained AI). "
                   "Back these up to carry your account + learning to a new device.")
        # Generate the backup only on demand (not on every render — that held the
        # DB lock each time the Settings tab loaded).
        if st.button("🗜️ Prepare DB Backup"):
            try:
                st.session_state["_dbbackup"] = db.export_backup_bytes()
            except Exception as e:  # noqa: BLE001
                st.error(f"Backup failed: {e}")
        if st.session_state.get("_dbbackup"):
            st.download_button("📥 Download DB Backup", st.session_state["_dbbackup"],
                               file_name="trading_bot.db", mime="application/octet-stream")
        import os as _os
        if _os.path.exists(lgbm.MODEL_PATH):
            with open(lgbm.MODEL_PATH, "rb") as _mf:
                st.download_button("🤖 Download AI Model", _mf.read(),
                                   file_name="lgbm_model.pkl", mime="application/octet-stream")

        st.markdown("**Restore** — upload a previously downloaded `trading_bot.db`:")
        up = st.file_uploader("Restore DB backup", type=["db"], key="restore_db")
        if up is not None and st.button("⚠️ Restore & Overwrite"):
            db.restore_backup_bytes(up.read())
            st.success("Restored. Now restart the service: `systemctl restart futures-bot`")

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
    _sc_mode = db.get_setting("scalping_mode", "paper")
    _sw_mode = db.get_setting("swing_mode", "paper")
    if _sc_mode == "real" and _sw_mode == "real":
        _mode_badge = "💰 REAL"
    elif _sc_mode == "real" or _sw_mode == "real":
        _mode_badge = "⚡💰 MIXED"
    else:
        _mode_badge = "🧪 PAPER"
    cols[1].caption(f"UTC {datetime.now(timezone.utc).strftime('%H:%M:%S')} | "
                    f"{_mode_badge} | {conn}")

    t1, t2, t3, t4 = st.tabs(["📊 Dashboard", "⚡ Scalping", "📈 Swing", "⚙️ Settings"])
    with t1:
        tab_dashboard()
    with t2:
        engine_tab("scalping", "⚡ Scalping Engine",
                   ["1m", "3m", "5m", "15m", "30m", "1h"], ["5m", "15m", "30m", "1h"],
                   ["15m", "1h", "4h"])
    with t3:
        engine_tab("swing", "📈 Swing Engine",
                   ["1h", "4h", "1d"], ["4h", "1d"], ["1d", "3d", "1w"], swing=True)
    with t4:
        tab_settings()


# Streamlit executes this script top-to-bottom on every rerun, so we call
# main() directly rather than guarding on __main__.
main()
