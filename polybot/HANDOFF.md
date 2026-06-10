# 🤝 HANDOFF — Polymarket bot (continue in any new session)

This bot's state lives in **git**, not in chat. Point a new Claude Code session
at this repo + branch and read this file to continue.

- **Repo:** `hpuaung/hpuaung`  ·  **Folder:** `polybot/`  ·  **Branch:** `claude/hello-r7q8se`
- **Main file:** `polybot/bot.py` (single-file Flask app, ~3000 lines, 19 sections).
- **Live VPS:** AWS Ubuntu `54.79.57.179`, dashboard `http://54.79.57.179:8501`,
  path `~/polybot/bot.py`, DB `~/polybot/polybot.db`.
- This is **separate** from the Binance Futures bot in `trading_bot/`.

## What it is
Polymarket prediction-market bot: 7-layer entry filter, multi-market scan, Kelly
sizing, smart capital allocation, HuggingFace sentiment + GNews, 2-way Telegram,
Flask dashboard with admin login. Paper + real (CLOB) modes. See `README.md`.

## Status (per owner, v2.4)
- Paper trading mode: **ON**
- L2 (momentum) bypass: **ON** (DB setting)
- GNews: optional, 100 req/day free tier; falls back to HuggingFace direct
  (`router.huggingface.co/hf-inference`) when quota is exhausted.
- Auto disk-clean: hourly.

## Known issues (VPS)
- Disk ~93% full (6.7 GB box).
- GNews 100 req/day quota exhausts quickly.
- No swap configured (0 B).

## Code audit — fixed vs remaining
**Fixed (P0, paper-mode correctness):**
- Kelly sizing now uses the L3 win probability (`_estimated_prob`) and the
  side-effective entry price, not the raw sentiment score / YES price
  (`trading_loop`).
- NO-side positions: `monitor_positions` and `execute_sell_order` now convert
  the CLOB YES-token price to the NO effective price (`1 - price`) so
  entry/target/PnL stay on one basis.

**Still open:**
- 🔴 **Live CLOB orders not functional.** `OrderArgs(side=...)` is passed
  "YES"/"NO" but the CLOB expects `BUY`/`SELL`; buying NO needs the NO token
  (`clobTokenIds[1]`), which the scanner never stores. Must fix before real mode.
- P1: Gamma API returns `outcomePrices` / `clobTokenIds` as JSON *strings*
  sometimes — `isinstance(..., list)` fails and token_id can be wrong
  (`scan_markets`).
- P2: exit E2 fetches news + HuggingFace for every position every 10 min —
  burns the limited GNews/HF quota.

## Pending / next ideas
- **Live trading entry** — see the live-CLOB bug above; also verify wallet +
  funder address + USDC before trusting real mode.
- SSL / HTTPS for the dashboard.
- Add swap space on the VPS.
- Get `~/polybot` on the VPS onto git (currently the only synced copy is here).

## Secrets
All config is read from env vars (`.env`, gitignored). See `.env.example`:
`POLYGON_WALLET_PRIVATE_KEY`, `POLYMARKET_FUNDER_ADDRESS`, `GNEWS_API_KEY`,
`HUGGINGFACE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`FLASK_SECRET_KEY`, `ADMIN_PASSWORD`. No secrets are hardcoded in `bot.py`.

## Conventions
Edit `bot.py`, test in paper mode, commit, push to the branch. To update the
VPS, pull/copy the new `bot.py` to `~/polybot/` and restart (see README).
