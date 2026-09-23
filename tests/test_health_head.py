"""Uptime-monitor regression: HEAD must not 405.

UptimeRobot's default HTTP monitor probes with HEAD; FastAPI's APIRoute
answers 405 for HEAD unless the route lists the method explicitly - which
made the monitor report the site DOWN while it was perfectly healthy
(HEAD /health -> 405, GET /health -> 200).
"""
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HealthHeadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        port = _free_port()
        cls.base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "PYTHONPATH": ROOT}
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "dashboard:app", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=ROOT,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen(cls.base + "/health", timeout=2)
                return
            except Exception:
                time.sleep(0.5)
        cls.proc.kill()
        raise RuntimeError("dashboard did not start within 60s")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()

    def _status(self, method: str, path: str) -> int:
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_head_health_returns_200(self):
        self.assertEqual(self._status("HEAD", "/health"), 200)

    def test_head_index_returns_200(self):
        self.assertEqual(self._status("HEAD", "/"), 200)

    def test_get_health_returns_200(self):
        self.assertEqual(self._status("GET", "/health"), 200)


if __name__ == "__main__":
    unittest.main()
