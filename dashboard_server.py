"""Run BOTH the always-on Telegram bot AND the Streamlit dashboard on Render.

Render's Web Service expects a single long-running process that binds to $PORT.
This script starts:
  1. The Telegram long-polling bot (same as bot_server.py) in a background thread.
  2. The Streamlit dashboard (dashboard.py) as the foreground process binding
     to $PORT, so Render's health check passes and the dashboard is served.

Usage:  python dashboard_server.py
"""
import logging
import os
import subprocess
import sys
import threading

# Streamlit must be installed; it is in requirements.txt.
import streamlit  # noqa: F401

from corp_actions import config

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("dashboard_server")


def _flush():
    sys.stdout.flush()


def run_bot_server():
    """Run the Telegram bot server in a background thread.

    SERVE_DASHBOARD is set to false so bot_server.main() does not try to
    start the dashboard again (which would recurse infinitely).
    """
    log.info("Starting Telegram bot server (long-polling)...")
    os.environ["SERVE_DASHBOARD"] = "false"
    try:
        import bot_server
        bot_server.main()
    except Exception as exc:
        log.error("bot_server failed: %s", config.redact(exc))
        _flush()


def main():
    log.info("Starting dashboard + bot server on Render...")
    _flush()

    # Start the Telegram bot in a background thread.
    bot_thread = threading.Thread(target=run_bot_server, daemon=True, name="telegram-bot")
    bot_thread.start()

    log.info("Starting Streamlit dashboard on PORT=%s", os.getenv("PORT", "10000"))
    _flush()

    port = os.getenv("PORT", "10000")
    # Run Streamlit as a subprocess so we can pass the port and disable the
    # default telemetry / dev mode checks.
    cmd = [
        sys.executable,
        "-m", "streamlit",
        "run",
        "dashboard.py",
        "--server.port", port,
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    log.info("Streamlit command: %s", " ".join(cmd))
    _flush()
    try:
        proc = subprocess.run(cmd, check=False)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()