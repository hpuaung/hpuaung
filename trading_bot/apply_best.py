"""Show (and optionally apply) the configuration the grand sweep selected.

    .venv/bin/python apply_best.py            # show current vs target, change nothing
    .venv/bin/python apply_best.py --apply    # write the target settings

Why these values -- from grand_sweep across 30m/1h/4h/6h/12h/1d x R:R 1..3 on
38 pairs, fees and slippage included, gated on a 3-era walk-forward:

  swing    trend    12h 1:3   R/month 2.99  PF 1.23  n=652  eras 1.22/1.11/1.53
  scalping emastoch 12h 1:2   R/month 1.55  PF 2.18  n=91   eras 1.49/3.39/2.30

Nothing at 6h or below survived the walk-forward on any strategy, so the
scalping slot moves to 12h. reversion lost in all 20 timeframe/R:R cells it
was tested in and is switched off in both slots.
"""
from __future__ import annotations

import sys

import database as db

TARGET = {
    # ai_hybrid short-circuits aggregate_signal (engine.py:80): when it is on it
    # runs trend+reversion+breakout itself, ignores every per-strategy toggle,
    # and never reaches emastoch at all. It defaults to ON, it forces in the
    # reversion signal that lost all 20 cells of the sweep, and it is the one
    # strategy the sweep never measured. Off in both slots until it is tested.
    "swing_hybrid_on": "0",
    "scalping_hybrid_on": "0",
    # swing slot -- already correct, listed so the whole picture is visible
    "swing_timeframe": "12h",
    "swing_trend_on": "1",
    "swing_breakout_on": "0",
    "swing_reversion_on": "0",
    "swing_emastoch_on": "0",
    "swing_fixed_rr": "3.0",
    # scalping slot -- 6h -> 12h, native 3R -> 2R
    "scalping_timeframe": "12h",
    "scalping_trend_on": "0",
    "scalping_breakout_on": "0",
    "scalping_reversion_on": "0",
    "scalping_emastoch_on": "1",
    "scalping_fixed_rr": "2.0",
}


def main() -> int:
    apply = "--apply" in sys.argv
    changes = []
    print(f"{'setting':26}{'current':>12}{'target':>12}")
    print("-" * 50)
    for key, want in TARGET.items():
        cur = str(db.get_setting(key, "(unset)"))
        same = cur == want or _same_number(cur, want)
        print(f"{key:26}{cur:>12}{want:>12}{'' if same else '   <-- change'}")
        if not same:
            changes.append((key, cur, want))

    if not changes:
        print("\nAlready configured as recommended. Nothing to do.")
        return 0

    if not apply:
        print(f"\n{len(changes)} setting(s) differ. Re-run with --apply to write them.")
        return 0

    for key, cur, want in changes:
        db.save_setting(key, want)
        db.log_event("CONFIG_APPLIED", f"{key}: {cur} -> {want}")
    print(f"\nWrote {len(changes)} setting(s). Restart the engine to pick them up:")
    print("  systemctl restart futures-engine")
    return 0


def _same_number(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
