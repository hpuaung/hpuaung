#!/bin/sh
# run_sweep.sh — one-shot backtest sweep: every strategy across scalping AND swing
# timeframes on a deep sample, so we can pick the best strategy+timeframe combo.
# Run on the VPS from trading_bot/:  nohup sh run_sweep.sh > /tmp/sweep.txt 2>&1 &
PY=.venv/bin/python

echo "### SCALP SWEEP 5m/15m/30m (deep sample) ###"
$PY strategy_backtest.py all 5m,15m,30m 6000

echo
echo "### SWING SWEEP 1h/4h/1d (deep sample) ###"
$PY strategy_backtest.py all 1h,4h,1d 3000

echo
echo "### TREND-FOLLOWING (chandelier trail) 4h then 1d ###"
$PY trend_backtest.py all 4h 3000
$PY trend_backtest.py all 1d 3000

echo
echo ALL_DONE
