"""Movement screens over an index universe: /movers, /gainers, /losers.

One implementation backs all three commands so they stay feature-identical.
Replies in two stages so the user never waits blind: an immediate
acknowledgment, then the initial price-only report as soon as quotes are in,
and finally an updated full report with fundamentals.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic

from .. import storage
from ..core.text import escape, split_messages
from ..formatting.stock import _fundamentals_lines, _rsi_signal, _wk52_signal
from ..market import MOVERS_PERIODS, fetch_period_change, period_label
from ..sources import FUND_MAX_ROWS, get_fundamentals, get_index_universe
from ..telegram.markup import fundamentals_button, symbol_buttons
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def parse_screen_parts(parts, default_period, default_direction,
                       default_count, default_universe) -> tuple:
    """Extract (period, direction, count, universe) from screen command args.

    One shared parser backs /movers, /gainers and /losers so they all accept
    the same tokens in any order:
      periods   5m 15m 30m 1h 2h 4h today 2d 3d 5d 1w 2w 1mo 3mo 6mo 1y
      direction gainers/losers/all
      count     any number 1-100
      universe  n100/nifty100 or n500/nifty500 keyword, or a second number

    A bare `100`/`500` means the index universe for /movers (which shows all
    stocks anyway) but a *count* for /gainers and /losers, so `/losers 1mo
    100` means "top 100 losers" while `/movers 500` means "NIFTY 500".
    """
    period, direction, count, universe = (
        default_period, default_direction, default_count, default_universe)
    explicit_count = False
    for token in parts[1:]:
        t = token.lower()
        if t in MOVERS_PERIODS:
            period = MOVERS_PERIODS[t]
        elif t in ("gainers", "gainer", "positive", "up"):
            direction = "gainers"
        elif t in ("losers", "loser", "negative", "down"):
            direction = "losers"
        elif t in ("all", "both", "mixed"):
            direction = "all"
        elif t in ("100", "n100", "nifty100", "nifty-100", "nifty 100"):
            if t == "100" and not explicit_count and default_count is not None:
                count = 100
                explicit_count = True
            else:
                universe = "nifty100"
        elif t in ("500", "n500", "allstocks", "all-stocks", "nifty500",
                   "nifty-500", "nifty 500"):
            if t == "500" and not explicit_count and default_count is not None:
                count = 500
                explicit_count = True
            else:
                universe = "nifty500"
        else:
            try:
                count = max(1, min(100, int(t)))
                explicit_count = True
            except ValueError:
                pass
    return period, direction, count, universe


def _move_icon(change: float) -> str:
    if change >= 3.0:
        return "\U0001F7E2\u25b2\u25b2"
    if change >= 1.0:
        return "\U0001F7E2\u25b2"
    if change <= -3.0:
        return "\U0001F534\u25bc\u25bc"
    if change <= -1.0:
        return "\U0001F534\u25bc"
    if change >= 0:
        return "\U0001F7E1\u25b2"
    return "\U0001F7E1\u25bc"


def format_price_movers_report(rows: list, header: str) -> str:
    """Format the fast initial price-only movers report (Phase 1)."""
    from ..core.numbers import fmt_money

    lines = [header]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        price = d.get("price")
        sign = "+" if change >= 0 else ""
        lines.append(
            f"{idx}. {_move_icon(change)} <b>{escape(sym)}</b>  "
            f"{fmt_money(price)}  <b>{sign}{change:.2f}%</b>"
        )
    lines.append("")
    lines.append(
        f"\u23f3 Price data loaded for {len(rows)} stocks. "
        "Fetching 52W range, RSI, P/E &amp; fundamentals... "
        "Updated report coming in a few seconds."
    )
    return "\n".join(lines)


def format_enriched_movers_report(rows: list, header: str, fund_by_sym: dict) -> str:
    """Format the full enriched fundamentals movers report with spacious card layout."""
    from ..core.numbers import fmt_money

    enriched_lines = [header, ""]
    for idx, (sym, d) in enumerate(rows, 1):
        change = d["change_pct"]
        price = d.get("price")
        fund = fund_by_sym.get(sym)
        sign = "+" if change >= 0 else ""
        chg_str = f"{sign}{change:.2f}%"
        sig_emoji, _ = _wk52_signal(price, fund)
        sig_prefix = f" {sig_emoji}" if sig_emoji else ""
        enriched_lines.append(
            f"{idx}. {_move_icon(change)}{sig_prefix} <b>{escape(sym)}</b>  "
            f"{fmt_money(price)}  <b>{chg_str}</b>"
        )
        fund_lines = _fundamentals_lines(fund, price)
        for fl in fund_lines:
            enriched_lines.append("   " + fl)
        enriched_lines.append("")
    return "\n".join(enriched_lines)


_LAST_SCREEN: dict[int, dict] = {}


def handle_market_screen(chat_id, parts, default_direction="all",
                         default_period=("intraday", 60), default_count=15,
                         default_universe="nifty100") -> None:
    """Screen an index universe by price movement over a time window."""
    period, direction, count, universe = parse_screen_parts(
        parts, default_period, default_direction, default_count,
        default_universe)

    universe_label = "NIFTY 500" if universe == "nifty500" else "NIFTY 100"
    period_label_text = period_label(*period)
    t0 = monotonic()
    log.info(
        "screen %s: period=%s direction=%s count=%d universe=%s",
        parts[0], period_label_text, direction, count, universe_label,
    )

    # Phase 0 - acknowledge immediately so the user never waits blind while
    # the universe + quotes are fetched (NIFTY 500 can take a minute or two).
    reply(
        chat_id,
        f"Scanning {universe_label} over {period_label_text} for {direction}... "
        "This can take a minute or two.",
    )
    log.info("screen %s: sent initial acknowledgment", parts[0])

    symbols = get_index_universe(universe)
    if not symbols:
        log.warning("screen %s: no symbols loaded for universe %s", parts[0], universe)
        reply(chat_id, "Could not load the stock universe right now. Try again in a minute.")
        return
    log.info(
        "screen %s: universe loaded (%d symbols) in %.1fs",
        parts[0], len(symbols), monotonic() - t0,
    )

    def _fetch(sym):
        return sym, fetch_period_change(sym, period)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            try:
                data = fut.result()[1]
            except Exception as exc:
                data = None
                log.info(
                    "screen %s: change fetch failed for %s: %s",
                    parts[0], sym, exc,
                )
            fetched.append((sym, data))
            if done % 100 == 0 or done == len(symbols):
                log.info(
                    "screen %s: change fetch progress %d/%d symbols",
                    parts[0], done, len(symbols),
                )
    log.info(
        "screen %s: change fetch complete (%d symbols) in %.1fs",
        parts[0], len(symbols), monotonic() - t0,
    )

    rows = [(sym, d) for sym, d in fetched if d and d.get("change_pct") is not None]
    if direction == "gainers":
        rows = [r for r in rows if r[1]["change_pct"] > 0]
        rows.sort(key=lambda r: r[1]["change_pct"], reverse=True)  # highest first
        title = f"<b>Top Gainers - {period_label_text}</b>"
    elif direction == "losers":
        rows = [r for r in rows if r[1]["change_pct"] < 0]
        rows.sort(key=lambda r: r[1]["change_pct"])  # most negative first
        title = f"<b>Top Losers - {period_label_text}</b>"
    else:
        rows.sort(key=lambda r: r[1]["change_pct"])  # lower -> higher
        title = f"<b>Movers - {period_label_text}</b> · {direction} (lower \u2192 higher)"

    if count:
        rows = rows[:count]
    failed = len(fetched) - sum(
        1 for _, d in fetched if d and d.get("change_pct") is not None
    )
    if not rows:
        ok = len(fetched) - failed
        log.warning(
            "screen %s: no %s in %s over %s (universe=%d, quotes ok=%d/%d) - "
            "market may be closed or everything moved the other way",
            parts[0], direction, universe_label, period_label_text,
            len(symbols), ok, len(fetched),
        )
        reply(chat_id, f"No movement data found for {period_label_text} ({universe_label}).")
        return

    header = f"{title} · {universe_label} (Top {len(rows)})"

    # Phase 1 - the initial report: movers and their current price only, so
    # the user gets actionable numbers now instead of waiting for the slower
    # fundamentals enrichment.
    phase1_lines = format_price_movers_report(rows, header)
    if failed:
        phase1_lines += f"\n({failed} of {len(symbols)} stocks could not be loaded)"
    fund_mode = (storage.get_user_settings(chat_id) or {}).get("movers_fund", "button")
    phase1_markup = None
    if fund_mode == "button":
        # Remember this screen so the "Get Fundamentals" button can enrich it.
        _LAST_SCREEN[chat_id] = {
            "rows": rows,
            "header": header,
            "failed": failed,
            "symbols": len(symbols),
        }
        phase1_markup = fundamentals_button()
    reply_messages(chat_id, split_messages(phase1_lines.split("\n")), reply_markup=phase1_markup)
    log.info(
        "screen %s: initial report sent (%d rows) in %.1fs (fundamentals=%s)",
        parts[0], len(rows), monotonic() - t0, fund_mode,
    )
    if fund_mode == "button":
        return

    send_screen_fundamentals(chat_id, rows, header, failed, len(symbols), parts[0], t0)


def send_screen_fundamentals(chat_id, rows, header, failed, symbols_total, screen_cmd, t0) -> None:
    """Fetch fundamentals and send the enriched movers report.

    Used by the "Get Fundamentals" button (button mode) and as Phase 2 of a
    normal screen run (auto mode). Rows come from the live screen context so
    the button always enriches the exact report the user just saw.
    """
    # Phase 2 - fetch fundamentals (Screener + Yahoo Finance) and send the
    # enriched report. To protect against screener.in's aggressive rate
    # limiting, the slow screener.in part is only fetched for the first
    # FUND_MAX_ROWS rows; the rest get the fast Yahoo-only fundamentals.
    def _fund_fetch(sym, with_screener):
        return sym, get_fundamentals(sym, with_screener=with_screener)

    t_fund = monotonic()
    fund_by_sym = {}
    tasks = [
        (sym, i < FUND_MAX_ROWS)
        for i, (sym, _) in enumerate(rows)
    ]
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {
            ex.submit(_fund_fetch, sym, with_screener): (sym, with_screener)
            for sym, with_screener in tasks
        }
        done = 0
        for fut in as_completed(futures):
            done += 1
            sym, _ = futures[fut]
            try:
                fund_by_sym[sym] = fut.result()[1]
            except Exception as exc:  # fundamentals are best-effort
                fund_by_sym[sym] = None
                log.info(
                    "screen %s: fundamentals failed for %s: %s",
                    screen_cmd, sym, exc,
                )
            if done % 10 == 0 or done == len(tasks):
                log.info(
                    "screen %s: fundamentals progress %d/%d rows",
                    screen_cmd, done, len(tasks),
                )
    log.info(
        "screen %s: fundamentals fetch complete (%d rows) in %.1fs",
        screen_cmd, len(tasks), monotonic() - t_fund,
    )

    enriched_report = format_enriched_movers_report(rows, header, fund_by_sym)
    if len(rows) > FUND_MAX_ROWS:
        enriched_report += (
            f"\n(fundamentals detail shown for the first "
            f"{FUND_MAX_ROWS} stocks)"
        )
    if failed:
        enriched_report += f"\n({failed} of {symbols_total} stocks could not be loaded)"
    # Cross-link: one tappable button per top symbol -> deep fundamentals
    tap_symbols = [sym for sym, _ in rows[:10]]
    reply_messages(
        chat_id,
        split_messages(enriched_report.split("\n")),
        reply_markup=symbol_buttons(tap_symbols, "fund") if tap_symbols else None,
    )
    log.info(
        "screen %s: final report sent (%d rows) in %.1fs (total %.1fs), "
        "quote failures=%d",
        screen_cmd, len(rows), monotonic() - t_fund, monotonic() - t0, failed,
    )


def handle_movers(chat_id, parts) -> None:
    """Movement screen over an index (default NIFTY 100, all directions)."""
    handle_market_screen(
        chat_id, parts,
        default_direction="all",
        default_period=("intraday", 60),
        default_count=None,
        default_universe="nifty100",
    )


def handle_gainers_losers(chat_id, parts, direction: str) -> None:
    """Top gainers / losers over an index (default NIFTY 500, top 30, today)."""
    handle_market_screen(
        chat_id, parts,
        default_direction=direction,
        default_period=("days", 1),
        default_count=30,
        default_universe="nifty500",
    )
