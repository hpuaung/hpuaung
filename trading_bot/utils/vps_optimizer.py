"""
utils/vps_optimizer.py — RAM monitor + garbage collection for a 1GB Ubuntu VPS.

Runs in its own daemon thread (Thread 3). Every 60 seconds, if auto-clean is
enabled and RAM usage is above the configured threshold, it forces a garbage
collection and clears the in-memory caches.
"""

import gc
import time
import threading

import psutil

import database as db

# These caches are registered by the modules that own them so we can flush
# them under memory pressure without importing those modules here (avoids
# circular imports).
_registered_caches = []
_lock = threading.Lock()


def register_cache(cache_dict):
    """Register a dict-like cache to be cleared when RAM is high."""
    with _lock:
        if cache_dict not in _registered_caches:
            _registered_caches.append(cache_dict)


def current_ram_pct():
    return psutil.virtual_memory().percent


def force_clean(reason="manual"):
    """Run GC and flush all registered caches. Returns RAM% after cleaning."""
    gc.collect()
    gc.collect(2)
    with _lock:
        for cache in _registered_caches:
            try:
                cache.clear()
            except Exception:  # noqa: BLE001
                pass
    ram_after = current_ram_pct()
    db.save_setting("vps_last_clean", db.utcnow_str())
    db.log_event("VPS_CLEAN", f"reason={reason} ram_after={ram_after:.1f}%")
    return ram_after


def vps_monitor_loop(stop_event=None):
    """Long-running loop. Pass a threading.Event to allow clean shutdown."""
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            if db.get_bool("vps_auto_clean_on", True):
                ram_pct = current_ram_pct()
                threshold = db.get_float("vps_ram_threshold_pct", 80.0)
                if ram_pct > threshold:
                    force_clean(reason=f"ram {ram_pct:.1f}% > {threshold:.0f}%")
        except Exception as e:  # noqa: BLE001 - monitor must never die
            db.log_event("VPS_ERROR", str(e))
        time.sleep(60)


def start_vps_monitor():
    """Spawn the monitor thread (idempotent via a module flag)."""
    if getattr(start_vps_monitor, "_started", False):
        return
    t = threading.Thread(target=vps_monitor_loop, daemon=True, name="vps-monitor")
    t.start()
    start_vps_monitor._started = True
