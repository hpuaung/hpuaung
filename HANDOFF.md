# 🤝 HANDOFF — continue this project in any new session

This bot's entire state lives in **git**, not in any chat. To continue in a new
Claude Code session, point it at this repo + branch and have it read this file.

- **Repo:** `hpuaung/hpuaung`
- **Branch:** `claude/binance-futures-bot-debug-sks967`
- **App folder:** `trading_bot/`  ·  **Phone app:** `flutter_app/`
- **Live VPS:** `150.95.84.241`, dashboard `http://150.95.84.241:8080` (Ubuntu).

## ⚠️ READ THIS FIRST — the honest bottom line (2026-07 testing)

After ~1 month of tuning + **~20 rigorous backtests across a full timeframe
sweep**, the verdict has TWO parts. Short-timeframe direction prediction has NO
edge — but a **timeframe sweep found ONE real, broad edge: breakout on the DAILY
(1d) chart.** That is now what the bot trades on paper.

### ✅ The edge we found — breakout on 1d (deployed, paper)
- `strategy_backtest.py all 1d 1500` over **~4 years**: breakout alone posts
  **PF 1.68, expectancy +0.187R, ~72% win**, and it is **broad — positive on 5 of
  7 pairs**, not one lucky pair. This clears the deploy bar (expectancy > 0 AND
  PF > 1.3) by a wide margin and on a long sample.
- Why it works where scalping doesn't: a daily-close breakout of a multi-day
  consolidation is a *structural* event (real supply/demand shift), not 5m noise.
  Fees are trivial vs the move size, and there are only **~26 trades/year** — this
  is patient SWING trading, not scalping. Expect **days-to-weeks between entries**;
  `... swing -> NONE` on most bars is NORMAL, not a bug.
- **Live config (set via getset.py, paper):** `swing_bot_on=1 scalping_bot_on=0
  swing_mode=paper swing_timeframe=1d swing_auto_tf=0 swing_breakout_on=1`
  (trend/reversion/hybrid OFF) `swing_auto_tpsl=1 swing_partial_tp=1
  atr_sl_enabled=0 min_rr_ratio=0.3 min_tp_pct=0.0 ai_model_on=0` + all swing
  filters (session/mtf/win/dir/hour/session_pair/min_lgbm) OFF so the raw breakout
  edge isn't filtered away. `min_rr_ratio` MUST be low (0.3) — breakout's native
  SL sits at the broken level (tight), so a high min_rr would block every entry.

### ❌ What has NO edge (do not revive these on real money)
- Live paper scalping: ~17–29% win rate, net **negative** (~$100 → $92).
- All 4 strategies on **1h & 4h**: breakeven-to-losing noise (PF 0.98–1.03).
  - `trend`: **74–76% win but LOSES** (wins ~+0.3R, losses ~-1.0R) — high win
    rate ≠ profit (tiny TP = the trap).
  - `hybrid` (marketed "70–80%"): actually **39% win, PF ~1.0** — marketing false.
  - `breakout` at 1h/4h: +0.004R → **-0.14R** (small-sample positive vanished).
    The edge only appears at **1d** — timeframe is everything here.
  - `reversion`: 22–31% win, negative.
- Extras all failed: entry filters (confirm/ADX/volume), R:R sweep (2/3/4),
  AI/LightGBM model on **or** off (model is noise — losers scored ~same), signal
  **flip** (R:R geometry kills it), **grid** (profits in chop, the underwater bag
  blows up in trends: -11.6% over 62 days).
- Root cause of the scalp failures: **entries fired at reversal points — 63% of
  losing trades stopped out in <12 min.** You cannot predict short-term direction.

**Lesson (from this session):** don't declare "no edge" from a couple of
timeframes. A full sweep (5m→1d) is what surfaced the 1d breakout edge. Any new
idea gets the same treatment before it's dismissed OR deployed.

### Still to validate
Paper-validate that the live 1d config reproduces the backtest — but this is
SLOW (~26 trades/yr, so weeks/months for a meaningful sample). Optionally widen
the backtest to BTC/ETH/BNB/DOGE/ADA/DOT for out-of-sample confidence before ever
considering real money. Until validated live, keep it **paper only**.

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
  ALONE (its own SL/TP): win% / expectancy / PF. **Start here.** `tf` accepts a
  comma list to SWEEP timeframes (`5m,15m,1h,4h,1d`); add `detail` for per-pair,
  `only=breakout` to isolate one. **This is the tool that found the 1d breakout
  edge — always sweep up to 1d before concluding "no edge".**
- **`grid_backtest.py [pairs] [tf] [candles]`** — grid trading (static + reanchor).
- **`trend_backtest.py`** — HTF trend-following with chandelier trail.
- `getset.py <key> [value]` — read/write a setting.
- Backtests need Binance access (VPS has it; sandboxed dev envs are blocked). For
  long runs over flaky SSH: `nohup ... > /tmp/x.txt 2>&1 & echo STARTED` then `cat`.

## If you (or anyone) want to keep going — the honest paths
1. **Validate the 1d breakout on paper (current path).** The bot is configured
   for it now. Let it run on paper for a real sample (weeks/months — 1d is slow)
   and compare live PF to the backtested 1.68. Keep it paper until it proves out.
2. **Harden the 1d breakout before real money:** widen the backtest to more pairs
   (BTC/ETH/BNB/DOGE/ADA/DOT) and out-of-sample date ranges; confirm the edge
   isn't a 5/7-pair artifact. Only then consider tiny real size.
3. **Structural edges** (NOT direction prediction): cross-exchange arbitrage,
   funding-rate farming, market-making. Different build; real but competitive.
4. Any OTHER new idea MUST clear `strategy_backtest.py` (sweep to 1d) with
   expectancy > 0 and PF > 1.3 on a long sample BEFORE deploying. Every
   short-timeframe idea tried so far failed that bar; only 1d breakout passed.

## Conventions
After editing source: `python build_single_file.py` → commit → push → run the
VPS update command above. Diagnostic/backtest scripts are not bundled into bot.py.
