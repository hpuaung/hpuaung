#!/usr/bin/env python3
"""pairs_backtest.py — statistical-arbitrage / pairs-trading backtest. Instead of
predicting a coin's direction, trade the SPREAD (price ratio) between two
correlated coins: when A gets expensive vs B (z-score high) short A + long B and
bet the ratio reverts to its mean. Market-neutral — profits whether the market
goes up or down, as long as the spread mean-reverts.

For each candidate coin-pair it computes the rolling z-score of ratioA/B and
simulates: enter at |z|>=ENTRY, take profit at |z|<=EXIT, stop at |z|>=STOP.
Reports win% / avg win% / avg loss% / PF / total%. Fees on all 4 legs included.

Usage:  .venv/bin/python pairs_backtest.py [interval] [candles]
        .venv/bin/python pairs_backtest.py 1h 4000
"""
import sys
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import binance_client as bc

db.init_db()
_a = sys.argv[1:]
INTERVAL = _a[0] if len(_a) > 0 else "1h"
LIMIT = int(_a[1]) if len(_a) > 1 else 4000

W = 100          # rolling window for the spread mean/std
ENTRY = 2.0      # enter when |z| >= this
EXIT = 0.4       # take profit when |z| <= this (reverted)
STOP = 4.0       # stop when |z| >= this (spread diverged further)
FEE = 0.0004     # taker per leg; a full round trip touches 4 legs
MAXHOLD = 200

# correlated coin pairs (A/B): same-sector / high-beta pairs that tend to co-move
CAND = [
    ("ETHUSDT", "BTCUSDT"), ("BNBUSDT", "BTCUSDT"), ("SOLUSDT", "ETHUSDT"),
    ("LTCUSDT", "BTCUSDT"), ("XRPUSDT", "BTCUSDT"), ("AVAXUSDT", "SOLUSDT"),
    ("LINKUSDT", "ETHUSDT"), ("DOTUSDT", "ETHUSDT"), ("MATICUSDT", "ETHUSDT"),
    ("ADAUSDT", "XRPUSDT"), ("NEARUSDT", "SOLUSDT"), ("ETCUSDT", "ETHUSDT"),
    ("AAVEUSDT", "ETHUSDT"), ("OPUSDT", "ARBUSDT"), ("DOGEUSDT", "SHIBUSDT"),
]


def fetch(sym):
    df = bc.get_ohlcv_deep(sym, INTERVAL, LIMIT, api_mode="real")
    if df is None or len(df) < W + 100:
        return None
    return dict(zip((df["open_time"].astype("int64")//10**6).tolist(),
                    df["close"].astype(float).tolist()))


def simulate(ca, cb):
    # align on common timestamps
    ts = sorted(set(ca) & set(cb))
    if len(ts) < W + 100:
        return None
    ratio = [ca[t]/cb[t] for t in ts if cb[t] > 0]
    n = len(ratio)
    trades = []
    pos = None
    for i in range(W, n):
        win = ratio[i-W:i]
        m = sum(win)/W
        var = sum((x-m)**2 for x in win)/W
        sd = var**0.5
        if sd <= 0:
            continue
        z = (ratio[i]-m)/sd
        if pos:
            held = i - pos["i"]
            done = False
            if abs(z) <= EXIT:                    # reverted -> win
                done = True
            elif (pos["side"] == "short" and z >= STOP) or \
                 (pos["side"] == "long" and z <= -STOP):   # diverged -> loss
                done = True
            elif held >= MAXHOLD:
                done = True
            if done:
                # short-spread profits when ratio falls; long when it rises
                d = -1 if pos["side"] == "short" else 1
                ret = d * (ratio[i] - pos["entry"]) / pos["entry"] - 4*FEE
                trades.append(ret)
                pos = None
        if not pos:
            if z >= ENTRY:
                pos = {"i": i, "side": "short", "entry": ratio[i]}
            elif z <= -ENTRY:
                pos = {"i": i, "side": "long", "entry": ratio[i]}
    return trades


def stats(t):
    if not t:
        return None
    w = [x for x in t if x > 0]
    gW = sum(w); gL = -sum(x for x in t if x <= 0)
    pf = gW/gL if gL > 0 else 99
    aw = (sum(w)/len(w)*100) if w else 0
    al = (sum(x for x in t if x <= 0)/max(len(t)-len(w), 1))*100
    return len(t), 100*len(w)//len(t), aw, al, sum(t)*100, pf


print("="*72)
print(f"PAIRS TRADING (stat-arb) BACKTEST  interval={INTERVAL} candles={LIMIT}")
print(f"z-score spread: enter|z|>={ENTRY} exit|z|<={EXIT} stop|z|>={STOP} win={W}")
print("="*72)
print(f"{'pair A/B':20}{'n':>5}{'win%':>6}{'avgW%':>8}{'avgL%':>8}{'total%':>9}{'PF':>7}")
cache = {}
allt = []
for a, b in CAND:
    try:
        if a not in cache:
            cache[a] = fetch(a)
        if b not in cache:
            cache[b] = fetch(b)
        if cache[a] is None or cache[b] is None:
            print(f"{a[:-4]}/{b[:-4]:14} no data")
            continue
        t = simulate(cache[a], cache[b])
        s = stats(t)
        if not s:
            print(f"{a[:-4]}/{b[:-4]:14} no trades")
            continue
        allt += t
        flag = " 🟢" if (s[4] > 0 and s[5] > 1.3) else ""
        print(f"{a[:-4]+'/'+b[:-4]:20}{s[0]:>5}{s[1]:>6}{s[2]:>+8.2f}"
              f"{s[3]:>+8.2f}{s[4]:>+9.1f}{s[5]:>7.2f}{flag}")
    except Exception as e:  # noqa: BLE001
        print(f"{a}/{b}  ERROR {str(e)[:40]}")

print("-"*72)
s = stats(allt)
if s:
    print(f"{'ALL PAIRS':20}{s[0]:>5}{s[1]:>6}{s[2]:>+8.2f}{s[3]:>+8.2f}"
          f"{s[4]:>+9.1f}{s[5]:>7.2f}")
print("🟢 = total>0 AND PF>1.3. Pairs trading is market-neutral: the edge is the")
print("spread reverting, not price direction. If PF>1.3 broadly -> worth building.")
