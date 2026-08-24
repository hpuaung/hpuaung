#!/usr/bin/env python3
"""forward_test.py — the ONE readout for the clean forward test.

Measures ONLY trades opened after the `forward_test_start` marker, so the
contaminated pre-test history (manual closes, testnet-era prices, bug-era
phantoms) is excluded WITHOUT deleting anything.

WHAT THIS ACTUALLY TESTS — read this, it matters:
  10-15 trades can NOT re-validate a strategy edge. That was already done on
  years of mainnet history (backtest + 3-era walk-forward: trend-12h PF~1.54,
  trend-6h PF~1.36). Statistically, PF over 15 trades is almost noise.

  What 10-15 trades CAN prove is EXECUTION INTEGRITY: that the machine takes the
  validated setup and realises it faithfully — entry at a live price, a win that
  pays ~3R, a loss that costs ~1R, no phantom instant-closes, no re-entry
  spirals. That is the actual open question after two months of bug fixing, and
  that is what this scores.

  So: EXECUTION verdict is the gate. PF is shown as context only, with its
  honest confidence caveat.

  cd /root/hpuaung/trading_bot && .venv/bin/python forward_test.py
  .venv/bin/python forward_test.py start     # (re)mark the test start = now
"""
import sys
import warnings
warnings.filterwarnings("ignore")

from collections import defaultdict
from datetime import datetime, timezone

import database as db

db.init_db()

# --- start marker -----------------------------------------------------------
if len(sys.argv) > 1 and sys.argv[1] == "start":
    now = db.utcnow_str()
    db.save_setting("forward_test_start", now)
    print(f"✅ Forward test start marked: {now} (UTC)")
    print("Only trades opened from now on are measured. Nothing was deleted.")
    raise SystemExit(0)

START = db.get_setting("forward_test_start", "")
if not START:
    print("⚠️  No forward-test start marker set.")
    print("    Run:  .venv/bin/python forward_test.py start")
    raise SystemExit(1)


def num(x):
    try:
        return float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0


def hold_hours(t):
    """Parse the stored hold string ('1.5h' / '0.0h' / '2d 3h') to hours."""
    s = str(t.get("hold_duration") or "").strip()
    if not s:
        return None
    try:
        total = 0.0
        for part in s.split():
            if part.endswith("d"):
                total += float(part[:-1]) * 24
            elif part.endswith("h"):
                total += float(part[:-1])
            elif part.endswith("m"):
                total += float(part[:-1]) / 60.0
        return total
    except Exception:  # noqa: BLE001
        return None


def r_multiple(t):
    """Realised R = (exit-entry)*dir / |entry-sl|. None when SL wasn't recorded
    (trades closed before the sl_price column existed)."""
    e, x, sl = num(t.get("entry_price")), num(t.get("exit_price")), num(t.get("sl_price"))
    if e <= 0 or x <= 0 or sl <= 0:
        return None
    risk = abs(e - sl)
    if risk <= 0:
        return None
    d = 1 if str(t.get("side")).upper() == "BUY" else -1
    return (x - e) * d / risk


# --- load the test window ---------------------------------------------------
allt = db.get_trades()
T = [t for t in allt
     if str(t.get("entry_timestamp") or t.get("timestamp") or "") >= START
     and (t.get("status") or "closed") == "closed"]
open_now = db.get_open_positions()

print("=" * 74)
print("CLEAN FORWARD TEST")
print("=" * 74)
print(f"start marker : {START} (UTC)")
print(f"now          : {db.utcnow_str()} (UTC)")
print(f"closed trades in window : {len(T)}   (excluded as pre-test: {len(allt)-len(T)})")
print(f"currently open          : {len(open_now)}")

# --- config check: is the bot running the VALIDATED setup? ------------------
print("\n" + "-" * 74)
print("CONFIG (must match the validated edge)")
print("-" * 74)
cfg_ok = True
# Each slot must run EXACTLY ONE validated strategy on its validated timeframe:
#   swing    = trend on 12h        (PF 1.54, n=371)
#   scalping = EMA+Stoch on 6h     (PF 1.74, n=203 — the user's own, newest era
#                                   strongest, parity-checked against the backtest)
EXPECT = {
    "swing":    {"tf": "12h", "key": "trend_on",    "name": "trend",     "rr": 3.0},
    "scalping": {"tf": "6h",  "key": "emastoch_on", "name": "EMA+Stoch", "rr": 3.0},
}
ALL_STRATS = ("trend_on", "breakout_on", "reversion_on", "emastoch_on", "hybrid_on")
for slot, want in EXPECT.items():
    on = db.get_bool(f"{slot}_bot_on", False)
    mode = db.get_setting(f"{slot}_mode", "paper")
    tf = db.get_setting(f"{slot}_timeframe", "?")
    rr = db.get_float(f"{slot}_fixed_rr", 0.0)
    active = [k for k in ALL_STRATS if db.get_bool(f"{slot}_{k}", False)]
    good = (on and mode == "paper" and tf == want["tf"]
            and active == [want["key"]] and abs(rr - want["rr"]) < 0.01)
    cfg_ok = cfg_ok and good
    shown = ",".join(a.replace("_on", "") for a in active) or "none"
    print(f"  {slot:9} on={on!s:5} mode={mode:5} tf={tf:4} strategy={shown:12} "
          f"rr={rr:g}  " + ("✅" if good else
                            f"❌ EXPECTED {want['name']} on {want['tf']} rr=3 paper"))
print(f"  {'risk':9} max_concurrent={db.get_int('max_concurrent_trades',5)} "
      f"swing_corr={db.get_bool('swing_corr_filter',False)}/{db.get_int('swing_max_corr_trades',3)} "
      f"scalp_corr={db.get_bool('scalping_corr_filter',False)}/{db.get_int('scalping_max_corr_trades',2)}")

if not T:
    print("\n⏳ No closed trades in the test window yet. On 12h/6h timeframes the")
    print("   first entries can take a day or more. Leave the bot alone and re-run.")
    raise SystemExit(0)

# --- EXECUTION INTEGRITY (the real verdict) ---------------------------------
print("\n" + "-" * 74)
print("EXECUTION INTEGRITY  ← the actual thing being tested")
print("-" * 74)

problems = []

# 1. Phantom instant-closes: a TP/SL hit within minutes of entry is the old bug.
phantoms = [t for t in T
            if (hold_hours(t) is not None and hold_hours(t) < 0.1
                and str(t.get("close_reason")) in ("TP", "SL", "TP1", "TP2", "TP3"))]
print(f"1. phantom instant-closes (<0.1h to TP/SL) : {len(phantoms)}"
      + ("  ✅ none" if not phantoms else "  ❌ BUG STILL PRESENT"))
if phantoms:
    problems.append(f"{len(phantoms)} phantom instant-close(s) — entry price is still stale")
    for t in phantoms[:5]:
        print(f"     {t.get('pair')} {t.get('side')} {t.get('close_reason')} "
              f"hold={t.get('hold_duration')}")

# 2. Re-entry spiral: same pair+strategy closing many times in the window.
by_pair = defaultdict(int)
for t in T:
    by_pair[(t.get("pair"), t.get("strategy"))] += 1
spirals = {k: v for k, v in by_pair.items() if v >= 5}
print(f"2. re-entry spirals (same pair 5+ closes)  : {len(spirals)}"
      + ("  ✅ none" if not spirals else "  ❌ COOLDOWN NOT HOLDING"))
if spirals:
    problems.append(f"re-entry spiral on {list(spirals)[:3]} — cooldown not holding")

# 3. R-multiple fidelity: does a win pay ~3R and a loss cost ~1R?
rs = [(t, r_multiple(t)) for t in T]
rs = [(t, r) for t, r in rs if r is not None]
tp_rs = [r for t, r in rs if str(t.get("close_reason")).upper().startswith("TP")]
sl_rs = [r for t, r in rs if str(t.get("close_reason")).upper() in ("SL", "TRAIL", "MAXDAYS")]
no_r = len(T) - len(rs)
print(f"3. R-multiple fidelity (target: TP≈+3R, SL≈-1R)")
if no_r:
    print(f"     {no_r} trade(s) predate SL recording — no R available (expected for old rows)")
if tp_rs:
    avg_tp = sum(tp_rs) / len(tp_rs)
    ok_tp = 2.4 <= avg_tp <= 3.6
    print(f"     TP closes n={len(tp_rs):<3} avg R = {avg_tp:+.2f}  "
          + ("✅ 1:3 is being realised" if ok_tp else "❌ NOT paying 3R"))
    if not ok_tp:
        problems.append(f"TP closes average {avg_tp:+.2f}R, expected ~+3R — the 1:3 is not being realised")
else:
    print("     TP closes n=0   (no target hit yet — need more time)")
if sl_rs:
    avg_sl = sum(sl_rs) / len(sl_rs)
    ok_sl = -1.6 <= avg_sl <= -0.6
    print(f"     SL closes n={len(sl_rs):<3} avg R = {avg_sl:+.2f}  "
          + ("✅ risk is 1R as sized" if ok_sl else "❌ losing MORE than 1R per stop"))
    if not ok_sl:
        problems.append(f"SL closes average {avg_sl:+.2f}R, expected ~-1R — risk sizing/stop is off")
else:
    print("     SL closes n=0")

# 4. Manual contamination: user interference invalidates the measurement.
manual = [t for t in T if str(t.get("close_reason")) in ("Manual", "manual")]
pct_manual = 100.0 * len(manual) / len(T)
print(f"4. manual closes (must stay ~0)            : {len(manual)} ({pct_manual:.0f}%)"
      + ("  ✅ untouched" if not manual else "  ⚠️ YOU are closing trades — invalidates the test"))
if manual:
    problems.append(f"{len(manual)} manual close(s) — let trades reach TP/SL or the R:R can't show")

# --- PF / expectancy (context only, honest about confidence) ----------------
print("\n" + "-" * 74)
print("PROFITABILITY (context only — NOT statistically meaningful at this size)")
print("-" * 74)
nets = [num(t.get("net_pnl")) for t in T]
wins = [x for x in nets if x > 0]
losses = [x for x in nets if x <= 0]
gW, gL = sum(wins), -sum(losses)
pf = gW / gL if gL > 0 else 99.0
winpct = 100.0 * len(wins) / len(T)
print(f"  n={len(T)}  net=${sum(nets):+.2f}  win%={winpct:.0f}  PF={pf:.2f}")
if wins and losses:
    print(f"  avg win=${sum(wins)/len(wins):+.2f}   avg loss=${sum(losses)/len(losses):+.2f}   "
          f"realised R:R = {(sum(wins)/len(wins))/abs(sum(losses)/len(losses)):.2f} : 1")
print(f"  break-even win% at 1:3 = 25%   ·   backtest expectation PF ≈ 1.36-1.54")
if len(T) < 30:
    print(f"  ⚠️  {len(T)} trades is FAR too few to judge PF — at this size a good")
    print("     strategy shows PF 0.5 or 3.0 purely by luck. Judge EXECUTION above,")
    print("     not this number. Edge validation already came from the backtests.")

by_reason = defaultdict(list)
for t in T:
    by_reason[str(t.get("close_reason") or "?")].append(num(t.get("net_pnl")))
print("\n  by close reason:")
for k, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
    print(f"    {k:12} n={len(v):<3} net=${sum(v):+7.2f}")

# The two slots run DIFFERENT validated strategies, so they have to be scored
# separately — an average across both hides which one is actually working.
print("\n  by engine (swing = trend-12h · scalping = EMA+Stoch-6h):")
for slot in ("swing", "scalping"):
    rows = [t for t in T if str(t.get("strategy")) == slot]
    if not rows:
        print(f"    {slot:9} no closed trades yet")
        continue
    nets = [num(t.get("net_pnl")) for t in rows]
    w = [x for x in nets if x > 0]
    gl = -sum(x for x in nets if x <= 0)
    p = (sum(w) / gl) if gl > 0 else 99.0
    rl = [r_multiple(t) for t in rows]
    rl = [r for r in rl if r is not None]
    ravg = f"{sum(rl)/len(rl):+.2f}R" if rl else "n/a"
    print(f"    {slot:9} n={len(rows):<3} net=${sum(nets):+7.2f} "
          f"win%={100*len(w)//len(rows):<3} PF={p:.2f}  avgR={ravg}")

# --- VERDICT ----------------------------------------------------------------
print("\n" + "=" * 74)
print("VERDICT")
print("=" * 74)
resolved = len(tp_rs) + len(sl_rs)
if not cfg_ok:
    print("❌ CONFIG WRONG — the bot is not running the validated setup (see above).")
    print("   Fix the config first; measurements before that are meaningless.")
elif problems:
    print("❌ EXECUTION PROBLEMS FOUND:")
    for p in problems:
        print(f"   • {p}")
    print("\n   Do NOT go to real money. Send this output back and I'll fix these.")
elif resolved < 10:
    print(f"⏳ PENDING — {resolved} trade(s) have resolved at TP/SL "
          f"(need ~10 to judge execution).")
    print("   Everything checked so far looks correct. Leave the bot alone and re-run")
    print("   in a day or two. Do NOT press Stop-All / Emergency Close.")
elif not tp_rs:
    # HARD GATE. The stop side proving out says nothing about the target side.
    # Without a single TP close, "wins pay ~3R" has never been observed, and a
    # 1:3 system lives entirely on those wins — so this can NEVER be reported as
    # confirmed. (An earlier version did exactly that after 15 straight losses.)
    print(f"❌ NOT CONFIRMED — {resolved} trades resolved and NOT ONE reached target.")
    print(f"   Losses behave correctly (avg {sum(sl_rs)/len(sl_rs):+.2f}R, as sized), so the")
    print("   machinery is sound. But the +3R side has never happened, so the half")
    print("   of the R:R that MAKES the money is still completely untested.")
    print(f"\n   Win rate so far: 0 of {len(T)}. A 1:3 system needs ~25% wins just to")
    print("   break even. Zero is far outside that — either the market period was")
    print("   hostile to this setup, or the edge is not there in the current regime.")
    print("\n   Do NOT go to real money on this.")
else:
    print("✅ EXECUTION CONFIRMED — entries are live-priced, wins pay ~3R, losses")
    print("   cost ~1R, no phantoms, no spirals, no interference.")
    print("   The machine faithfully executes the validated edge.")
    print(f"\n   Context: PF {pf:.2f} over {len(T)} trades (backtest expects ~1.4).")
    print(f"   Verified on {len(tp_rs)} winning and {len(sl_rs)} losing closes.")
    print("   Next step: real money at the SMALLEST size you can trade, and keep")
    print("   measuring. Expect a thin, slow edge — not fast gains.")
print("=" * 74)
