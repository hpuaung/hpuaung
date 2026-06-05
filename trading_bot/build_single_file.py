#!/usr/bin/env python3
"""
build_single_file.py — bundle the whole multi-module bot into one `bot.py`.

Each source module is base64-embedded (so quoting can never break) and, at
runtime, registered into sys.modules and exec'd in dependency order. The
original source is used verbatim — no code is rewritten — so the single-file
build behaves identically to the package. Re-run this whenever the source
changes:  python build_single_file.py
"""

import os
import base64

HERE = os.path.dirname(os.path.abspath(__file__))

# (sys.modules name, relative source path) in dependency order.
MODULES = [
    ("database", "database.py"),
    ("utils.rate_limiter", "utils/rate_limiter.py"),
    ("utils.vps_optimizer", "utils/vps_optimizer.py"),
    ("utils.indicators", "utils/indicators.py"),
    ("utils.binance_client", "utils/binance_client.py"),
    ("utils.news", "utils/news.py"),
    ("models.train", "models/train.py"),
    ("strategies.trend", "strategies/trend.py"),
    ("strategies.reversion", "strategies/reversion.py"),
    ("strategies.breakout", "strategies/breakout.py"),
    ("strategies.ai_hybrid", "strategies/ai_hybrid.py"),
    ("execution.risk_guard", "execution/risk_guard.py"),
    ("execution.orders", "execution/orders.py"),
    ("execution.position_manager", "execution/position_manager.py"),
    ("notifications.telegram_bot", "notifications/telegram_bot.py"),
    ("engine", "engine.py"),
]
APP = ("app", "app.py")
PACKAGES = ["utils", "strategies", "models", "execution", "notifications"]


def _b64(path):
    with open(os.path.join(HERE, path), "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def build():
    sources = {name: (rel, _b64(rel)) for name, rel in MODULES}
    app_rel, app_b64 = APP[1], _b64(APP[1])

    lines = []
    lines.append("#!/usr/bin/env python3")
    lines.append('"""')
    lines.append("Binance Futures Trading Bot — SINGLE-FILE build (auto-generated).")
    lines.append("")
    lines.append("Run with:  streamlit run bot.py --server.address 0.0.0.0 --server.port 8080")
    lines.append("Dependencies still required: pip install -r requirements.txt")
    lines.append("All API keys / tokens are entered in the dashboard (Settings tab) and")
    lines.append("stored in this folder's trading_bot.db — nothing is hardcoded.")
    lines.append('"""')
    lines.append("import sys, types, base64")
    lines.append("")
    lines.append(f"_PACKAGES = {PACKAGES!r}")
    lines.append("# name -> (original_relative_path, base64 source)")
    lines.append("_SRC = {")
    for name, rel in MODULES:
        rel_, b64 = sources[name]
        lines.append(f"    {name!r}: ({rel_!r}, {b64!r}),")
    lines.append("}")
    lines.append(f"_APP = ({app_rel!r}, {app_b64!r})")
    lines.append(f"_ORDER = {[n for n, _ in MODULES]!r}")
    lines.append("")
    lines.append("def _bootstrap():")
    lines.append("    # Register parent packages.")
    lines.append("    for p in _PACKAGES:")
    lines.append("        if p not in sys.modules:")
    lines.append("            m = types.ModuleType(p); m.__path__ = []; sys.modules[p] = m")
    lines.append("    # Register every submodule (empty) and attach to its parent package.")
    lines.append("    for name in _ORDER:")
    lines.append("        rel, _ = _SRC[name]")
    lines.append("        mod = types.ModuleType(name); mod.__file__ = rel")
    lines.append("        sys.modules[name] = mod")
    lines.append("        if '.' in name:")
    lines.append("            parent, child = name.rsplit('.', 1)")
    lines.append("            setattr(sys.modules[parent], child, mod)")
    lines.append("    # Execute in dependency order.")
    lines.append("    for name in _ORDER:")
    lines.append("        rel, b64 = _SRC[name]")
    lines.append("        src = base64.b64decode(b64).decode('utf-8')")
    lines.append("        exec(compile(src, rel, 'exec'), sys.modules[name].__dict__)")
    lines.append("")
    lines.append("_bootstrap()")
    lines.append("")
    lines.append("# Run the Streamlit app as the main module.")
    lines.append("_app_src = base64.b64decode(_APP[1]).decode('utf-8')")
    lines.append("exec(compile(_app_src, _APP[0], 'exec'), {'__name__': '__main__', '__file__': _APP[0]})")
    lines.append("")

    out = os.path.join(HERE, "bot.py")
    with open(out, "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote {out} ({os.path.getsize(out)} bytes, {len(MODULES)+1} modules bundled)")


if __name__ == "__main__":
    build()
