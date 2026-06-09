# 🤝 HANDOFF — continue this project in any new session

This bot's entire state lives in **git**, not in any chat. To continue in a new
Claude Code session (web/desktop) or project, point it at this repo + branch and
have it read this file.

- **Repo:** `hpuaung/hpuaung`
- **Branch:** `claude/code-writing-help-pzEP9`
- **App folder:** `trading_bot/`  ·  **Phone app:** `flutter_app/`
- **Live VPS:** `150.95.84.241`, dashboard at `http://150.95.84.241:8080`
  (Ubuntu, systemd service `futures-bot` runs `streamlit run app.py`).
- **Update the VPS after a push:**
  ```
  ssh root@150.95.84.241 'cd /root/hpuaung && git pull -q origin claude/code-writing-help-pzEP9 && systemctl restart futures-bot && systemctl is-active futures-bot'
  ```

## What it is
Binance **Futures** trading bot: Streamlit mobile dashboard + SQLite, Scalping +
Swing engines, 4 strategies (trend/reversion/breakout/AI-hybrid), LightGBM, news
sentiment (GNews + HuggingFace FinBERT), full risk guard, paper + real trading,
self-learning win predictor. Optimised for a 1GB VPS.

## Architecture (trading_bot/)
- `app.py` — Streamlit UI (bottom tabs: Dashboard / Scalping / Swing / Settings).
- `engine.py` — trading loop, `aggregate_signal`, per-engine paper/real mode.
- `database.py` — SQLite (settings, trades, active_positions, signals, events,
  counters, **learning** = win-predictor memory). The message bus.
- `strategies/` trend, reversion, breakout, ai_hybrid.
- `models/train.py` — LightGBM price model **and** the self-learning win model.
- `execution/` orders, position_manager, risk_guard.
- `notifications/telegram_bot.py`; `utils/` binance_client, indicators, news, …
- `bot.py` — auto-generated single-file build (`python build_single_file.py`).

## Done (high level)
Pure-pandas indicators (no pandas_ta); 300-candle fetch; relaxed strategies +
strong-trend entry; closed-candle signals; AI-hybrid runs base strategies even
when toggled off + soft model nudge; adaptive win-rate filter; **closed-loop win
predictor** (entry features → win/loss, retrains every 10 trades); auto/manual on
timeframe, TP/SL, trailing, risk, AI threshold, max-hold; per-engine Paper/Real;
paper wallet that moves with PnL; bottom-tab UI; admin login; Backup & Restore
(DB + model); Flutter WebView app; settings-persistence migration fix; API
"Saved" indicators; balance shown only when keys exist.

## Pending / next ideas
- Real-mode trading needs the user's live key to have Futures permission + enough
  margin (live wallet was ~$63.99). Verify before trusting real mode.
- 🟢/🔴 dot indicators on every connection test (badges exist already).
- Native push notifications in the Flutter app (currently Telegram).
- Optional: custom HTML/JS frontend would need a JSON API backend.
- Full line-by-line code audit.

## Diagnostic scripts (run on VPS in trading_bot/ with `.venv/bin/python`)
`check_db.py` `check_conn.py` `check_signals.py` `check_agg.py` `check_entry.py`
`check_engine.py` `check_status.py` `check_live.py` `check_news.py`
`getset.py <key> [value]` (read/write a setting).

## Conventions
After editing source, run `python build_single_file.py` to refresh `bot.py`,
commit, push to the branch, then run the VPS update command above.
