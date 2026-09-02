#!/usr/bin/env python3
"""funding_backtest.py — measure the historical yield of delta-neutral FUNDING
FARMING before building it. Strategy: hold spot (long) + short the perp of the
same size = no price risk, and collect the 8-hourly funding the short side earns.

For each pair it pulls the real Binance funding-rate history and reports:
  - passive:  annualised yield if you just held the hedge the whole time
              (= sum of ALL funding; you also PAY when funding goes negative)
  - pos-only: annualised yield if you only hold while funding is positive and
              step out when it's negative, minus round-trip fees per episode
  - %pos:     how often funding was positive (higher = friendlier to the farm)

Yields are on the PERP NOTIONAL. Real yield on total capital is lower because a
delta-neutral hedge ties up ~1.5x (spot + perp margin). Honest, not marketing.

Usage:  .venv/bin/python funding_backtest.py [pairs] [events]
        .venv/bin/python funding_backtest.py all 3000     # ~2.7yr (3/day)
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc

db.init_db()
_a = sys.argv[1:]
pairs_arg = _a[0] if len(_a) > 0 else "all"
EVENTS = int(_a[1] if len(_a) > 1 else 3000)     # funding events (8h each)

LIQUID = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,"
          "LINKUSDT,LTCUSDT,DOTUSDT,TRXUSDT,ATOMUSDT,NEARUSDT,FILUSDT,AAVEUSDT,"
          "INJUSDT,SUIUSDT,ARBUSDT,OPUSDT")
if pairs_arg == "all":
    PAIRS = [p.strip() for p in LIQUID.split(",") if p.strip()]
else:
    PAIRS = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]

PER_YEAR = 365 * 3          # 3 funding events per day
ROUNDTRIP_FEE = 0.0028      # enter+exit, both legs (spot ~0.1% + perp ~0.04%)*2


def fetch_funding(symbol, total):
    client = bc.get_client("real")
    rows = []
    end = None
    while len(rows) < total:
        kw = dict(symbol=symbol, limit=1000)
        if end is not None:
            kw["endTime"] = end
        page = client.futures_funding_rate(**kw)
        if not page:
            break
        rows = page + rows
        end = int(page[0]["fundingTime"]) - 1
        if len(page) < 1000:
            break
    # de-dup + sort by time
    seen = {}
    for r in rows:
        seen[int(r["fundingTime"])] = float(r["fundingRate"])
    return [seen[t] for t in sorted(seen)]


def analyse(rates):
    n = len(rates)
    if n < 100:
        return None
    years = n / PER_YEAR
    total = sum(rates)
    pos = [r for r in rates if r > 0]
    pct_pos = 100 * len(pos) / n
    passive_ann = (total / years) * 100
    # positive-only: count contiguous positive episodes for the fee cost
    episodes = 0
    prev = False
    for r in rates:
        cur = r > 0
        if cur and not prev:
            episodes += 1
        prev = cur
    pos_net = sum(pos) - episodes * ROUNDTRIP_FEE
    posonly_ann = (pos_net / years) * 100
    return dict(n=n, years=years, passive=passive_ann, posonly=posonly_ann,
                pct_pos=pct_pos, episodes=episodes)


print("=" * 70)
print(f"FUNDING FARMING BACKTEST  (delta-neutral, {EVENTS} events ≈ "
      f"{EVENTS/PER_YEAR:.1f}yr)")
print("yields = annualised % on perp notional (real capital yield ~/1.5)")
print("=" * 70)
print(f"{'pair':10}{'yrs':>5}{'passive%/y':>12}{'pos-only%/y':>13}{'%pos':>7}")

agg_passive = []
agg_posonly = []
for sym in PAIRS:
    try:
        rates = fetch_funding(sym, EVENTS)
        a = analyse(rates)
        if not a:
            print(f"{sym:10}  not enough data")
            continue
        agg_passive.append(a["passive"])
        agg_posonly.append(a["posonly"])
        flag = " 🟢" if a["passive"] >= 8 else ""
        print(f"{sym:10}{a['years']:>5.1f}{a['passive']:>12.1f}"
              f"{a['posonly']:>13.1f}{a['pct_pos']:>6.0f}%{flag}")
    except Exception as e:  # noqa: BLE001
        print(f"{sym:10}  ERROR {str(e)[:40]}")

print("-" * 70)
if agg_passive:
    mp = sum(agg_passive) / len(agg_passive)
    mo = sum(agg_posonly) / len(agg_posonly)
    print(f"{'AVERAGE':10}{'':>5}{mp:>12.1f}{mo:>13.1f}")
    print("-" * 70)
    print(f"READ: passive ~{mp:.0f}%/yr on notional (~{mp/1.5:.0f}%/yr on capital).")
    print("🟢 = passive >= 8%/yr. pos-only is the active upper bound (needs")
    print("switching + fees). Compare to just holding stablecoins / staking.")
