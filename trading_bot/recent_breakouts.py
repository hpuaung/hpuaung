#!/usr/bin/env python3
"""recent_breakouts.py — answer "why no entries in a week?" with data. For every
pair it replays the REAL breakout.run() over the most recent ~40 daily candles and
reports how many days ago the last breakout signal fired, plus a count of signals
in the last 7 / 14 / 30 days across the whole basket.

If the basket has had ~0 breakouts in the last 7 days, the live bot entering 0 is
CORRECT (quiet/rangebound market). If there WERE recent breakouts the live bot
missed, that's a bug to chase.

Usage:  .venv/bin/python recent_breakouts.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc
from utils import indicators as ind
from strategies import breakout

db.init_db()

PAIRS = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
LIMIT = 400        # ~13 months of daily candles (plenty of warmup + recent window)
WINDOW = 45        # evaluate the last ~45 closed daily candles


def _valid(res):
    return res and res.get("signal", "NONE") in ("BUY", "SELL")


d7 = d14 = d30 = 0
rows = []
for sym in PAIRS:
    try:
        df = bc.get_ohlcv_deep(sym, "1d", LIMIT, api_mode="real")
        if df is None or len(df) < 260:
            rows.append((sym, None, 0))
            continue
        df = ind.compute_indicators(df)
        # drop the still-forming last candle, like the live engine does
        df = df.iloc[:-1]
        n = len(df)
        last_ago = None
        cnt30 = 0
        start = max(210, n - WINDOW)
        for i in range(start, n):
            sub = df.iloc[:i + 1]
            if not ind.has_enough(sub):
                continue
            r = breakout.run(sub, mtf=False)
            if _valid(r):
                ago = (n - 1) - i          # candles (days) before the last closed one
                if last_ago is None or ago < last_ago:
                    last_ago = ago
                if ago <= 30:
                    cnt30 += 1
                if ago <= 7:
                    d7 += 1
                if ago <= 14:
                    d14 += 1
                if ago <= 30:
                    d30 += 1
        rows.append((sym, last_ago, cnt30))
    except Exception as e:  # noqa: BLE001
        rows.append((sym, f"ERR {e}", 0))

print("=" * 60)
print("RECENT BREAKOUTS  (real breakout.run over the last ~45 daily candles)")
print("=" * 60)
print(f"{'pair':10}{'last signal':>16}{'signals(30d)':>14}")
for sym, ago, c in rows:
    if ago is None:
        txt = "none in window"
    elif isinstance(ago, str):
        txt = ago
    else:
        txt = f"{ago} days ago"
    print(f"{sym:10}{txt:>16}{c:>14}")
print("-" * 60)
print(f"BASKET breakout signals — last 7d: {d7} | 14d: {d14} | 30d: {d30}")
print("-" * 60)
if d7 == 0:
    print("VERDICT: ~0 breakouts in the last 7 days across all pairs → the market")
    print("has been rangebound. The live bot entering 0 is CORRECT, not a bug.")
else:
    print(f"VERDICT: {d7} breakout(s) fired in the last 7 days. If the live bot")
    print("opened none of them, investigate the engine (guard/cooldown/mtf).")
