"""Live HTTP check for the running dashboard (default http://127.0.0.1:8000).

Verifies every page returns 200 with its marker content, every key API
returns JSON, and the grouped nav is present. Pure stdlib so it runs
anywhere, anytime:

    python tools/check_live.py [base_url]

Exit 0 = all green, 1 = something failed. Use it after every change,
before every commit, and whenever the deployed page looks off.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

PAGES = {
    "/": "Royal Stock",
    "/watchlist": "Watchlist",
    "/fundamentals": "Stock report",
    "/market": "Market screener",
    "/movers": "Market movers",
    "/forecast": "Analyst forecast",
    "/checklist": "Investment checklist",
    "/indicator": "Technical indicator",
    "/news": "Market news",
    "/invest": "Investment tools",
    "/invest/stocks": "Stock tools",
    "/invest/mutual-funds": "Mutual Fund Calculator",
    "/invest/bonds": "Bonds",
    "/invest/commodities": "Gold vs Silver",
    "/exdates": "Corporate actions",
    "/system": "System",
}

APIS = [
    "/health",
    "/api/status",
    "/api/universe?universe=nifty100",
    "/api/search?q=reliance&limit=3",
    "/api/quote?symbol=RELIANCE",
    "/api/metals",
]

NAV_MARKERS = ["nav-group", "/invest/stocks", "topSearch"]


def get(path: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "check_live/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, ""
    except Exception as error:
        return -1, str(error)


def main() -> int:
    failures: list[str] = []
    print(f"live check: {BASE}")

    for path, marker in PAGES.items():
        status, body = get(path)
        ok = status == 200 and marker in body
        print(("  OK  " if ok else "  FAIL"), f"GET {path} -> {status}")
        if not ok:
            failures.append(f"page {path} ({status})")

    for path in APIS:
        status, body = get(path, timeout=60)
        try:
            json.loads(body)
            valid = True
        except Exception:
            valid = False
        ok = status == 200 and valid
        print(("  OK  " if ok else "  FAIL"), f"GET {path} -> {status}")
        if not ok:
            failures.append(f"api {path} ({status})")

    status, home = get("/")
    for marker in NAV_MARKERS:
        ok = status == 200 and marker in home
        print(("  OK  " if ok else "  FAIL"), f"nav marker {marker!r}")
        if not ok:
            failures.append(f"nav marker {marker!r}")

    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for failure in failures:
            print(" -", failure)
        return 1
    print(f"\nall green ({len(PAGES)} pages, {len(APIS)} apis, {len(NAV_MARKERS)} nav markers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
