"""Which pairs can a small account actually trade?

    .venv/bin/python account_size_check.py            # $100 at 1% risk
    .venv/bin/python account_size_check.py 100 2.0    # $100 at 2% risk

execution/orders.py blocks an order rather than rounding it up when the sized
quantity falls under the exchange's minQty or minNotional. That protects the
risk model -- a $100 account never silently takes a $500 position -- but it
means some pairs simply cannot be entered at all, so the real entries per
month are lower than the backtest's.

This walks the selected pairs, pulls the live 12h ATR and the exchange
filters, sizes the position exactly the way orders.py does, and reports which
pairs would go through and which would be blocked.
"""
from __future__ import annotations

import sys

import database as db
from utils import binance_client as bc
from utils import indicators

API = "real"          # public endpoints only: klines and exchangeInfo need no key
SL_ATR = 1.5          # emastoch_sl_atr / the trend stop, both 1.5 x ATR


def main() -> int:
    equity = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    risk_pct = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    risk_usd = equity * risk_pct / 100.0

    pairs = [p.strip() for p in db.get_setting("selected_pairs", "").split(",")
             if p.strip()][:40]
    if not pairs:
        print("No pairs selected in settings; nothing to check.")
        return 1

    print(f"equity ${equity:.0f}   risk {risk_pct}%  = ${risk_usd:.2f} per trade   "
          f"stop {SL_ATR} x ATR(12h)\n")
    print(f"{'pair':14}{'price':>12}{'stop%':>8}{'notional':>10}{'minNotl':>9}"
          f"{'verdict':>10}")
    print("-" * 63)

    ok = blocked = 0
    for sym in pairs:
        try:
            df = indicators.compute_indicators(bc.get_ohlcv(sym, "12h", 250, API))
            atr = float(df["atr"].iloc[-1])
            price = float(df["close"].iloc[-1])
            f = bc.get_filters(sym, API)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:14}{'':>12}{'':>8}{'':>10}{'':>9}{'err':>10}  {str(e)[:30]}")
            continue

        stop_dist = SL_ATR * atr
        if stop_dist <= 0 or price <= 0:
            continue
        raw_qty = risk_usd / stop_dist
        qty = bc.truncate_qty(raw_qty, f["stepSize"])
        notional = price * qty
        min_notl = f.get("minNotional", 5.0)

        if qty < f["minQty"]:
            verdict, why = "BLOCKED", f"minQty {f['minQty']}"
            blocked += 1
        elif notional < min_notl:
            verdict, why = "BLOCKED", f"under ${min_notl:g}"
            blocked += 1
        else:
            verdict, why = "ok", ""
            ok += 1
        print(f"{sym:14}{price:>12.4f}{100 * stop_dist / price:>7.1f}%"
              f"{notional:>10.2f}{min_notl:>9.1f}{verdict:>10}  {why}")

    total = ok + blocked
    print("-" * 63)
    print(f"{ok}/{total} pairs tradeable at ${equity:.0f} and {risk_pct}% risk.")
    if total:
        print(f"The backtest assumed all {total}. Expect roughly "
              f"{100 * ok // total}% of its entries per month.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
