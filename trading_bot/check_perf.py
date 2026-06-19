#!/usr/bin/env python3
"""check_perf.py — performance + AI-model diagnostic on the live trade history.

Run on the VPS in trading_bot/ with the venv:
    .venv/bin/python check_perf.py            # all closed trades
    .venv/bin/python check_perf.py 7          # only the last 7 days

Reports overall and per-strategy win rate / avg win / avg loss / profit factor,
whether the LGBM direction model's confidence actually predicts wins, and the
current state of the risk and model settings.
"""
import sys
import warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone, timedelta
import database as db

db.init_db()

days = None
if len(sys.argv) >= 2:
    try:
        days = int(sys.argv[1])
    except ValueError:
        days = None


def _ts(t):
    return t.get("exit_timestamp") or t.get("timestamp") or ""


trades = [t for t in db.get_trades() if (t.get("status") or "closed") == "closed"]
if days is not None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    trades = [t for t in trades if _ts(t) >= cutoff]


def stats(rows, label):
    if not rows:
        print(f"{label:10} : no trades")
        return
    net = [float(t.get("net_pnl") or 0) for t in rows]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    wr = 100 * len(wins) / len(rows)
    print(f"{label:10} : n={len(rows):<4} net={sum(net):+7.2f}  win%={wr:4.0f}  "
          f"avgW={aw:+.3f}  avgL={al:+.3f}  PF={pf:.2f}")


print("=" * 64)
print(f"PERFORMANCE  ({'last %d days' % days if days else 'all closed trades'})")
print("=" * 64)
stats(trades, "OVERALL")
for s in ("scalping", "swing"):
    stats([t for t in trades if t.get("strategy") == s], s)

# Close-reason breakdown.
print("--- by close reason ---")
reasons = {}
for t in trades:
    r = t.get("close_reason") or "?"
    reasons.setdefault(r, []).append(float(t.get("net_pnl") or 0))
for r, v in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
    w = len([x for x in v if x > 0])
    print(f"  {r:8} n={len(v):<4} net={sum(v):+7.2f}  win%={100*w/len(v):4.0f}")

# Does LGBM confidence predict wins?
print("=" * 64)
print("LGBM DIRECTION MODEL — does confidence predict wins?")
print("=" * 64)


def bucket(rows, label):
    if not rows:
        print(f"  {label:18}: (none)")
        return
    w = len([t for t in rows if float(t.get("net_pnl") or 0) > 0])
    print(f"  {label:18}: win%={100*w/len(rows):4.0f}  (n={len(rows)})")


have = [t for t in trades if float(t.get("lgbm_score") or 0) > 0]
none = [t for t in trades if float(t.get("lgbm_score") or 0) == 0]
bucket([t for t in have if float(t["lgbm_score"]) >= 0.7], "lgbm >= 0.70")
bucket([t for t in have if 0.5 <= float(t["lgbm_score"]) < 0.7], "lgbm 0.50-0.70")
bucket([t for t in have if float(t["lgbm_score"]) < 0.5], "lgbm < 0.50")
bucket(none, "lgbm = 0 (no model)")
print("  NOTE: if higher buckets do NOT have higher win%, the model is not")
print("        adding predictive value and should be de-emphasised.")

# Current settings.
print("=" * 64)
print("CURRENT SETTINGS")
print("=" * 64)
for k in ("min_rr_ratio", "min_tp_pct", "atr_sl_enabled", "atr_sl_mult",
          "swing_min_lgbm", "sl_cooldown_on", "learn_from_paper",
          "lgbm_accuracy", "lgbm_last_trained",
          "win_model_samples", "win_model_acc"):
    print(f"  {k:20} = {db.get_setting(k)}")
