# 🤝 HANDOFF — continue this project in any new session

This bot's entire state lives in **git**, not in any chat. To continue in a new
Claude Code session, point it at this repo + branch and have it read this file.

- **Repo:** `hpuaung/hpuaung`
- **Branch:** `claude/binance-futures-bot-debug-sks967`
- **App folder:** `trading_bot/`  ·  **Phone app:** `flutter_app/`
- **Live VPS:** `150.95.84.241`, dashboard `http://150.95.84.241:8080` (Ubuntu).

## ⚠️ READ THIS FIRST — the honest bottom line (2026-06 testing)

After ~1 month of tuning + **~15 rigorous backtests**, the conclusion is:
**this strategy-based auto-bot has NO tradeable edge. Do NOT put real money in.**
It is safe on paper as a learning tool only. The evidence (all reproducible with
the tools below):

- Live paper: ~17–29% win rate, net **negative** (~$100 → $92).
- Backtest of all 4 strategies (1h & 4h, ~62–250 days): best case is **breakeven
  noise** (PF 0.98–1.03); most lose after fees.
  - `trend`: **74–76% win rate but LOSES** (wins ~+0.3R, losses ~-1.0R) — proof
    that high win rate ≠ profit.
  - `hybrid` (marketed "70–80%"): actually **39% win, PF ~1.0** — marketing false.
  - `breakout`: +0.004R at 1h (noise) → **-0.14R at 4h** (small-sample positive
    vanished with more data).
  - `reversion`: 22–31% win, negative.
- Extras all failed too: entry filters (confirm/ADX/volume), R:R sweep (2/3/4),
  AI/LightGBM model on **or** off (model is noise — losers had ~same score),
  signal **flip** (R:R geometry kills it), **grid** (profits in chop but the
  underwater bag blows up in trends: -11.6% over 62 days).
- Root cause: **entries fire at reversal points — 63% of losing trades stop out
  in <12 min.** You cannot predict short-term direction; grid just moves the risk
  to a tail that eventually hits.

Why: markets are ~efficient; retail has no speed/data edge; fees eat any thin
margin. Real algo profit comes from structural edges (arb, market-making,
funding) + infrastructure, not "AI predicts direction."

## Architecture (trading_bot/)
- `app.py` — Streamlit UI. Now serves UI only when `ENGINE_EXTERNAL=1`.
- `run_engine.py` — **engine runs as its own process** (see systemd below) so the
  1GB VPS dashboard stays responsive.
- `engine.py` — trading loop, `aggregate_signal`. `db.auto_flag()` = a setting
  that Auto Pilot forces on.
- `database.py` — SQLite message bus. `_seed_from_env()` seeds API keys from
  `trading_bot/.env` (git-ignored) so they survive DB resets.
- `strategies/` trend, reversion, breakout, ai_hybrid.
- `execution/` orders (SL-aware sizing), position_manager, risk_guard.
- `bot.py` — auto-generated single-file build (`python build_single_file.py`).

## Two systemd services (engine split — done this session)
- `futures-engine` → `.venv/bin/python run_engine.py` (the trading loop)
- `futures-bot` → `streamlit run app.py`, with drop-in `Environment=ENGINE_EXTERNAL=1`
- **Update the VPS after a push:**
  ```
  ssh root@150.95.84.241 'cd /root/hpuaung && git pull -q origin claude/binance-futures-bot-debug-sks967 && systemctl restart futures-engine futures-bot && systemctl is-active futures-engine futures-bot'
  ```

## Changes made this session (all live)
Crash-loop fix (orphan on :8080) + journald cap; `min_rr_ratio` 2.0; `min_tp_pct`
0.4 fee floor; re-entry cooldown on any losing close; ATR-based SL (`atr_sl_mult`
2.5, sizing is SL-aware so wider SL = smaller qty, same $ risk); `learn_from_paper`
(win model learns from paper); **Auto Pilot** master switch (`auto_pilot` + per-
section auto via `auto_flag`); global risk auto (`global_auto_risk`); AI model
on/off toggle (`ai_model_on`, default proven to be noise); `.env` API seeding;
Apply-once forms in Global Risk (reduce dashboard reruns).

## Diagnostic + backtest tools (run on VPS in trading_bot/ with `.venv/bin/python`)
- `check_status.py` `check_live.py` `check_perf.py [days]` — live trade health/PF.
- `check_breakdown.py` — win% by strategy/side/pair/close-reason.
- `check_winloss.py` — winners vs losers (hold time, lgbm, session) → *why*.
- `check_skips.py` — which filter/guard is blocking entries.
- **`backtest.py [pairs] [tf] [candles] [rr] [slmult]`** — replays ai_hybrid entry,
  compares baseline vs volume/ADX/confirm filters at a fixed R:R.
- **`strategy_backtest.py [pairs] [tf] [candles]`** — each of the 4 strategies
  ALONE (its own SL/TP): win% / expectancy / PF. **Start here.**
- **`grid_backtest.py [pairs] [tf] [candles]`** — grid trading (static + reanchor).
- **`trend_backtest.py`** — HTF trend-following with chandelier trail.
- `getset.py <key> [value]` — read/write a setting.
- Backtests need Binance access (VPS has it; sandboxed dev envs are blocked). For
  long runs over flaky SSH: `nohup ... > /tmp/x.txt 2>&1 & echo STARTED` then `cat`.

## If you (or anyone) want to keep going — the honest paths
1. **Stop / keep as paper.** The bot is safe on paper (no keys needed; keys are
   only for showing real balance). Recommended.
2. **Structural edges** (NOT direction prediction): cross-exchange arbitrage,
   funding-rate farming, market-making. Different build; real but competitive.
3. If chasing this style further: any new idea MUST clear `backtest.py` /
   `strategy_backtest.py` with expectancy > 0 and PF > 1.3 on 60+ days BEFORE
   deploying. Every idea tried so far has failed that bar.

## Conventions
After editing source: `python build_single_file.py` → commit → push → run the
VPS update command above. Diagnostic/backtest scripts are not bundled into bot.py.
