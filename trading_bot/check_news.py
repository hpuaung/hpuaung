#!/usr/bin/env python3
"""check_news.py — quick health check for the GNews + HuggingFace integration.

Run on the VPS:  cd /root/hpuaung/trading_bot && .venv/bin/python check_news.py
"""
import warnings
warnings.filterwarnings("ignore")

import database as db
from utils import news

print("GNews token set:", bool(db.get_setting("gnews_api")))
print("HF token set   :", bool(db.get_setting("hf_token")))

print("--- GNews fetch (BTC) ---")
h = news.get_cached_gnews("BTCUSDT", cache_min=0)
print("headlines:", (h[:200] if h else "(EMPTY - no token / quota / error)"))

print("--- HuggingFace FinBERT ---")
test = h or "Bitcoin surges to new highs on strong institutional demand"
print("sentiment score:", news.call_huggingface_api(test), "(range -1..+1)")

print("--- combined ---")
print("BTC sentiment:", news.get_sentiment("BTCUSDT", cache_min=0))
print("GNews used today:", news.gnews_requests_today(), "/100")
print("HF used month   :", news.hf_requests_month(), "/30000")

print("--- recent news errors ---")
found = False
for e in db.get_recent_events(40):
    if e["kind"] in ("GNEWS_ERROR", "HF_ERROR"):
        found = True
        print(" ", e["timestamp"][11:], e["kind"], "-", (e["message"] or "")[:80])
if not found:
    print("  (none)")
