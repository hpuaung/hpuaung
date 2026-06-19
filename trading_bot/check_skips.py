#!/usr/bin/env python3
"""check_skips.py — why aren't entries firing? Count recent signal outcomes and
skip reasons so we can see which filter is blocking trades.

    .venv/bin/python check_skips.py
"""
import warnings
warnings.filterwarnings("ignore")

from collections import Counter
import database as db

db.init_db()

sigs = db.get_recent_signals(400)
print("=" * 56)
print(f"RECENT SIGNAL OUTCOMES  (last {len(sigs)} scans)")
print("=" * 56)
c = Counter((s["action"] or "?") for s in sigs)
for k, n in c.most_common():
    print(f"  {k:16} {n}")
print("  (BUY/SELL = entered · NONE = no setup · *_SKIP = blocked by a filter)")

ev = db.get_recent_events(200)
print("=" * 56)
print(f"RECENT EVENT KINDS  (last {len(ev)})")
print("=" * 56)
ce = Counter((e["kind"] or "?") for e in ev)
for k, n in ce.most_common():
    print(f"  {k:18} {n}")

print("--- last 12 events ---")
for e in ev[:12]:
    print(" ", (e["timestamp"] or "")[11:19], e["kind"], "-", (e["message"] or "")[:48])

print("=" * 56)
print("FILTER SETTINGS (entry gates)")
print("=" * 56)
for k in ("min_rr_ratio", "min_tp_pct", "win_filter_min", "learn_from_paper",
          "scalping_win_filter", "swing_win_filter",
          "scalping_dir_filter", "scalping_hour_filter", "scalping_session_pair_filter",
          "win_model_samples"):
    print(f"  {k:26} = {db.get_setting(k)}")
