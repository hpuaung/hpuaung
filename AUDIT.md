# 🔍 BOT AUDIT — full end-to-end review vs the current strategy

**Current strategy:** `breakout` on the **1d** chart, **swing** engine, **paper**,
20 pairs. Model-free, rule-based, no training. This document audits every part of
the bot against that config: what runs, what's dead weight, and exactly what each
setting should be.

---

## PART 1 — What actually runs (end-to-end)

```
futures-engine  (run_engine.py, own process)
  ├─ vps_optimizer.start_vps_monitor()   → disk/RAM auto-clean   [KEEP]
  ├─ LightGBM load                        → SKIPPED (ai_model_on=0) [OFF now]
  ├─ telegram_bot.start_command_bot()     → alerts + /commands    [KEEP if used]
  └─ engine.start_engine()                → the trading loop:
        every ~30s, for each of the 20 pairs:
          _gather_indicator_frames()  → fetch 1d klines, compute indicators
          aggregate_signal()          → breakout.run() only (others off)
          risk/entry guards           → min_rr, concurrency, blackout, cooldown
          orders._sizing()            → qty = risk$ / SL-distance (SL-aware)
          position_manager            → manage TP1/2/3, break-even, max-hold, SL

futures-bot  (streamlit app.py, ENGINE_EXTERNAL=1)
  └─ UI only (4 tabs: Dashboard / Scalping / Swing / Settings). Shares the
     SQLite DB with the engine as the message bus.
```

**Live data path for a trade:** 1d candle closes → breakout rule (price breaks
resistance + volume spike + ATR expand) → SL at broken level, TP1/2/3 from
consolidation height → SL-aware sizing → paper fill → partial TP + break-even →
close. **No ML anywhere.**

---

## PART 2 — Dead weight (unused by breakout-1d). Safe to leave OFF; do NOT delete code.

Deleting code risks breaking imports for ~zero RAM gain (these are dormant unless
their toggle is ON). The one real weight item — LightGBM — is already skipped.

| Area | Modules / settings | Status |
|---|---|---|
| **Scalping engine** | all `scalping_*` (~40 settings), scalping tab | OFF (`scalping_bot_on=0`) — dormant |
| **AI / LightGBM** | `models/train.py`, `ai_hybrid`, `ai_monitor.py`, `ai_model_on`, `*_hybrid_on`, `*_ai_threshold`, `*_auto_threshold`, `swing_min_lgbm`, `*_win_filter`, `win_filter_min`, `learn_from_paper`, `lgbm_*` | OFF — **not loaded into RAM** (fixed) |
| **News / sentiment** | `utils/news.py`, `hf_token`, `gnews_api`, `*_news_on`, `*_gnews_*`, `*_hf_min_score` | OFF (`*_news_on=0`) — dormant |
| **Funding / OI** | `*_funding_filter`, `*_funding_weight` | OFF — dormant |
| **Session filter** | `*_session_filter`, `*_london_on`, `*_ny_on`, `*_asia_on`, `*_weekend_off` | OFF — dormant |
| **MTF frames** | `swing_confirm_tf`, `swing_trend_tf` | UNUSED (`swing_mtf_filter=0`, 1d only) |
| **Trend / Reversion** | `strategies/trend.py`, `reversion.py`, `swing_trend_on`, `swing_reversion_on` | OFF — dormant (proven no edge) |
| **Adaptive filters** | `swing_dir_filter`, `swing_hour_filter`, `swing_session_pair_filter` | OFF — dormant |
| **Blackout mode** | `blackout_*` | OFF — dormant |
| **Win-streak optimizer** | `win_streak_bonus_on`, `streak_*` | OFF — dormant |
| **Grid / other backtests** | `grid_backtest.py`, `trend_backtest.py`, `mtf_backtest.py`, `backtest.py` | tools only, never run by engine |

**Model files on disk (deletable to free disk):** `lgbm_model.pkl`, `win_model.pkl`.
```
rm -f /root/hpuaung/trading_bot/lgbm_model.pkl /root/hpuaung/trading_bot/win_model.pkl
```

**Optional UI declutter (safe — hides sections, keeps settings):** Settings tab
expanders 2 (News & AI), 3 (LightGBM), 9 (Performance Optimizer), 8 (Blackout),
and the whole Scalping tab could be hidden while this strategy runs. Say the word.

---

## PART 3 — Settings that had to be TUNED to match breakout-1d (with values)

These are set by `configure_bot.py`. The ⚠️ rows are ones the defaults got WRONG
for a 20-pair 1d basket and were just fixed:

| Setting | Value | Why |
|---|---|---|
| `swing_bot_on` / `scalping_bot_on` | 1 / 0 | swing only |
| `swing_mode` | paper | safe, no keys |
| `swing_timeframe` | **1d** | the only timeframe with an edge |
| `swing_auto_tf` | **0** | must stay off or it jumps to 4h/1d/3d |
| `swing_mtf_filter` | **0** | 1d only; MTF was proven to lose |
| `swing_breakout_on` | 1 | the edge |
| `swing_trend/reversion/hybrid_on` | 0 | no edge / dilute |
| `swing_auto_tpsl` / `swing_partial_tp` | 1 / 1 | use breakout's own SL/TP1-3 |
| `atr_sl_enabled` | 0 | don't override breakout's SL |
| `min_rr_ratio` | **0.3** | breakout's SL is tight; high min_rr blocks all |
| `min_tp_pct` | 0.0 | don't filter small-but-valid TPs |
| `ai_model_on` | 0 | model is noise |
| `max_concurrent_trades` | **20** | take every signal (match backtest) |
| ⚠️ `swing_corr_filter` | **0** | default(2) would block 18/20 on a market-wide breakout |
| ⚠️ `swing_auto_maxhold` | **0** | default force-closes at 7d, cutting winners |
| ⚠️ `swing_max_hold_days` | **30** | let a 1d breakout run toward TP |
| all `swing_*_filter` (session/win/dir/hour/session_pair) | 0 | raw edge, unfiltered |
| `auto_pilot` / `global_auto_risk` | 0 / 0 | no auto override |
| `selected_pairs` | 20 🟢 pairs | per-pair edge (PF>1.3, n≥15) |

**Left on their sensible defaults (used, fine as-is):**
`swing_auto_risk=1` (auto-sizes leverage/risk from balance+health — fine for
paper; R-multiples are scale-invariant), `lev_risk_hard_cap_pct=10`,
`daily_loss_limit_pct=10`, `max_drawdown_pause_pct=25`, `paper_slippage_pct=0.05`,
`swing_auto_be=1`, `swing_trail_auto=0`.

---

## PART 4 — Dashboard settings: what to set / what's auto / what's useless

### 📈 Swing tab (the one that matters)
| Section | Control | Set to |
|---|---|---|
| Mode & Control | Mode | **paper** · press START |
| Timeframe | Auto Timeframe | **OFF** |
| Timeframe | Entry / Confirm / Trend | **1d** / — / — (MTF off) |
| Timeframe | MTF Filter | **OFF** |
| Strategy Mix | AI Hybrid | **OFF** |
| Strategy Mix | Trend / Reversion / **Breakout** | OFF / OFF / **ON** |
| Strategy Mix | News Filter | **OFF** |
| Market Context | Funding/OI | **OFF** |
| Risk Management | Auto Risk Adjust | **ON** (auto = fine) or manual ~1% risk / 3x |
| TP/SL | Auto TP/SL | **ON** (uses breakout's own levels) |
| TP/SL | Auto Break-Even | ON (optional) |
| TP/SL | Trailing SL | OFF |
| TP/SL | Auto Max Hold Days | **OFF** → set **30** |
| Session Filter | Session Filter | **OFF** |
| Correlation Filter | Correlation Filter | **OFF** ⚠️ (or Max = 20) |

### ⚙️ Settings tab (global)
| Expander | Keep / value | Note |
|---|---|---|
| 1. API Configuration | keys optional | paper needs none; keys only show real balance |
| 2. News & AI APIs | **skip** | unused |
| 3. LightGBM Model | **skip** | AI off; don't retrain |
| 4. Global Risk Limits | keep defaults | daily 10% / DD 25% / cap 10% / **concurrent 20** |
| 5. Starting Balance / Reset Paper | use to reset paper $ | e.g. 100 |
| 6. Telegram | optional | alerts to phone |
| 7. VPS Optimizer | **ON** | auto disk/RAM clean (you had a disk alert) |
| 8. News Blackout | **skip** | unused |
| 9. Performance Optimizer | **skip** | win-streak sizing, off |
| 10/11. System / Login | as needed | password, restart, etc. |
| 12. Backup & Restore | use before VPS moves | exports the DB |

### ⚠️ Never turn ON (breaks the proven config)
- **Auto Pilot** (forces every auto/adaptive filter on)
- **Auto Timeframe (swing)** (leaves 1d)
- **AI Hybrid / AI model** (noise)
- **Correlation Filter** (throttles the 20-pair basket)

### 🤖 The only "auto" you WANT on
- **Auto TP/SL** — uses breakout's native SL(broken level)/TP1-3. Correct.
- **Auto Risk Adjust** — sizes positions from balance/health. Fine for paper.
- **VPS Optimizer** — disk/RAM hygiene.
