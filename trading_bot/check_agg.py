#!/usr/bin/env python3
"""check_agg.py — show strategy toggles + the ENGINE's actual aggregate result."""
import warnings
warnings.filterwarnings("ignore")

import database as db
import engine

print("=== strategy toggles ===")
for strat in ("scalping", "swing"):
    print(f"{strat}: trend={db.get_bool(f'{strat}_trend_on')} "
          f"reversion={db.get_bool(f'{strat}_reversion_on')} "
          f"breakout={db.get_bool(f'{strat}_breakout_on')} "
          f"hybrid={db.get_bool(f'{strat}_hybrid_on')} "
          f"mtf={db.get_bool(f'{strat}_mtf_filter')} "
          f"bot_on={db.get_bool(f'{strat}_bot_on')}")

print("=== ENGINE aggregate_signal (the real path) ===")
pairs = [p.strip() for p in db.get_setting("selected_pairs", "").split(",") if p.strip()]
for sym in pairs:
    for strat in ("scalping", "swing"):
        try:
            e, cf, tr = engine._gather_indicator_frames(sym, strat, "test")
            sig, trig, score = engine.aggregate_signal(sym, strat, e, cf, tr, 0, 0, 0)
            print(f"  {sym:9} {strat:8} AGG={sig['signal']:4} trig='{trig}' score={round(score,2)}")
        except Exception as ex:  # noqa: BLE001
            print(f"  {sym:9} {strat:8} ERR {str(ex)[:40]}")
