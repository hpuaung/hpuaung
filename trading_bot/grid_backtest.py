#!/usr/bin/env python3
"""grid_backtest.py — simulate a static GRID bot on historical candles to test if
grid trading (harvesting price oscillation) has edge on these pairs BEFORE we
build a live grid engine. Grid does not predict direction; it buys dips and sells
the next level up, profiting from chop — but it bleeds in strong trends (the open
inventory goes underwater). This measures both.

For each pair: a price band around the first candle, N evenly spaced levels.
Walk candles: buy a lot when the low touches a level, sell it when the high
reaches the next level up. Report REALIZED grid profit, the OPEN (underwater)
inventory marked to the last price, and the NET. Net > 0 with many round-trips
and a small bag = promising. Net dragged negative by the bag = trend risk wins.

Usage:
  .venv/bin/python grid_backtest.py [pairs] [interval] [candles]
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc

db.init_db()

_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
INTERVAL = _a[1] if len(_a) > 1 else "5m"
LIMIT = int(_a[2]) if len(_a) > 2 else 1500

if pairs_arg == "all":
    PAIRS = [p.strip() for p in db.get_setting(
        "selected_pairs", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

FEE = 0.0004     # taker fee per side
LOT = 10.0       # $ notional per grid lot

# (label, band fraction around start price, number of levels)
CONFIGS = [
    ("band5%  x10", 0.05, 10),
    ("band8%  x15", 0.08, 15),
    ("band12% x20", 0.12, 20),
    ("band20% x30", 0.20, 30),
]


def simulate(hi, lo, c, band, levels):
    p0 = c[0]
    lower, upper = p0 * (1 - band), p0 * (1 + band)
    grid = [lower + (upper - lower) * i / levels for i in range(levels + 1)]
    holding = [False] * levels          # level i: buy at grid[i], sell at grid[i+1]
    entry = [0.0] * levels
    realized = 0.0
    roundtrips = 0
    for k in range(1, len(c)):
        H, L = hi[k], lo[k]
        # sells first (conservative: a lot bought this candle can't sell same candle)
        for i in range(levels):
            if holding[i] and H >= grid[i + 1]:
                realized += LOT * (grid[i + 1] - grid[i]) / grid[i] - LOT * FEE * 2
                holding[i] = False
                roundtrips += 1
        # buys: price dipped down to a level we are not holding
        for i in range(levels):
            if not holding[i] and L <= grid[i] <= upper:
                holding[i] = True
                entry[i] = grid[i]
    last = c[-1]
    unreal = 0.0
    openpos = 0
    for i in range(levels):
        if holding[i]:
            openpos += 1
            unreal += LOT * (last - entry[i]) / entry[i] - LOT * FEE
    cap = LOT * levels
    net = realized + unreal
    return dict(realized=realized, unreal=unreal, net=net, roundtrips=roundtrips,
                openpos=openpos, cap=cap, inrange=lower <= last <= upper,
                pricechg=100 * (last - p0) / p0)


print("=" * 74)
print(f"GRID BACKTEST  pairs={len(PAIRS)} interval={INTERVAL} candles={LIMIT}  lot=${LOT:.0f}")
print("net = realized grid profit + open inventory marked to last price")
print("=" * 74)

data = {}
for sym in PAIRS:
    try:
        df = bc.get_ohlcv(sym, INTERVAL, LIMIT, api_mode="real")
        if df is None or len(df) < 100:
            print(f"  {sym}: not enough data")
            continue
        data[sym] = (df["high"].astype(float).tolist(),
                     df["low"].astype(float).tolist(),
                     df["close"].astype(float).tolist())
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: ERROR {e}")

for label, band, levels in CONFIGS:
    print(f"\n--- {label}  (capital=${LOT*levels:.0f}/pair) ---")
    tot_real = tot_unreal = tot_net = 0.0
    for sym, (hi, lo, c) in data.items():
        r = simulate(hi, lo, c, band, levels)
        tot_real += r["realized"]; tot_unreal += r["unreal"]; tot_net += r["net"]
        flag = "" if r["inrange"] else "  ⚠️LEFT-RANGE"
        print(f"  {sym:9} trips={r['roundtrips']:<4} realized=${r['realized']:+6.2f} "
              f"bag=${r['unreal']:+6.2f}(x{r['openpos']}) NET=${r['net']:+6.2f} "
              f"price{r['pricechg']:+.0f}%{flag}")
    cap = LOT * levels * len(data)
    print(f"  {'TOTAL':9} realized=${tot_real:+.2f}  bag=${tot_unreal:+.2f}  "
          f"NET=${tot_net:+.2f}  ({100*tot_net/cap:+.1f}% of ${cap:.0f} capital)")

print("\n" + "=" * 74)
print("READ: want NET > 0 with many round-trips and a small bag. If the open")
print("bag (trend losses) swamps realized grid profit -> grid fails here too.")
