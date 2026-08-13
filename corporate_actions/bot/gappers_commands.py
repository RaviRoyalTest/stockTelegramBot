"""Overnight gap scanner: /gappers - stocks that gapped at the open.

Shows the gap between the previous close and today's open (the overnight
move) plus how far the price has moved since the open - the component
/topgainers and /toplosers do NOT show (they measure prev close -> current
price only). Universe-aware (nifty100 / nifty500 / sp500) and
direction-aware (up / down / all). The single-symbol form (/gappers
GODREJCP) shows that stock's recent per-session gap history, e.g. a drop
from one day's close into the next day's open.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.numbers import format_money
from ..core.text import escape, split_messages
from ..sources import get_gap_change, get_gap_history, get_index_universe, get_quote
from .movers_commands import parse_screen_parts
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_DEFAULT_COUNT = 15
_DEFAULT_UNIVERSE = "nifty500"

_UNIVERSE_LABEL = {
    "nifty100": "NIFTY 100",
    "nifty500": "NIFTY 500",
    "sp500": "S&P 500",
    "nasdaq100": "NASDAQ 100",
}

# Tokens that mean "scan the universe" rather than "this is a symbol".
_SCAN_TOKENS = {
    "up", "down", "all", "both", "gapup", "gapdown",
    "100", "500", "n100", "nifty100", "nifty-100", "nifty 100",
    "n500", "nifty500", "nifty-500", "nifty 500", "allstocks", "all-stocks",
    "sp500", "spx", "s&p500", "s&p-500", "us500", "snp500", "s&p 500", "sp-500",
    "nasdaq100", "nasdaq-100", "nasdaq", "ndx", "us100", "us", "america",
}


def _gap_icon(gap_pct: float) -> str:
    """Green up arrow for a gap-up, red down arrow for a gap-down."""
    return "\U0001F7E2\u25b2" if gap_pct >= 0 else "\U0001F534\u25bc"


def _usd(universe: str) -> bool:
    return universe in ("nasdaq100", "sp500")


def handle_gappers(chat_id, parts) -> None:
    """Route /gappers: a symbol gets its gap history, anything else scans."""
    if len(parts) >= 2:
        token = parts[1].lower().strip().removesuffix(".ns").removesuffix(".bo")
        if token not in _SCAN_TOKENS and not token.isdigit():
            handle_symbol_gap(chat_id, token)
            return
    handle_universe_scan(chat_id, parts)


def handle_universe_scan(chat_id, parts) -> None:
    """Scan an index universe for today's overnight gaps (top N by size)."""
    _, direction, count, universe = parse_screen_parts(
        parts, ("days", 1), "all", _DEFAULT_COUNT, _DEFAULT_UNIVERSE,
    )
    is_us = _usd(universe)
    exchange = "US" if is_us else "NSE"
    label = _UNIVERSE_LABEL.get(universe, "NIFTY 500")
    # parse_screen_parts uses the movers vocabulary (gainers/losers/all);
    # gap semantics: gainers=gap-up, losers=gap-down.
    direction_text = {"gainers": "gap-up", "losers": "gap-down"}.get(direction, "gap-up AND gap-down")
    reply(
        chat_id,
        f"Scanning {label} for {direction_text} (prev close \u2192 today's open)... "
        "this can take a minute or two.",
    )

    symbols = get_index_universe(universe)
    if not symbols:
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info("gappers: scanning %s (%d symbols) for %s gaps", label, len(symbols), direction)

    def _fetch(symbol):
        return symbol, get_gap_change(exchange, symbol)

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
        move_str = f" \u00b7 now {now_price} ({move:+.1f}% from open)" if move is not None else ""
        lines.append(
            f"{index}. {_gap_icon(gap)} <b>{escape(symbol)}</b>  opened <b>{gap:+.2f}%</b> "
            f"({prev} \u2192 {open_price}){move_str}"
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
