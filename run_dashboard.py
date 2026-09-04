"""Simple runner for the FastAPI dashboard.

Usage: python run_dashboard.py
"""
import os
import socket
import uvicorn
from dashboard import app


def _find_free_port(preferred: int | None = None) -> int:
    if preferred is not None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", int(preferred)))
                return int(preferred)
        except OSError:
            pass
    # Let the OS pick a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


if __name__ == "__main__":
    env_port = os.getenv("PORT")
    preferred = int(env_port) if env_port and env_port.isdigit() else None
    port = _find_free_port(preferred)
    print(f"Starting Royal Stock dashboard on 0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
