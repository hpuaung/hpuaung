# Binance Futures Trading Bot

Production-ready Binance **Futures-only** trading bot with a Streamlit
mobile-friendly dashboard, SQLite storage, two engines (Scalping + Swing),
LightGBM AI hybrid, GNews + HuggingFace FinBERT sentiment, and a full global
risk guard. Optimised to run on a 1GB Ubuntu VPS (cloud HuggingFace calls only,
no local model loading).

## Architecture

`streamlit run app.py` is the single main process. It starts three daemon
threads exactly once and uses **SQLite as the message bus** between them:

| Thread | Module | Job |
|--------|--------|-----|
| Trading engine | `engine.py` | scan pairs, aggregate signals, run guards, place/monitor orders |
| Telegram bot | `notifications/telegram_bot.py` | `/start /dashboard /status /stop` + notifications |
| VPS monitor | `utils/vps_optimizer.py` | RAM watch + `gc.collect()` + cache flush |

The UI only ever reads/writes settings, positions and trades in `trading_bot.db`;
it never restarts the engine on a Streamlit rerun.

## Install & Run

```bash
cd trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Then open the dashboard, go to **⚙️ Settings → API Configuration**, paste your
Binance Testnet (or Live) keys, **Test Connection**, optionally add GNews /
HuggingFace / Telegram tokens, pick your pairs on the Dashboard, then start an
engine from the **⚡ Scalping** or **📈 Swing** tab.

> Starts in **PAPER** mode by default. Live equity is auto-read from Binance —
> nothing is hardcoded. Switching to REAL requires explicit confirmation.

## Layout

```
app.py                  Streamlit UI (4 tabs)
engine.py               Main trading loop + signal aggregator
database.py             SQLite CRUD + message bus
strategies/             trend / reversion / breakout / ai_hybrid
models/train.py         LightGBM training (background, RAM-optimised)
execution/              orders, position_manager, risk_guard
notifications/          telegram_bot
utils/                  binance_client, indicators, news, rate_limiter, vps_optimizer
```

## Safety

- **Hard cap guard** (`Lev × Risk ≤ cap`) is always on and cannot be disabled.
- Health-ratio drawdown ladder: 75% → 0.75x, 50% → 0.50x (swing only),
  25% → **all engines stop** + cancel orders + Telegram alert.
- Daily-loss, correlation, session, blackout and concurrency guards run before
  every entry.

This is trading software for **your own authorised account**. Test thoroughly on
testnet/paper before risking real funds.
