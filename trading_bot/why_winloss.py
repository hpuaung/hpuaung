#!/usr/bin/env python3
"""why_winloss.py — the pro edge-refinement readout.

Entry snapshots are ALREADY logged: every closed trade stored its 23-feature
market state (rsi, adx, ema alignment, atr, macd, ...) paired with win/loss in
the `learning` table. This reads them back and asks the only question that
refines an edge: WHICH entry conditions separated winners from losers?

For each feature it prints the AVERAGE for winners vs losers and a standardized
separation score (|Δ| / pooled-std). Features at the top are where winners and
losers looked most different — candidate FILTERS (e.g. "only enter when ADX>25"
if winners averaged ADX 28 and losers 18).

  cd /root/hpuaung/trading_bot && .venv/bin/python why_winloss.py
  .venv/bin/python why_winloss.py swing      # one strategy only
  .venv/bin/python why_winloss.py all real   # real trades only
"""
import sys
import json
import math
import warnings
warnings.filterwarnings("ignore")

import database as db

db.init_db()

# Feature order MUST match utils/indicators.build_features exactly.
NAMES = [
    "rsi", "ema8_ratio", "ema21_ratio", "ema50_ratio", "ema200_ratio",
    "bb_position", "bb_width_ratio", "stoch_k", "cci_norm", "adx",
    "macd_hist", "supertrend_dir", "atr_ratio", "volume_ratio", "funding_rate",
    "oi_change_pct", "sentiment_score", "candle_body_ratio", "upper_wick_ratio",
    "lower_wick_ratio", "trend_sig", "reversion_sig", "breakout_sig",
]

_a = sys.argv[1:]
strat = (_a[0] if len(_a) > 0 and _a[0] != "all" else None)
mode = (_a[1] if len(_a) > 1 else "paper")   # paper (default) / real / all

conn = db.get_conn()
q = "SELECT strategy, pair, features, won, net_pnl FROM learning WHERE features IS NOT NULL"
params = []
if strat:
    q += " AND strategy=?"
    params.append(strat)
if mode == "paper":
    q += " AND paper_mode=1"
elif mode == "real":
    q += " AND paper_mode=0"
rows = conn.execute(q, params).fetchall()

wins, losses = [], []      # each = list of 23 floats
for r in rows:
    try:
        f = json.loads(r["features"])
    except Exception:  # noqa: BLE001
        continue
    if not isinstance(f, list) or len(f) < len(NAMES):
        continue
    (wins if r["won"] else losses).append([float(x) for x in f[:len(NAMES)]])

print("=" * 74)
print(f"WHY WIN vs LOSS — entry conditions   strategy={strat or 'ALL'}  mode={mode}")
print("=" * 74)
print(f"winners={len(wins)}   losers={len(losses)}")
if len(wins) < 5 or len(losses) < 5:
    print("\n⚠️  Not enough closed trades yet (need >=5 wins AND >=5 losses) for a")
    print("    meaningful split. Let the bot accumulate trades, then re-run. The")
    print("    logging is working — this just needs more samples.")
    raise SystemExit(0)


def col(rowsi, j):
    return [row[j] for row in rowsi]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def std(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


results = []
for j, name in enumerate(NAMES):
    wv, lv = col(wins, j), col(losses, j)
    mw, ml = mean(wv), mean(lv)
    sw, sl = std(wv), std(lv)
    pooled = math.sqrt((sw ** 2 + sl ** 2) / 2) or 1e-9
    sep = abs(mw - ml) / pooled          # standardized separation
    results.append((sep, name, mw, ml, mw - ml))

results.sort(reverse=True)
print("\nRanked by how differently WINNERS vs LOSERS looked at entry")
print("(bigger separation = stronger candidate filter):\n")
print(f"{'feature':16}{'winners':>10}{'losers':>10}{'Δ(w-l)':>10}{'separation':>12}")
print("-" * 74)
for sep, name, mw, ml, d in results:
    arrow = "↑win" if d > 0 else "↓win"
    star = "  <<<" if sep >= 0.5 else ("  <" if sep >= 0.3 else "")
    print(f"{name:16}{mw:>10.3f}{ml:>10.3f}{d:>+10.3f}{sep:>12.2f}{star}  {arrow if sep>=0.3 else ''}")

print("\n" + "=" * 74)
print("READ THIS:")
print(" - Rows with separation >= 0.5 (<<<) are the strongest signals: winners")
print("   and losers genuinely differed on that condition at entry.")
print(" - Example: if adx winners=28 losers=17 sep=0.7, a filter 'skip entries")
print("   with adx<22' would have removed mostly losers -> higher PF.")
print(" - ema*_ratio ~1.0 = price at that EMA; >1 above, <1 below (trend align).")
print(" - Confirm a candidate filter with a backtest BEFORE turning it on live —")
print("   a split on few trades can be noise. This RANKS suspects; backtest is")
print("   the judge.")
