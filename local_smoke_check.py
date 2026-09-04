#!/usr/bin/env python3
"""Local smoke-check for the Telegram stock bot.

Run this before deploying to Render or after pulling fresh code.
It validates the environment, the universe loader, and the regression tests.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def log(title: str, ok: bool, detail: str) -> None:
    status = "OK" if ok else "FAIL"
    print(f"[{status}] {title}: {detail}")


def env_ok() -> bool:
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        log("Environment", False, f"missing {', '.join(missing)}")
        return False
    log("Environment", True, "required variables are set")
    return True


def universe_ok() -> bool:
    try:
        sys.path.insert(0, str(ROOT))
        from corporate_actions.sources.universe import get_index_universe

        symbols = get_index_universe("nifty500")
        ok = bool(symbols)
        log("Universe loader", ok, f"loaded {len(symbols)} symbols")
        return ok
    except Exception as exc:  # pragma: no cover - smoke check only
        log("Universe loader", False, f"import or fetch failed: {exc}")
        return False


def test_ok() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_universe_fallback"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            log("Regression test", True, "unittest passed")
            return True
        log("Regression test", False, result.stdout.strip() or result.stderr.strip() or "failed")
        return False
    except Exception as exc:  # pragma: no cover - smoke check only
        log("Regression test", False, f"could not run: {exc}")
        return False


def main() -> int:
    all_ok = True
    if not env_ok():
        all_ok = False
    if not universe_ok():
        all_ok = False
    if not test_ok():
        all_ok = False

    print("\nSummary:")
    print("Local smoke check passed." if all_ok else "Local smoke check failed.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
