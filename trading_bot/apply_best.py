"""Show (and optionally apply) the single configuration the grand sweep selected.

    .venv/bin/python apply_best.py            # show current vs target, change nothing
    .venv/bin/python apply_best.py --apply    # write the target settings

THE PICK
    swing = trend, 12h, R:R 1:3
        R/month 2.99   PF 1.23   win 35%   n=652   ~7 targets hit per month
        eras 1.22 / 1.11 / 1.53   (all three clear of 1.0, newest strongest)

Chosen from the full sweep: 4 strategies x 6 timeframes (30m/1h/4h/6h/12h/1d)
x 5 R:R = 120 cells, 38 pairs, fees and slippage modelled, gated on a 3-era
walk-forward. 13 cells survived, every one of them at 12h or 1d -- nothing at
6h or below survived on any strategy.

It is not the largest number in the table. breakout 1d 1:3 shows 3.37 R/month
but its neighbours at 1:2 and 1:2.5 both fail and its newest era is 1.24
against a 1.20 threshold, so it is an isolated cell. trend 12h 1:3 sits on a
run of three passing R:R values and clears every era by a margin.

EVERYTHING ELSE IS STOPPED
    scalping_bot_on = 0 stops new scalping entries. Open positions are still
    managed: engine.py calls position_manager.monitor_all() outside the
    per-slot loop, so existing trades keep their stops and targets.
    The scalping slot's own settings are left parked at emastoch 12h 1:2
    (R/month 1.55, PF 2.18) -- the runner-up, ready if it is ever re-enabled.

    ai_hybrid stays off in both slots. It short-circuits aggregate_signal,
    forces in the reversion signal that lost all 20 cells it was tested in,
    and is the one strategy the sweep never measured.
"""
from __future__ import annotations

import sys

import database as db

TARGET = {
    # --- the pick: swing = trend 12h 1:3 -------------------------------------
    "swing_bot_on": "1",
    "swing_hybrid_on": "0",
    "swing_timeframe": "12h",
    "swing_trend_on": "1",
    "swing_breakout_on": "0",
    "swing_reversion_on": "0",
    "swing_emastoch_on": "0",
    "swing_fixed_rr": "3.0",
    # --- everything else stopped ---------------------------------------------
    "scalping_bot_on": "0",
    "scalping_hybrid_on": "0",
    # parked config, only used if the slot is ever switched back on
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

    open_scalp = [p for p in db.get_open_positions(strategy="scalping")]
    if open_scalp:
        print(f"\n{len(open_scalp)} scalping position(s) still open: "
              f"{', '.join(p['symbol'] for p in open_scalp)}")
        print("These keep their stops and targets -- only new entries stop.")

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
