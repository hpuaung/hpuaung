# Trading specification

Everything three to four months of work actually produced, written so it can be
rebuilt from scratch without repeating any of it. This is the deliverable; the
code is one implementation of it.

Nothing here is opinion. Every number comes from `grand_sweep.py` over 4
strategies x 6 timeframes (30m, 1h, 4h, 6h, 12h, 1d) x 5 R:R values = **120
configurations**, each simulated on 38 USDT-M perpetual pairs with 0.04% fees
per side and 0.05% slippage on stops, and gated on a three-era walk-forward.

---

## 1. What to trade

Two systems, run in parallel, on separate slots.

### Slot A — Trend, 12h, R:R 1:3

Measured: **R/month 2.99**, PF 1.23, win 35%, n=652, 19.6 entries per 30 days,
median hold 13.5 days, eras 1.22 / 1.11 / 1.53.

Entry (all must hold on the **closed** 12h candle):

| | Long | Short |
|---|---|---|
| EMA stack | ema21 > ema50 > ema200 | ema21 < ema50 < ema200 |
| ADX(14) | > 20 | > 20 |
| Supertrend(7, 3.0) direction | up | down |
| MACD(12,26,9) | macd > signal | macd < signal |
| Trigger | within 1.5% of ema21 **or** ADX > 30 | same |

Stop: **ema50 x 0.999** (long) / **ema50 x 1.001** (short). This is the
strategy's own structural stop — do not substitute an ATR stop, that is a
different system and was not the one measured.

Target: entry + 3 x (entry − stop), i.e. a clean 1:3 off the actual stop
distance.

### Slot B — EMA+Stochastic, 12h, R:R 1:2

Measured: **R/month 1.55**, PF 2.18, win 52%, n=91, 2.7 entries per 30 days,
median hold 5.5 days, eras 1.49 / 3.39 / 2.30. The steadiest region in the
whole grid: seven adjacent R:R cells pass across 12h and 1d.

Entry (closed 12h candle):

| | Long | Short |
|---|---|---|
| EMA stack | ema9 > ema21 > ema200 | ema9 < ema21 < ema200 |
| Slope of ema9 and ema21, measured over 5 bars and divided by (5 x ATR) | >= +0.10 | <= −0.10 |
| Slope of ema200 | > 0 | < 0 |
| Stochastic %K(14,3,3) | crossed **up** through 20 within the last 5 bars | crossed **down** through 80 within the last 5 bars |

RSI is **not** part of it. Neither is any price-action rule. Both were tested
and neither improved the result.

Stop: **1.5 x ATR(14)** from entry. Target: 2 x that distance.

### Why these two and not the largest number in the table

`breakout 1d 1:3` scores higher (R/month 3.37) and should not be used. Both
neighbouring R:R values fail the walk-forward and its newest era is 1.24
against a 1.20 threshold — an isolated cell that passes on the third decimal.
`trend 12h` passes at 1:2, 1:2.5 and 1:3 and clears every era with room. A
result with passing neighbours is worth more than a larger result standing
alone.

The two picks together: ~22 entries and ~8 targets a month, 37% wins,
**4.54 R/month**. At 1% risk per trade that is ~4.5% a month.

---

## 2. How to leave a trade

This matters as much as the entry, and getting it wrong is invisible: the
backtest keeps reporting a number the live bot cannot produce.

- **Exit the whole position at the target.** No partial fills, no runner, no
  moving the stop to break-even. Taking 50% off at the target and trailing the
  rest turned an average win of 3R into $0.10 against an average loss of $0.82.
- **Use the strategy's own stop** as specified above. Replacing trend's ema50
  stop with 1.5 x ATR makes it a tighter, different, unmeasured system.
- **No trailing stop.** Not measured.
- **Allow up to 200 bars** (100 days on 12h) before a time-based close. The
  median hold is 13.5 days and winners take far longer than losers — a 1:3
  winner has to travel three times as far as a loser. A 7-day or 30-day cap
  cuts winners while leaving every loser intact, which reads as "the strategy
  never reaches its target."
- One position per symbol per slot, and **never two slots on the same symbol** —
  the two systems agree often enough to double the risk on one coin unnoticed.

---

## 3. How to size

- **Fixed 1% of equity at risk per trade.** Position size = risk / stop
  distance.
- **Do not adapt risk to recent win rate.** A rule that cuts size below a 40%
  win rate permanently halves a system designed to win 35% — and its win rate
  never climbs out, because it is not supposed to. Break-even at 1:3 is 25%.
- **Leverage 5x.** It does not change risk (position size comes from the stop
  distance); it decides whether ~9 concurrent positions fit in the margin.
- **Block an order that falls under the exchange minimum. Never round it up.**
  At $100 with 1% risk, 34 of 38 pairs are tradeable and 4 are not; rounding up
  would silently take 5x the intended risk on those 4.

Same rule for any filter that skips entries on past win rate: derive its floor
from the R:R, `100/(1+rr)`, not from a flat percentage. A flat 35–40% floor
switches off each pair exactly as it accumulates enough trades to be judged.

---

## 4. What was ruled out

The expensive half of the work. Do not re-test these.

- **Mean reversion is dead.** All 20 timeframe/R:R cells lose. At 12h 1:1 its
  profit factor is 0.33 and it fires ~970 times a month. Not a strategy.
- **Nothing works at 6h or below.** 30m, 1h, 4h and 6h: 80 cells, zero
  survivors, across every strategy. Edge exists only at 12h and 1d. There is no
  scalping system here and there never was.
- **Maker orders are not worth building.** A fill-window simulation moved the
  profit factor by +0.01 to +0.05.
- **The AI hybrid cannot be honestly measured** — no history for its news
  score, and its model was trained on the same data, so any backtest of it
  flatters itself. It also forces the reversion signal in regardless of the
  toggles. Leave it off.
- `breakout` is the only strategy with a positive expectancy at every
  timeframe, and is the natural third candidate if either pick decays.

---

## 5. Execution requirements

Bugs that were found the hard way. A rebuild that omits these will rediscover
them with real money.

- **A position without a stop must not exist.** If the stop order fails, close
  the position immediately.
- **One authority over exits.** Either exchange-side targets or a software
  monitor, never both.
- **Reconcile against the exchange.** If the exchange shows flat and the bot
  shows open, record the close.
- **Enter at the live price, not the signal candle's close** — on 12h that
  close can be hours old — and shift the stop and target by the same offset so
  the risk distance and R:R survive.
- **Evaluate signals on closed candles only.** A forming candle flickers
  indicators across their thresholds and the signal is missed.
- **Cooldown after a stop.** The entry candle's signal is unchanged after a
  stop-out, so re-entry immediately repeats the same losing trade.
- **Stamp every trade with the settings that produced it** — a config hash plus
  timeframe, strategy, R:R and risk. Without it, changing one setting makes the
  entire history unreadable and the only honest answer is "start over". With
  it, a change opens a new bucket and the old ones stay valid.
- **Report configuration drift.** The failure that cost months was silent: the
  bot looked correct while trading something else. Warn — do not block. A guard
  that stops trading on its own judgement is how this bot went 600 days without
  an entry.

---

## 6. How the result was measured

Reusable method, independent of these strategies.

- Rank by **R/month = expectancy in R x entries per 30 days**. Profit factor
  alone rewards a system that trades three times a month and never compounds.
- Split the history into **three equal eras**; require every era above 1.0 and
  the newest at or above 1.2. This is what killed reversion.
- Prefer a cell whose **neighbours also pass**. An isolated winner in a grid is
  usually the grid's luckiest cell.
- Model fees and slippage before comparing frequencies, or the fast systems
  look better than they are.
- Eras are fractions of each timeframe's own history, and the timeframes cover
  different spans — so an era number is **not** comparable across timeframes.
  Convert to calendar dates before reading anything into it.

---

## 7. What is still unproven

Stated plainly so a rebuild does not inherit false confidence.

- **No real order has ever been placed.** Every trade on record is paper. Live
  fills, partial fills, margin rejections and funding have never been exercised.
- The 46 recorded paper trades were taken under unknown settings — 44 of them
  predate config stamping — and 16 were closed by hand. They cannot be used to
  judge anything, including these two picks.
- These strategies have therefore **never run live in the configuration
  specified above**. The numbers in section 1 are backtest expectations, and a
  backtest is always kinder than a live fill.
- Expect roughly one month in four to finish negative, and a run of about 12
  consecutive losses somewhere in a year. That is the arithmetic of a 37% win
  rate, not a fault.
