#!/bin/sh
# run_mtf_sweep.sh — test the user's exact multi-timeframe structure on all 10
# pairs, per-pair, BEFORE deploying: does the confirm+trend filter give an edge?
#   SWING : entry 1h  / confirm 4h  / trend 1d
#   SCALP : entry 3m  / confirm 15m / trend 1h
# Run on the VPS from trading_bot/:  nohup sh run_mtf_sweep.sh > /tmp/mtf.txt 2>&1 &
PY=.venv/bin/python
P=BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,LTCUSDT

echo "### MTF SWING  entry 1h / confirm 4h / trend 1d  (breakout+trend) ###"
$PY mtf_backtest.py $P 1h 4h 1d 6000 detail

echo
echo "### MTF SCALP  entry 3m / confirm 15m / trend 1h ###"
$PY mtf_backtest.py $P 3m 15m 1h 6000 detail

echo
echo ALL_DONE
