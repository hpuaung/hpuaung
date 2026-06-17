"""
ai_monitor.py — Autonomous bot monitor + auto-adjuster.

Runs every 4 hours via cron. Analyses trade history, applies safe
automatic adjustments (pair removal, risk reduction), then sends a
Burmese-language Telegram report.

If a Claude API key is stored in settings (claude_api_key), the analysis
is powered by Claude AI. Otherwise it falls back to rule-based logic —
so the monitor works even without an API key.

Setup (run once on VPS):
    (crontab -l; echo "0 */4 * * * cd /root/hpuaung/trading_bot && \
    /root/hpuaung/trading_bot/.venv/bin/python ai_monitor.py >> \
    /tmp/ai_monitor.log 2>&1") | crontab -
"""

import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database as db
from notifications.telegram_bot import send_message


# ---------------------------------------------------------------------------
# Gather stats from the database
# ---------------------------------------------------------------------------
def _gather_stats():
    trades = db.get_trades(paper_mode=0)
    if len(trades) < 10:
        return None

    recent = trades[:50]  # Last 50 real trades

    pair_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "net": 0.0})
    session_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "net": 0.0})

    for t in recent:
        pnl = float(t.get("net_pnl") or 0)
        pair = t["pair"]
        sess = t.get("session") or "off"
        side = t.get("side", "")
        won = pnl > 0

        if won:
            pair_stats[pair]["wins"] += 1
            session_stats[sess]["wins"] += 1
        else:
            pair_stats[pair]["losses"] += 1
            session_stats[sess]["losses"] += 1
        pair_stats[pair]["net"] += pnl
        session_stats[sess]["net"] += pnl
        pair_stats[pair].setdefault("sides", defaultdict(lambda: {"w": 0, "l": 0}))
        pair_stats[pair]["sides"][side]["w" if won else "l"] += 1

    wins = sum(1 for t in recent if float(t.get("net_pnl") or 0) > 0)
    net = sum(float(t.get("net_pnl") or 0) for t in recent)

    consec_loss = 0
    for t in trades:
        if float(t.get("net_pnl") or 0) < 0:
            consec_loss += 1
        else:
            break

    return {
        "total": len(recent),
        "wins": wins,
        "losses": len(recent) - wins,
        "win_rate": wins / max(len(recent), 1) * 100,
        "net_pnl": net,
        "consecutive_losses": consec_loss,
        "pair_stats": dict(pair_stats),
        "session_stats": dict(session_stats),
        "current_pairs": db.get_setting("selected_pairs", ""),
        "risk_pct": float(db.get_setting("scalping_base_risk_pct") or 1.0),
        "win_model_samples": db.get_int("win_model_samples", 0),
        "win_model_acc": db.get_float("win_model_acc", 0.0),
    }


# ---------------------------------------------------------------------------
# Apply safe automatic adjustments
# ---------------------------------------------------------------------------
def _apply_adjustments(remove_pairs=None, new_risk=None):
    actions = []
    current_pairs = [p.strip() for p in
                     db.get_setting("selected_pairs", "").split(",") if p.strip()]

    if remove_pairs:
        new_pairs = [p for p in current_pairs if p not in remove_pairs]
        if new_pairs and set(new_pairs) != set(current_pairs):
            db.save_setting("selected_pairs", ",".join(new_pairs))
            actions.append(f"Pairs ဖယ်: {', '.join(remove_pairs)}")
            db.log_event("AI_MONITOR", f"Auto removed pairs: {remove_pairs}")

    current_risk = float(db.get_setting("scalping_base_risk_pct") or 1.0)
    if new_risk is not None and abs(new_risk - current_risk) >= 0.05:
        safe_risk = max(0.25, min(1.5, new_risk))
        db.save_setting("scalping_base_risk_pct", f"{safe_risk:.2f}")
        actions.append(f"Risk: {current_risk:.2f}% → {safe_risk:.2f}%")
        db.log_event("AI_MONITOR", f"Auto risk adjustment: {current_risk} → {safe_risk}")

    return actions


# ---------------------------------------------------------------------------
# Rule-based analysis (no API needed)
# ---------------------------------------------------------------------------
def _rule_based_analysis(stats):
    remove_pairs = []
    new_risk = None
    lines = ["📊 <b>BOT AUTO REPORT</b> (Rule-based)"]

    wr = stats["win_rate"]
    net = stats["net_pnl"]
    consec = stats["consecutive_losses"]

    # Overall
    wr_emoji = "🟢" if wr >= 50 else ("🟡" if wr >= 40 else "🔴")
    lines.append(f"{wr_emoji} Win Rate: <b>{wr:.0f}%</b> | Net: <b>${net:.2f}</b>")
    lines.append(f"Streak Loss: {consec} | Trades: {stats['total']}")
    lines.append("──────────────")

    # Pair analysis
    lines.append("📌 <b>Pair Analysis:</b>")
    for pair, s in sorted(stats["pair_stats"].items(),
                          key=lambda x: x[1]["net"]):
        total = s["wins"] + s["losses"]
        if total < 5:
            continue
        wr_p = s["wins"] / total * 100
        emoji = "🟢" if wr_p >= 50 else ("🟡" if wr_p >= 35 else "🔴")
        lines.append(f"{emoji} {pair}: {wr_p:.0f}% ({total}trades) ${s['net']:.2f}")
        if total >= 8 and wr_p < 30:
            remove_pairs.append(pair)

    # Session analysis
    lines.append("──────────────")
    lines.append("🌍 <b>Session:</b>")
    for sess, s in sorted(stats["session_stats"].items(),
                          key=lambda x: x[1]["net"]):
        total = s["wins"] + s["losses"]
        if total < 3:
            continue
        wr_s = s["wins"] / total * 100
        emoji = "🟢" if wr_s >= 50 else ("🟡" if wr_s >= 35 else "🔴")
        lines.append(f"{emoji} {sess}: {wr_s:.0f}% ({total}trades) ${s['net']:.2f}")

    # Risk adjustment
    current_risk = stats["risk_pct"]
    if consec >= 4 and current_risk > 0.3:
        new_risk = round(current_risk * 0.7, 2)
        lines.append(f"\n⚡ Risk လျှော့ (Loss {consec}ကြိမ်ဆက်): {current_risk}% → {new_risk}%")
    elif consec == 0 and wr >= 55 and current_risk < 1.0:
        new_risk = round(min(1.0, current_risk + 0.1), 2)
        lines.append(f"\n⚡ Risk ပြန်တိုး (WR {wr:.0f}%): {current_risk}% → {new_risk}%")

    # Win model info
    samples = stats["win_model_samples"]
    acc = stats["win_model_acc"]
    lines.append("──────────────")
    if samples >= 50:
        lines.append(f"🤖 Win Model: {samples} samples | Acc: {acc:.0%}")
    else:
        lines.append(f"🤖 Win Model: {samples}/50 samples (မပြည့်သေးဘူး)")

    # Actions
    actions = _apply_adjustments(remove_pairs, new_risk)
    if actions:
        lines.append("──────────────")
        lines.append("✅ <b>Auto Actions:</b>")
        for a in actions:
            lines.append(f"  • {a}")
    else:
        lines.append("\n✅ Setting အပြောင်းအလဲ မလိုသေးဘူး")

    return "\n".join(lines), actions


# ---------------------------------------------------------------------------
# Claude AI analysis
# ---------------------------------------------------------------------------
def _claude_analysis(stats, api_key):
    try:
        import anthropic
    except ImportError:
        db.log_event("AI_MONITOR", "anthropic package မရှိ — pip install anthropic")
        return None, []

    try:
        client = anthropic.Anthropic(api_key=api_key)

        pair_summary = {}
        for pair, s in stats["pair_stats"].items():
            total = s["wins"] + s["losses"]
            if total >= 3:
                pair_summary[pair] = {
                    "win_rate": f"{s['wins']/total*100:.0f}%",
                    "trades": total,
                    "net": f"${s['net']:.2f}"
                }

        sess_summary = {}
        for sess, s in stats["session_stats"].items():
            total = s["wins"] + s["losses"]
            if total >= 3:
                sess_summary[sess] = {
                    "win_rate": f"{s['wins']/total*100:.0f}%",
                    "trades": total,
                    "net": f"${s['net']:.2f}"
                }

        prompt = f"""You are monitoring a Binance Futures scalping bot. Analyze and respond in JSON only.

Bot stats (last {stats['total']} real trades):
- Win Rate: {stats['win_rate']:.0f}%
- Net PnL: ${stats['net_pnl']:.2f}
- Consecutive Losses Now: {stats['consecutive_losses']}
- Current Risk %: {stats['risk_pct']}
- Win Model: {stats['win_model_samples']} samples, {stats['win_model_acc']:.0%} acc

Pair performance: {json.dumps(pair_summary)}
Session performance: {json.dumps(sess_summary)}

Respond ONLY with this JSON (no markdown):
{{
  "summary_mm": "2 ကြောင်းခြုံ analysis (Burmese)",
  "remove_pairs": ["only pairs with 8+ trades AND win_rate < 30%"],
  "new_risk_pct": null or number between 0.25-1.5,
  "risk_reason_mm": "ဘာကြောင့် risk ပြောင်းရတာလဲ (Burmese)",
  "best_session": "london or ny or asia or off",
  "worst_pair": "XYZUSDT",
  "action_needed": true or false
}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )

        # Claude may wrap JSON in prose or ```json fences — extract the object.
        raw = msg.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        elif "{" in raw and "}" in raw:
            raw = raw[raw.index("{"):raw.rindex("}") + 1]
        result = json.loads(raw)

        # Build report
        lines = ["🤖 <b>BOT AI REPORT</b> (Claude Analysis)"]
        lines.append(result.get("summary_mm", ""))
        lines.append("──────────────")

        wr = stats["win_rate"]
        net = stats["net_pnl"]
        consec = stats["consecutive_losses"]
        wr_emoji = "🟢" if wr >= 50 else ("🟡" if wr >= 40 else "🔴")
        lines.append(f"{wr_emoji} Win Rate: <b>{wr:.0f}%</b> | Net: <b>${net:.2f}</b>")
        lines.append(f"Streak Loss: {consec} | Trades: {stats['total']}")

        if result.get("best_session"):
            lines.append(f"🟢 Best session: {result['best_session']}")
        if result.get("worst_pair"):
            lines.append(f"🔴 Worst pair: {result['worst_pair']}")

        remove = result.get("remove_pairs") or []
        new_risk = result.get("new_risk_pct")
        actions = _apply_adjustments(remove, new_risk)

        if actions:
            lines.append("──────────────")
            lines.append("✅ <b>Auto Actions:</b>")
            for a in actions:
                lines.append(f"  • {a}")
            if result.get("risk_reason_mm"):
                lines.append(f"  ({result['risk_reason_mm']})")
        else:
            lines.append("\n✅ Setting ပြောင်းစရာ မလိုဘူး")

        return "\n".join(lines), actions

    except Exception as e:
        db.log_event("AI_MONITOR_ERROR", f"{type(e).__name__}: {str(e)[:300]}")
        return None, []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run_monitor():
    db.init_db()

    stats = _gather_stats()
    if stats is None:
        db.log_event("AI_MONITOR", "Trade data မလုံလောက်သေးဘူး (< 10 trades)")
        return

    api_key = db.get_setting("claude_api_key", "") or ""

    if api_key.startswith("sk-ant-"):
        report, actions = _claude_analysis(stats, api_key)
        if report is None:
            report, actions = _rule_based_analysis(stats)
        source = "Claude AI"
    else:
        report, actions = _rule_based_analysis(stats)
        source = "Rule-based"

    db.log_event("AI_MONITOR", f"Report sent ({source}) | actions={len(actions)}")
    send_message(report)


if __name__ == "__main__":
    run_monitor()
