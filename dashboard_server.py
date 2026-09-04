"""Run the Telegram bot and the custom FastAPI dashboard on Render.

This replaces the old Streamlit server wrapper with a normal ASGI app.
"""
import logging
import os
import socket
import subprocess
import sys
import threading

from corporate_actions import config

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("dashboard_server")


def _flush():
    sys.stdout.flush()


def run_bot_server():
    """Run the Telegram bot server in a background thread."""
    log.info("Starting Telegram bot server (long-polling)...")
    os.environ["SERVE_DASHBOARD"] = "false"
    try:
        import bot_server
        bot_server.main()
    except Exception as error:
        log.error("bot_server failed: %s", config.redact(error))
        _flush()


def _pick_port(requested_port: int | str | None) -> int:
    """Return a free port. Prefer the requested one when available."""
    try:
        requested = int(requested_port) if requested_port not in (None, "") else 8000
    except (TypeError, ValueError):
        requested = 8000

    def is_port_free(port: int) -> bool:
        for host in ("127.0.0.1", "0.0.0.0"):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind((host, port))
                except OSError:
                    return False
        return True

    for port in (requested, 8000, 8001, 8002, 8003, 8004, 8005, 8006, 8007, 8008, 9000):
        if is_port_free(port):
            return port

    return requested if requested > 0 else 8000


def main():
    log.info("Starting custom dashboard + bot server on Render...")
    _flush()

    bot_thread = threading.Thread(target=run_bot_server, daemon=True, name="telegram-bot")
    bot_thread.start()

    port = _pick_port(os.getenv("PORT"))
    log.info("Starting FastAPI dashboard on PORT=%s", port)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "dashboard:app",
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
    ]
    log.info("FastAPI command: %s", " ".join(command))
    _flush()
    try:
        proc = subprocess.run(command, check=False)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
