"""proven_config.py — the recommended baseline = swing Plan 1 + reversion scalping.

Superseded by swing_plans.py. This thin wrapper keeps the old CLI / callers
working: it pins the baseline to swing Plan 1 (breakout-1d 1:3) plus the
independent reversion scalping slot plus the validated risk limits. To change
the swing engine use the dashboard selector or `python swing_plans.py {1|2|3}`.
"""
import swing_plans

PROVEN = {**swing_plans.GLOBAL,
          **swing_plans.swing_plan_settings(1),
          **swing_plans.SCALPING}


def apply():
    """Apply the baseline (global + swing Plan 1 + reversion scalping)."""
    return swing_plans.apply_all(1)


def drift():
    """Settings whose live value differs from the baseline."""
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
    print(f"applied {n} settings (swing Plan 1 + reversion scalping). restart the "
          "engine or wait for the next scan.")
