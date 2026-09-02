"""Show (and optionally apply) the single configuration the grand sweep selected.

    .venv/bin/python apply_best.py            # show current vs target, change nothing
    .venv/bin/python apply_best.py --apply    # write the target settings

THE PICKS
    swing    = trend, 12h, R:R 1:3
        R/month 2.99   PF 1.23   win 35%   n=652   ~7 targets hit per month
        eras 1.22 / 1.11 / 1.53   (all three clear of 1.0, newest strongest)
    scalping = emastoch, 12h, R:R 1:2
        R/month 1.55   PF 2.18   win 52%   n=91    ~1.4 targets per month
        eras 1.49 / 3.39 / 2.30 -- the steadiest region in the whole grid,
        7 adjacent R:R cells passing across 12h and 1d

    Together: ~22 entries and ~8 targets a month, 37% wins, 4.54 R/month.
    Different entry logic, so they do not lose together, and engine.py stops
    either slot stacking onto a symbol the other already holds.

Chosen from the full sweep: 4 strategies x 6 timeframes (30m/1h/4h/6h/12h/1d)
x 5 R:R = 120 cells, 38 pairs, fees and slippage modelled, gated on a 3-era
walk-forward. 13 cells survived, every one of them at 12h or 1d -- nothing at
6h or below survived on any strategy.

It is not the largest number in the table. breakout 1d 1:3 shows 3.37 R/month
but its neighbours at 1:2 and 1:2.5 both fail and its newest era is 1.24
against a 1.20 threshold, so it is an isolated cell. trend 12h 1:3 sits on a
run of three passing R:R values and clears every era by a margin.

EVERYTHING ELSE IS OFF
    breakout and reversion are off in both slots. reversion lost all 20
    timeframe/R:R cells it was tested in -- at 12h 1:1 its profit factor was
    0.33.

    ai_hybrid stays off too. It short-circuits aggregate_signal, forces in
    that same reversion signal regardless of the toggles, never reaches
    emastoch at all, and is the one strategy the sweep never measured.
"""
from __future__ import annotations

import sys

import database as db

TARGET = {
    # --- fixed risk ----------------------------------------------------------
    # auto_risk ignores base_risk_pct entirely (orders.py:24) and sizes from
    # risk_guard.recommended_risk(), which reads win rate alone and knows
    # nothing about R:R: under 40% wins it returns 0.5. trend 12h 1:3 wins 35%
    # by design -- break-even for 1:3 is 25% -- so auto mode permanently halves
    # the size of the very strategy the sweep selected, and the win rate never
    # climbs out of it because it is not supposed to. Fix the risk instead.
    # 5 x 1.0 = 5, inside the lev_risk_hard_cap_pct of 10, so nothing blocks.
    "swing_auto_risk": "0",
    "scalping_auto_risk": "0",
    "swing_base_risk_pct": "1.0",
    "scalping_base_risk_pct": "1.0",
    "swing_base_leverage": "5",
    "scalping_base_leverage": "5",
    # --- the pick: swing = trend 12h 1:3 -------------------------------------
    "swing_bot_on": "1",
    "swing_hybrid_on": "0",
    "swing_timeframe": "12h",
    "swing_trend_on": "1",
    "swing_breakout_on": "0",
    "swing_reversion_on": "0",
    "swing_emastoch_on": "0",
    "swing_fixed_rr": "3.0",
    # --- second slot: emastoch 12h 1:2 ---------------------------------------
    "scalping_bot_on": "1",
    "scalping_hybrid_on": "0",
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

    _projection()

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


# Straight from the sweep: (label, entries per 30d, win rate, R per month).
SLOTS = [
    ("swing", "trend 12h 1:3", 19.6, 0.35, 2.99),
    ("scalping", "emastoch 12h 1:2", 2.7, 0.52, 1.55),
]


def _projection() -> None:
    """Print what the two picked cells imply per month, at the configured risk."""
    print(f"\n{'slot':10}{'config':22}{'entry/mo':>10}{'TP/mo':>8}{'SL/mo':>8}"
          f"{'win%':>7}{'R/mo':>7}")
    print("-" * 72)
    tot_n = tot_tp = tot_r = 0.0
    for slot, label, per30, win, rmonth in SLOTS:
        tp = per30 * win
        print(f"{slot:10}{label:22}{per30:>10.1f}{tp:>8.1f}{per30 - tp:>8.1f}"
              f"{100 * win:>7.0f}{rmonth:>7.2f}")
        tot_n += per30
        tot_tp += tp
        tot_r += rmonth
    print("-" * 72)
    print(f"{'TOTAL':32}{tot_n:>10.1f}{tot_tp:>8.1f}{tot_n - tot_tp:>8.1f}"
          f"{100 * tot_tp / tot_n:>7.0f}{tot_r:>7.2f}")

    print("\nR is one unit of risk, so the percentages depend on risk per trade:")
    for slot, *_ in SLOTS:
        auto = db.get_bool(f"{slot}_auto_risk", False)
        base = db.get_float(f"{slot}_base_risk_pct", 0.0)
        note = "  <-- auto risk still ON: real size comes from win rate, not this" if auto else ""
        print(f"  {slot}_base_risk_pct = {base}{note}")
    print(f"\n{'risk/trade':>12}{'month':>9}{'year (compounded)':>20}")
    for risk in (0.5, 1.0, 1.5, 2.0):
        m = tot_r * risk / 100.0
        print(f"{risk:>11.1f}%{100 * m:>8.1f}%{100 * ((1 + m) ** 12 - 1):>19.0f}%")
    print("\nThese are backtest expectations, not a forecast. A 37% win rate\n"
          "means a run of ~12 straight losses somewhere in a year is normal,\n"
          "and roughly 1 month in 4 finishes negative.")


def _same_number(a: str, b: str) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
