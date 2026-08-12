"""Pure /schedule parsing helpers (no I/O, no Telegram).

Interval / pause-duration / clock-time parsing and the option tail parser
(market word + run window) used by the schedule commands and the dashboard.
Everything here is a pure function over strings, so it is trivially
unit-testable and shared without importing the command handlers.
"""
from __future__ import annotations

import re

MARKET_WORDS = ("in", "us", "any", "off")


def parse_interval_min(raw: str) -> int | None:
    """Parse an interval like '180', '3h', '90m', '1d' into minutes.

    Returns None when the value is unparseable or below the 15-minute floor.
    """
    match = re.fullmatch(r"(\d+)\s*([mhd])?", str(raw or "").strip().lower())
    if not match:
        return None
    minutes = int(match.group(1))
    unit = match.group(2) or "m"
    if unit == "h":
        minutes *= 60
    elif unit == "d":
        minutes *= 24 * 60
    if minutes < 15:
        return None
    return minutes


def parse_pause_minutes(raw: str) -> int | None:
    """Parse a pause duration like '12h', '1d', '3d', '1w', '2w', '1mo' into minutes."""
    match = re.fullmatch(r"(\d+)\s*(m|h|d|w|mo)?", str(raw or "").strip().lower())
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2) or "m"
    if value <= 0:
        return None
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 24 * 60
    if unit == "w":
        return value * 7 * 24 * 60
    if unit == "mo":
        return value * 30 * 24 * 60
    return value


def valid_hhmm(hhmm) -> bool:
    """True when the string is a valid 24h 'HH:MM' clock time."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or "").strip())
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    return 0 <= hour <= 23 and 0 <= minute <= 59


def parse_schedule_options(tokens: list[str]) -> tuple[list[str], dict]:
    """Strip trailing schedule options off a /schedule add command tail.

    Recognises an optional market word ([in|us|any|off]) and an optional
    explicit run window ([from HH:MM to HH:MM]) appended AFTER the command.
    Returns (command_tokens, options_dict) with options keys market,
    window_start, window_end. The market word must appear before the window.
    """
    rest = list(tokens)
    options = {}
    # window: ... from HH:MM to HH:MM (comes AFTER the market word)
    if (
        len(rest) >= 4
        and valid_hhmm(rest[-1])
        and rest[-2].lower() == "to"
        and valid_hhmm(rest[-3])
        and rest[-4].lower() == "from"
    ):
        options["window_end"] = rest[-1]
        options["window_start"] = rest[-3]
        rest = rest[:-4]
    elif (
        len(rest) >= 2
        and valid_hhmm(rest[-1])
        and rest[-2].lower() == "to"
    ):
        # bare 'to HH:MM' without a start -> window from midnight
        options["window_end"] = rest[-1]
        options["window_start"] = "00:00"
        rest = rest[:-2]
    # market word (right after the command / before the window)
    if rest and rest[-1].lower() in MARKET_WORDS:
        options["market"] = "any" if rest[-1].lower() == "off" else rest[-1].lower()
        rest = rest[:-1]
    return rest, options
