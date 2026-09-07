"""Headless screen verification: screenshot key pages on a fresh server.

Starts uvicorn on --port (default 8001), captures desktop + mobile shots
of every tab with Selenium + headless Edge/Chrome, saves PNGs to --out
(default .freebuff/shots, gitignored), then stops the server.

    python tools/shot.py [--port 8001] [--out .freebuff/shots] [--only /,/market]

Screenshots are for human/agent visual review (Read tool renders PNGs).
Commits should include this script, never the PNGs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAGES = [
    "/",
    "/watchlist",
    "/fundamentals?symbol=RELIANCE",
    "/market",
    "/movers",
    "/forecast?symbol=RELIANCE",
    "/checklist?symbol=RELIANCE",
    "/indicator?symbol=RELIANCE&name=RSI",
    "/news",
    "/invest",
    "/invest/stocks",
    "/invest/stocks/average",
    "/invest/stocks/profit",
    "/invest/stocks/recovery",
    "/invest/stocks/pnl",
    "/invest/stocks/checklist",
    "/invest/mutual-funds",
    "/invest/bonds",
    "/invest/commodities",
    "/exdates",
    "/system",
]

VIEWPORTS = {
    "desk": (1440, 2400),
    "mob": (390, 2200),
}

def wait_for_server(base: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def find_free_port(preferred: int) -> int:
    """First free 127.0.0.1 port from preferred upward (never hijack one).

    A stale listener (e.g. an orphaned earlier run) would otherwise serve
    outdated code while the new server fails to bind silently.
    """
    import socket

    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port found")


def slug(path: str) -> str:
    name = path.split("?")[0].strip("/").replace("/", "_") or "home"
    return name


def capture(base: str, out: Path, only: list[str] | None) -> int:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    failures = 0
    options = ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--hide-scrollbars")
    options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    driver = webdriver.Chrome(options=options)
    try:
        for name, (width, height) in VIEWPORTS.items():
            driver.set_window_size(width, height)
            for path in PAGES:
                if only and path not in only:
                    continue
                url = base + path
                try:
                    driver.get(url)
                    # let JS-rendered tables settle (screener/fundamentals fetch)
                    time.sleep(6)
                    dest = out / f"{slug(path)}-{name}.png"
                    driver.save_screenshot(str(dest))
                    print(f"  OK   {path} [{name}] -> {dest.name}")
                except Exception as error:
                    print(f"  FAIL {path} [{name}]: {error}")
                    failures += 1
    finally:
        driver.quit()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--out", default=".freebuff/shots")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    out = (ROOT / args.out) if not Path(args.out).is_absolute() else Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = [o.strip() for o in args.only.split(",") if o.strip()] or None
    port = find_free_port(args.port)
    if port != args.port:
        print(f"port {args.port} busy - using {port}")
    base = f"http://127.0.0.1:{port}"

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dashboard:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_server(base):
            print("server did not start")
            return 2
        print(f"server up: {base}")
        # fail fast if the fresh server doesn't know a requested route
        # (stale-code servers shoot misleading 404 screenshots otherwise)
        for path in PAGES:
            if only and path not in only:
                continue
            try:
                with urllib.request.urlopen(base + path, timeout=20) as resp:
                    code = resp.status
            except Exception as error:
                code = f"ERR {error}"
            if code != 200:
                print(f"  ABORT route {path} -> {code} (server code is stale?)")
                return 3
        failures = capture(base, out, only)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except Exception:
            server.kill()
    print("done" if failures == 0 else f"{failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
