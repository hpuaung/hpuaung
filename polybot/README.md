# Polymarket Smart Hybrid Trading Bot v2.4

Single-file Flask trading bot for **Polymarket** prediction markets. 7-layer
entry filter, multi-market scanning, Kelly position sizing, smart capital
allocation, HuggingFace sentiment, GNews headlines, 2-way Telegram, and a
mobile-friendly dashboard with admin login. Runs paper or real (CLOB) mode.

## Strategy — 7-layer entry filter

| Layer | Check |
|-------|-------|
| L1 Sentiment   | HuggingFace political model score > 0.55 |
| L2 Momentum    | 6-hour price change > 2% |
| L3 EV Edge     | `estimate − market_price` > 8% |
| L4 Liquidity   | volume > $10K, spread < 3% |
| L5 Time        | days to resolution > 14 |
| L6 No Duplicate| no existing position in the same market |
| L7 API Health  | all APIs responding |

**Exits:** E1 target hit `entry + edge×70%` · E2 sentiment drops below 0.30 ·
E3 time safety (48h before resolution).

**Capital allocation:** rank-1 signal → 40% of free capital, rank-2 → 35%,
rank-3 → 25%, with a 20% reserve always locked.

## Layout (`bot.py`, 19 sections)

Imports · env config · logging · SQLite manager · shared state · Telegram ·
HuggingFace sentiment · GNews · Polymarket client & scanner · 7-layer filter ·
Kelly sizing · order executor · position monitor · risk manager · ranking &
allocation · main loop · daily report · Flask dashboard · entry point.

The whole bot is one file; SQLite (`polybot.db`) is the shared state between the
trading loop and the dashboard threads.

## Install & run

```bash
cd polybot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in keys
python bot.py               # dashboard at http://0.0.0.0:8501
```

Starts in **PAPER** mode unless both `POLYGON_WALLET_PRIVATE_KEY` and
`POLYMARKET_FUNDER_ADDRESS` are set. Open the dashboard, log in with
`ADMIN_PASSWORD`, and configure pairs/keys from Settings.

## VPS deployment (AWS Ubuntu)

```bash
# start
cd ~/polybot && source venv/bin/activate \
  && sudo fuser -k 8501/tcp ; sleep 2 \
  && nohup python3 bot.py > output.log 2>&1 & echo $! > bot.pid
# stop
sudo fuser -k 8501/tcp
# log
tail -n 20 ~/polybot/output.log
```

## Safety & secrets

- Secrets live in `.env` only — never commit it (see `.gitignore`).
- Auto disk-clean runs hourly; the bot is tuned for a small (1 vCPU / 1 GB) VPS.
- Test thoroughly in paper mode before enabling real funds.
