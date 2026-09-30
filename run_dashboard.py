"""Simple runner for the FastAPI dashboard.

Usage: python run_dashboard.py
"""
import logging
import os
import socket

import uvicorn

from corporate_actions.logging_setup import setup_logging
from dashboard import app

log = logging.getLogger(__name__)


def _find_free_port(preferred: int | None = None) -> int:
    """Return the preferred port when bindable, else an OS-assigned free one."""
    if preferred is not None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", int(preferred)))
                return int(preferred)
        except OSError as error:
            log.warning("Preferred port %s unavailable (%s) - picking a free one", preferred, error)
    # Let the OS pick a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    setup_logging()
    env_port = os.getenv("PORT")
    preferred = int(env_port) if env_port and env_port.isdigit() else None
    port = _find_free_port(preferred)
    log.info("Starting Royal Stock dashboard on 0.0.0.0:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
