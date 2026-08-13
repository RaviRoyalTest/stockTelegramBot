"""Overnight gap scanner: /gappers - stocks that gapped at the open.

Shows the gap between a previous close and a session's open plus how far
the price moved after the open - the component /topgainers and /toplosers
do NOT show (they measure prev close -> current price only).

Modes (universe scan, top N by gap size):
  /gappers               -> today's overnight gaps (prev close -> today's open)
  /gappers 2d | 3d       -> the gaps that opened 2 / 3 sessions ago
  /gappers window 3d     -> today's OPEN vs the close 3 sessions ago
                            (multi-session gap window)
  Combine with direction (up / down), universe (nifty100 / nifty500 /
  sp500) and count: e.g. /gappers 2d down 10 nifty100, /gappers window 3d sp500.

Single-symbol form (/gappers GODREJCP) shows that stock's recent
per-session gap history, e.g. a drop from one day's close into the next
day's open.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..core.numbers import format_money
from ..core.text import escape, split_messages
from ..sources import (
    get_gap_change,
    get_gap_history,
    get_index_universe,
    get_quote,
    get_window_gap_change,
)
from ..telegram.markup import inline_command_buttons
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_DEFAULT_COUNT = 15
_DEFAULT_UNIVERSE = "nifty500"
_DEFAULT_WINDOW_SESSIONS = 3

_UNIVERSE_LABEL = {
    "nifty100": "NIFTY 100",
    "nifty500": "NIFTY 500",
    "sp500": "S&P 500",
    "nasdaq100": "NASDAQ 100",
}

# Day tokens -> session count (0 = today, used for the offset / window).
_DAYS_TOKENS = {
    "today": 0, "1d": 1, "2d": 2, "3d": 3, "4d": 4, "5d": 5, "6d": 6,
    "7d": 7, "1w": 7, "2w": 14, "1mo": 30, "month": 30,
}

_UNIVERSE_TOKENS = {
    "100": "nifty100", "n100": "nifty100", "nifty100": "nifty100", "nifty-100": "nifty100",
    "500": "nifty500", "n500": "nifty500", "nifty500": "nifty500", "nifty-500": "nifty500",
    "allstocks": "nifty500", "all-stocks": "nifty500",
    "sp500": "sp500", "spx": "sp500", "s&p500": "sp500", "s&p-500": "sp500",
    "us500": "sp500", "snp500": "sp500", "s&p 500": "sp500", "sp-500": "sp500",
    "nasdaq100": "nasdaq100", "nasdaq-100": "nasdaq100", "nasdaq": "nasdaq100",
    "ndx": "nasdaq100", "us100": "nasdaq100", "us": "nasdaq100", "america": "nasdaq100",
}

_DIRECTION_TOKENS = {
    "up": "gainers", "gainers": "gainers", "gainer": "gainers",
    "positive": "gainers", "gapup": "gainers",
    "down": "losers", "losers": "losers", "loser": "losers",
    "negative": "losers", "gapdown": "losers",
    "all": "all", "both": "all", "mixed": "all",
}

# Everything that means "scan the universe" rather than "this is a symbol".
_SCAN_TOKENS = (
    set(_DAYS_TOKENS) | set(_UNIVERSE_TOKENS) | set(_DIRECTION_TOKENS)
    | {"window", "from", "win", "gapwindow"}
)


def _gap_icon(gap_pct: float) -> str:
    """Green up arrow for a gap-up, red down arrow for a gap-down."""
    return "\U0001F7E2\u25b2" if gap_pct >= 0 else "\U0001F534\u25bc"


def _usd(universe: str) -> bool:
    return universe in ("nasdaq100", "sp500")


def _parse_parts(parts) -> tuple:
    """Parse /gappers args -> (mode, offset, sessions, direction, count, universe)."""
    mode = "today"  # today | offset | window
    offset = 0      # offset mode: session index (0 = today)
    sessions = 0    # window mode: sessions back for the base close
    direction = "all"
    count = _DEFAULT_COUNT
    universe = _DEFAULT_UNIVERSE
    for token in parts[1:]:
        low = token.lower()
        if low in ("window", "from", "win", "gapwindow"):
            mode = "window"
        elif low in _DAYS_TOKENS:
            value = _DAYS_TOKENS[low]
            if mode == "window":
                sessions = value
            elif value > 1:
                mode, offset = "offset", value
            # today / 1d while still in today mode -> keep today's gaps
        elif low in _DIRECTION_TOKENS:
            direction = _DIRECTION_TOKENS[low]
        elif low in _UNIVERSE_TOKENS:
            universe = _UNIVERSE_TOKENS[low]
        elif low.isdigit():
            count = max(1, min(100, int(low)))
    if mode == "window" and sessions <= 0:
        sessions = _DEFAULT_WINDOW_SESSIONS
    return mode, offset, sessions, direction, count, universe


def handle_gappers(chat_id, parts) -> None:
    """Route /gappers: a symbol gets its gap history, anything else scans."""
    if len(parts) >= 2:
        token = parts[1].lower().strip().removesuffix(".ns").removesuffix(".bo")
        if token not in _SCAN_TOKENS and not token.isdigit():
            handle_symbol_gap(chat_id, token)
            return
    handle_universe_scan(chat_id, parts)


def _fetch_gap_row(exchange: str, symbol: str, mode: str,
                   offset: int, sessions: int) -> dict | None:
    """Uniform gap row per mode (same keys as get_gap_change)."""
    try:
        if mode == "offset":
            history = get_gap_history(exchange, symbol, days=offset + 1)
            if len(history) <= offset:
                return None
            row = history[offset]
            return {
                "price": row.get("close"),
                "open": row.get("open"),
                "prev_close": row.get("prev_close"),
                "gap_pct": row.get("gap_pct"),
                "move_from_open_pct": row.get("move_from_open_pct"),
                "name": row.get("name"),
            }
        if mode == "window":
            return get_window_gap_change(exchange, symbol, sessions)
        return get_gap_change(exchange, symbol)
    except Exception:
        return None


def handle_universe_scan(chat_id, parts) -> None:
    """Scan an index universe for gaps (today / N sessions ago / window)."""
    mode, offset, sessions, direction, count, universe = _parse_parts(parts)
    is_us = _usd(universe)
    exchange = "US" if is_us else "NSE"
    label = _UNIVERSE_LABEL.get(universe, "NIFTY 500")
    direction_text = {"gainers": "gap-up", "losers": "gap-down"}.get(direction, "gap-up AND gap-down")
    if mode == "offset":
        ack = f"Scanning {label} for the gaps that opened {offset} session(s) ago ({direction_text})..."
    elif mode == "window":
        ack = f"Scanning {label}: today's open vs the close {sessions} session(s) ago ({direction_text})..."
    else:
        ack = f"Scanning {label} for {direction_text} (prev close \u2192 today's open)..."
    reply(chat_id, ack + " this can take a minute or two.")

    symbols = get_index_universe(universe)
    if not symbols:
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info("gappers: %s mode over %s (%d symbols, %s)", mode, label, len(symbols), direction)

    def _fetch(symbol):
        return symbol, _fetch_gap_row(exchange, symbol, mode, offset, sessions)

    rows = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_fetch, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                data = future.result()[1]
            except Exception:
                data = None
            if data and data.get("gap_pct") is not None:
                rows.append((symbol, data))

    if direction in ("gainers", "up"):
        rows = [row for row in rows if row[1]["gap_pct"] > 0]
        rows.sort(key=lambda row: row[1]["gap_pct"], reverse=True)
    elif direction in ("losers", "down"):
        rows = [row for row in rows if row[1]["gap_pct"] < 0]
        rows.sort(key=lambda row: row[1]["gap_pct"])
    else:
        rows.sort(key=lambda row: abs(row[1]["gap_pct"]), reverse=True)

    shown = rows[:count]
    if not shown:
        reply(
            chat_id,
            f"No gap data for {label} right now (market may be closed or Yahoo unavailable).",
        )
        return

    currency = "USD" if is_us else "INR"
    if mode == "offset":
        lines = [
            f"<b>OVERNIGHT GAP SCAN - {label}</b> \u00b7 {offset} session(s) ago",
            f"The gaps at the open {offset} session(s) ago (that session's prev close \u2192 its open).",
        ]
    elif mode == "window":
        lines = [
            f"<b>GAP WINDOW - {label}</b> \u00b7 close {sessions} session(s) ago \u2192 today's open",
            f"Today's open vs the close {sessions} session(s) ago (multi-session gap), then the move since today's open.",
        ]
    else:
        lines = [
            f"<b>OVERNIGHT GAP SCAN - {label}</b> \u00b7 today",
            "Prev close \u2192 today's open (gap), then the current move from open.",
        ]
    for index, (symbol, data) in enumerate(shown, 1):
        gap = data["gap_pct"]
        move = data.get("move_from_open_pct")
        prev = format_money(data["prev_close"], currency)
        open_price = format_money(data["open"], currency)
        now_price = format_money(data["price"], currency)
        if mode == "window":
            context = f"(close {sessions} session(s) ago {prev} \u2192 today's open {open_price})"
        else:
            context = f"({prev} \u2192 {open_price})"
        if mode == "offset":
            move_str = f" \u00b7 closed {now_price} ({move:+.1f}% from open)" if move is not None else ""
        else:
            move_str = f" \u00b7 now {now_price} ({move:+.1f}% from open)" if move is not None else ""
        lines.append(
            f"{index}. {_gap_icon(gap)} <b>{escape(symbol)}</b>  opened <b>{gap:+.2f}%</b> "
            f"{context}{move_str}"
        )
    if len(rows) > len(shown):
        lines.append(f"\u2026 {len(rows) - len(shown)} more of {len(rows)} gapping stocks (limit {count}).")
    lines.append("")
    lines.append("Use /gappers SYMBOL for that stock's recent gap history.")
    reply_messages(chat_id, split_messages(lines))
    log.info("gappers: sent %d row(s) for %s (%d gapping)", len(shown), label, len(rows))


def handle_symbol_gap(chat_id, raw_symbol: str) -> None:
    """Gap card for ONE symbol: today's gap + recent per-session gap history."""
    raw = raw_symbol.upper().strip()
    exchange = "NSE"
    try:
        quote = get_quote("NSE", raw)
        if not quote or quote.get("price") is None:
            us_quote = get_quote("US", raw) or {}
            if us_quote.get("price") is not None or us_quote.get("name"):
                exchange = "US"
    except Exception:
        pass

    history = get_gap_history(exchange, raw, days=7)
    if not history:
        reply(
            chat_id,
            f"No gap data found for <code>{escape(raw)}</code> right now. "
            f"Check the symbol (e.g. <code>/gappers GODREJCP</code> or <code>/gappers AAPL</code>).",
            reply_markup=inline_command_buttons(
                ["/gappers GODREJCP", "/gappers AAPL", "/gappers 2d", "/gappers"],
                config.BOT_USERNAME,
            ),
        )
        return

    currency = "USD" if exchange == "US" else "INR"
    today = history[0]
    name = escape((today.get("name") or raw).upper())
    lines = [f"<b>OVERNIGHT GAP - {name}</b> (<code>{escape(raw)}</code>) \u00b7 {exchange}"]
    gap = today["gap_pct"]
    prev = format_money(today["prev_close"], currency)
    open_price = format_money(today["open"], currency)
    lines.append(
        f"{_gap_icon(gap)} Today opened <b>{gap:+.2f}%</b> vs prev close "
        f"({prev} \u2192 {open_price})"
    )
    if today.get("move_from_open_pct") is not None:
        close_now = format_money(today["close"], currency)
        lines.append(
            f"Now {close_now} ({today['move_from_open_pct']:+.1f}% from open \u00b7 "
            f"{(today['close'] / today['prev_close'] - 1.0) * 100.0:+.1f}% vs prev close)"
        )
    lines.append("")
    lines.append("<b>Recent sessions (close \u2192 next open):</b>")
    for row in history[:7]:
        lines.append(
            f"  {row['date']}  open {format_money(row['open'], currency)}  "
            f"prev close {format_money(row['prev_close'], currency)}  "
            f"gap <b>{row['gap_pct']:+.2f}%</b>  close {format_money(row['close'], currency)}"
        )
    reply_messages(chat_id, split_messages(lines))
