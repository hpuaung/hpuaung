#!/usr/bin/env python3
"""run_engine.py — runs the trading engine + background services as a dedicated
process, separate from the Streamlit dashboard.

On a small VPS the dashboard and the engine fought over the Python GIL, so every
UI click showed "running/connecting" while the engine was busy. Running the
engine here (its own systemd service) keeps the dashboard responsive. Both
processes share the SQLite DB, which is the message bus between them.

Setup on the VPS:
  1) run this as a service: futures-engine.service -> .venv/bin/python run_engine.py
  2) set Environment=ENGINE_EXTERNAL=1 on the dashboard (futures-bot) service so
     it serves the UI only and does not also start the engine.
"""
import warnings
warnings.filterwarnings("ignore")

import time

import database as db
from utils import vps_optimizer
from models import train as lgbm
from notifications import telegram_bot as tg
import engine


def main():
    db.init_db()
    db.log_event("ENGINE", "standalone engine process starting")
    # Background services that belong with the engine, not the UI.
    try:
        vps_optimizer.start_vps_monitor()
    except Exception as e:  # noqa: BLE001
        db.log_event("ENGINE_ERROR", f"vps monitor: {e}")
    # Only touch LightGBM when the AI model is actually enabled. It's OFF by
    # config (backtests proved it's noise), and since `import lightgbm` happens
    # lazily inside train.py, skipping this keeps the heavy library out of RAM
    # entirely on the 1GB VPS.
    if db.get_bool("ai_model_on", False):
        try:
            lgbm.ensure_model_on_start()
        except Exception as e:  # noqa: BLE001
            db.log_event("ENGINE_ERROR", f"model load: {e}")
    else:
        db.log_event("ENGINE", "AI model OFF — skipping LightGBM load (lighter)")
    try:
        tg.start_command_bot()
    except Exception as e:  # noqa: BLE001
        db.log_event("ENGINE_ERROR", f"telegram bot: {e}")
    engine.start_engine()
    # Keep the process alive; the engine runs in its background thread.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
