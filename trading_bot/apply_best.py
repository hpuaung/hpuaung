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

THE EXITS HAVE TO MATCH TOO
    Picking the right strategy is only half of it. The sweep measured a
    specific way of leaving a trade -- the strategy's own stop, a full exit at
    the target, up to 200 bars of patience -- and the bot was doing something
    different on all three counts, every one of them cutting winners short.
    Those settings are pinned here alongside the strategy choice; see the
    comments in TARGET for what each was doing.

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
    # --- make the live exits match the backtest ------------------------------
    # The sweep measured one thing: enter, stop at the strategy's own stop,
    # exit the whole position at the R:R target, allow up to 200 bars. The bot
    # was doing none of that, and all three differences cut winners short --
    # which is why 33 swing trades produced zero targets and an average win of
    # $0.10 against an average loss of $0.82.
    #
    # 1. auto_pilot forces EVERY auto_* flag on regardless of its own setting
    #    (database.auto_flag), so nothing below holds unless it is off.
    "auto_pilot": "0",
    # 2. atr_sl_enabled replaced each strategy's stop with 1.5 x ATR
    #    (engine.py:288). The sweep used the strategy's own stop -- trend's is
    #    EMA50, normally much wider -- so the live stop was tighter than the
    #    one that was measured, and trades died in a median 1.4 days.
    #    emastoch is unaffected: its own stop already is 1.5 x ATR.
    "atr_sl_enabled": "0",
    # 3. swing positions were force-closed at 7 days (position_manager.py:377,
    #    hard-coded when swing_auto_maxhold is on; the manual default was 2).
    #    The sweep allowed 200 bars = 100 days on 12h, and trend 12h 1:3 has a
    #    median hold of 13.5 days -- so the winners were being guillotined
    #    before they could reach 3R while the losers stopped out normally.
    "swing_auto_maxhold": "0",
    "swing_max_hold_days": "100",
    # 4. partial_tp took 50% off at the target and moved the runner's stop to
    #    near break-even. The sweep exits the whole position at the target.
    "swing_partial_tp": "0",
    "scalping_partial_tp": "0",
    "swing_auto_be": "0",
    "scalping_auto_be": "0",
    "swing_trail_auto": "0",
    "scalping_trail_auto": "0",
    # 5. auto_tf overrides the timeframe setting outright (engine.py
    #    _effective_tfs): with it on, swing trades 1d and scalping trades 5m
    #    whatever swing_timeframe/scalping_timeframe say. Both slots must be on
    #    12h for the picks below to be the thing that actually runs.
    "swing_auto_tf": "0",
    "scalping_auto_tf": "0",
    # 6. The adaptive filters skipped any context under a flat 35-40% win rate.
    #    trend 12h 1:3 is designed to win 35% -- break-even is 25% -- so each
    #    pair would have been switched off as it reached 15 trades, exactly as
    #    the strategy started working. The floor now comes from the slot's R:R
    #    (engine._winrate_floor); this switch turns all four off outright.
    "adaptive_filters_on": "1",
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

    if not apply:
        print(f"\n{len(changes)} setting(s) differ. Re-run with --apply to write them."
              if changes else "\nAlready configured as recommended. Nothing to do.")
        return 0

    for key, cur, want in changes:
        db.save_setting(key, want)
        db.log_event("CONFIG_APPLIED", f"{key}: {cur} -> {want}")
    if not changes:
        print("\nSettings already correct; re-locking so drift detection is armed.")

    # Record what each slot is now set to, so the engine can tell if anything
    # moves it afterwards. A setting that quietly overrode the intended setup
    # used to be invisible for months; from here it is one Telegram message.
    for slot in ("swing", "scalping"):
        cid, desc = db.config_snapshot(slot)
        db.save_setting(f"locked_config_{slot}", cid)
        db.save_setting(f"drift_warned_{slot}", "")
        print(f"  locked {slot}: {cid}  {desc}")

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
