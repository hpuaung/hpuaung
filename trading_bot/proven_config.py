"""proven_config.py — the recommended baseline = swing Plan 1 (Balanced).

Superseded by swing_plans.py, which defines the three dashboard-selectable
plans. This thin wrapper keeps the old CLI / any callers working and pins the
"proven" baseline to Plan 1 (breakout-1d 1:3 + trend-12h 1:3 + validated risk
limits) — all walk-forward robust. To pick a different plan use the dashboard
selector or `python swing_plans.py {1|2|3}`.
"""
import swing_plans

PROVEN = swing_plans.plan_settings(1)


def apply():
    """Apply the Plan 1 baseline. Returns the number of settings written."""
    swing_plans.apply_plan(1)
    return len(PROVEN)


def drift():
    """Settings whose live value differs from the Plan 1 baseline."""
    import database as db
    out = []
    for k, v in PROVEN.items():
        if str(db.get_setting(k, "")) != v:
            out.append((k, db.get_setting(k, ""), v))
    return out


if __name__ == "__main__":
    import database as db
    db.init_db()
    n = apply()
    print(f"applied {n} settings (swing Plan 1 — Balanced). restart the engine "
          "or wait for the next scan.")
