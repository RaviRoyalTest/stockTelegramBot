"""Harmonic pattern scanner commands: single-stock reports + index screens."""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic

from .. import storage
from ..core.text import split_messages
from ..harmonic import SCAN_MAX_ROWS, SCAN_PRIORITY, analyze, format_report, format_scan_row
from ..sources import get_index_universe
from .helpers import reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

HARMONIC_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d", "1w")

# Universe keywords for the bulk screener. "all"/bare defaults to NIFTY 100,
# "500" switches to NIFTY 500 (mirrors the movers 100|500 index selector).
HARMONIC_SCAN_UNIVERSES = {
    "all": "nifty100",
    "100": "nifty100",
    "nifty100": "nifty100",
    "nifty-100": "nifty100",
    "500": "nifty500",
    "nifty500": "nifty500",
    "nifty-500": "nifty500",
}


def handle_harmonic(chat_id, parts) -> None:
    """Harmonic pattern scanner.

    Screener mode (compact, one line per stock - a "smaller version" of the
    full report so the whole index fits in a few messages):
      /harmonic          -> NIFTY 100, daily
      /harmonic all      -> NIFTY 100, daily
      /harmonic 500      -> NIFTY 500, daily
      /harmonic 500 1w   -> NIFTY 500, weekly chart
    Single-stock detail (full report with PRZ, entry, SL & targets):
      /harmonic SYMBOL [TIMEFRAME]   e.g. /harmonic RELIANCE, /harmonic TATATECH 1h
      /harmonic 3                    -> full report for watchlist #3
    """
    if len(parts) >= 2 and parts[1].lower() in HARMONIC_SCAN_UNIVERSES:
        universe = HARMONIC_SCAN_UNIVERSES[parts[1].lower()]
        tf = "1d"
        if len(parts) >= 3:
            cand = parts[2].lower()
            if cand in HARMONIC_TIMEFRAMES:
                tf = cand
            else:
                reply(
                    chat_id,
                    f"Unknown timeframe <code>{html.escape(parts[2])}</code>. "
                    f"Options: {', '.join(HARMONIC_TIMEFRAMES)}",
                )
                return
        handle_harmonic_scan(chat_id, universe, tf)
        return

    if len(parts) < 2:
        # Bare /harmonic -> default NIFTY 100 screener, like /movers.
        handle_harmonic_scan(chat_id, "nifty100", "1d")
        return

    raw = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    tf = "1d"
    if len(parts) >= 3:
        cand = parts[2].lower()
        if cand in HARMONIC_TIMEFRAMES:
            tf = cand
        else:
            reply(
                chat_id,
                f"Unknown timeframe <code>{html.escape(parts[2])}</code>. "
                f"Options: {', '.join(HARMONIC_TIMEFRAMES)}",
            )
            return

    if raw.isdigit():
        items = storage.get_user_list(chat_id)
        n = int(raw)
        if not items:
            reply(chat_id, "Your watchlist is empty — add stocks with <code>/add SYMBOL</code> first.")
            return
        if n < 1 or n > len(items):
            reply(chat_id, f"Your watchlist has {len(items)} stock(s) — use a position 1..{len(items)}.")
            return
        item = items[n - 1]
        symbol, exchange = item["symbol"], item["exchange"]
    else:
        symbol, exchange = raw, "NSE"

    t0 = monotonic()
    log.info("handle_harmonic: scanning %s on %s (chat %s)", symbol, tf, chat_id)
    try:
        res = analyze(exchange, symbol, tf)
    except Exception as exc:
        log.warning("handle_harmonic failed for %s: %s", symbol, exc)
        reply(chat_id, f"Could not scan <code>{html.escape(symbol)}</code>: {html.escape(str(exc))}")
        return
    if not res:
        reply_suggestions(chat_id, symbol, "harmonic")
        return

    lines = format_report(res)
    reply_messages(chat_id, split_messages(lines))
    log.info("handle_harmonic: done %s %s in %.1fs", symbol, tf, monotonic() - t0)


def handle_harmonic_scan(chat_id, universe, tf) -> None:
    """Bulk /harmonic screener over an index universe (compact report).

    Scans every symbol in NIFTY 100 / NIFTY 500 for a harmonic formation and
    replies with one compact line per stock that has one, sorted most
    actionable first. Full detail for any stock is one /harmonic SYMBOL away.
    """
    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    t0 = monotonic()
    log.info(
        "harmonic scan: universe=%s timeframe=%s (chat %s)",
        universe_label, tf, chat_id,
    )
    reply(
        chat_id,
        f"Scanning {universe_label} stocks for harmonic patterns on the "
        f"{tf} chart... this can take a minute or two.",
    )

    symbols = get_index_universe(universe)
    if not symbols:
        log.warning("harmonic scan: no symbols loaded for %s", universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info("harmonic scan: universe loaded (%d symbols)", len(symbols))

    def _scan(sym):
        try:
            return sym, analyze("NSE", sym, tf, light=True)
        except Exception as exc:  # one bad symbol must not kill the scan
            log.info("harmonic scan: failed for %s: %s", sym, exc)
            return sym, None

    found = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(_scan, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                res = fut.result()[1]
            except Exception as exc:
                res = None
            if res and res.get("pattern") and res.get("status") not in (
                "No harmonic pattern detected", "Pattern invalidated",
            ):
                found.append(res)
            if done % 50 == 0 or done == len(symbols):
                log.info(
                    "harmonic scan: progress %d/%d symbols, %d pattern(s)",
                    done, len(symbols), len(found),
                )

    log.info(
        "harmonic scan: %d/%d symbols with a pattern in %.1fs",
        len(found), len(symbols), monotonic() - t0,
    )
    if not found:
        reply(
            chat_id,
            f"No harmonic patterns detected across {universe_label} on the {tf} chart.",
        )
        return

    found.sort(
        key=lambda r: (
            SCAN_PRIORITY.get(r.get("status"), 9),
            r.get("pattern") or "",
            r["symbol"],
        )
    )
    shown = found[:SCAN_MAX_ROWS]
    lines = [
        f"<b>HARMONIC SCAN - {universe_label}</b> \u00b7 {tf} chart",
        f"{len(found)} stock(s) showing a formation"
        + (f" (top {len(shown)} by actionability)" if len(found) > len(shown) else ""),
    ]
    for idx, r in enumerate(shown, 1):
        lines.append(f"{idx}. {format_scan_row(r)}")
    lines.append("")
    lines.append("Use /harmonic SYMBOL for the full report (PRZ, entry, SL & targets).")
    reply_messages(chat_id, split_messages(lines))
    log.info(
        "harmonic scan: sent %d row(s) in %.1fs",
        len(shown), monotonic() - t0,
    )
