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
from ..core.dates import date_from_parts, format_date
from ..core.text import escape, split_messages
from ..formatting.stock_common import _rsi_signal, _wk52_signal
from ..formatting.stock_india import _fundamentals_lines
from ..formatting.stock_us import _us_movers_lines
from ..market import MOVERS_PERIODS, fetch_period_change, period_label
from ..sources import (
    FUND_MAX_ROWS,
    get_daily_change_on_date,
    get_fundamentals,
    get_index_universe,
    get_us_fundamentals,
    universe_exchange,
)
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
      universe  n100/nifty100, n500/nifty500, nasdaq100/ndx/us or
                sp500/s&p500/spx keyword, or a second number

    A bare `100`/`500` means the index universe for /movers (which shows all
    stocks anyway) but a *count* for /gainers and /losers, so `/losers 1mo
    100` means "top 100 losers" while `/movers 500` means "NIFTY 500".

    A DATE token (12-08-2026, 12 Aug, 12aug, 2026-08-12...) switches the
    screen to that day's HISTORICAL movers (close vs previous close on that
    date) instead of the current window - e.g. /toplosers 12-08-2026.
    """
    period, direction, count, universe = (
        default_period, default_direction, default_count, default_universe)
    explicit_count = False
    target_date = None
    tokens = parts[1:]
    index = 0
    while index < len(tokens):
        parsed, consumed = date_from_parts(tokens, index)
        if parsed is not None:
            if target_date is None:
                target_date = parsed
            index += consumed
            continue
        normalized_token = tokens[index].lower()
        if normalized_token in MOVERS_PERIODS:
            period = MOVERS_PERIODS[normalized_token]
        elif normalized_token in ("gainers", "gainer", "positive", "up"):
            direction = "gainers"
        elif normalized_token in ("losers", "loser", "negative", "down"):
            direction = "losers"
        elif normalized_token in ("all", "both", "mixed"):
            direction = "all"
        elif normalized_token in ("100", "n100", "nifty100", "nifty-100", "nifty 100"):
            if normalized_token == "100" and not explicit_count and default_count is not None:
                count = 100
                explicit_count = True
            else:
                universe = "nifty100"
        elif normalized_token in ("500", "n500", "allstocks", "all-stocks", "nifty500",
                   "nifty-500", "nifty 500"):
            if normalized_token == "500" and not explicit_count and default_count is not None:
                count = 500
                explicit_count = True
            else:
                universe = "nifty500"
        elif normalized_token in ("nasdaq100", "nasdaq-100", "nasdaq", "ndx", "us100",
                   "nasdaq-100", "us", "america", "american"):
            universe = "nasdaq100"
        elif normalized_token in ("sp500", "s&p500", "s&p-500", "snp500", "spx",
                   "us500", "s&p 500", "sp-500"):
            universe = "sp500"
        else:
            try:
                count = max(1, min(100, int(normalized_token)))
                explicit_count = True
            except ValueError:
                pass
        index += 1
    return period, direction, count, universe, target_date


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


def _vs_prev_close_tag(data: dict) -> tuple[str, bool]:
    """' · vs prev close +x.xx%' when the window move differs from today's move."""
    today = data.get("change_pct_today")
    change = data.get("change_pct")
    if today is None or change is None or abs(today - change) <= 0.005:
        return "", False
    sign = "+" if today >= 0 else ""
    return f"  \u00b7  vs prev close <b>{sign}{today:.2f}%</b>", True


def format_price_movers_report(rows: list, header: str, is_us: bool = False) -> str:
    """Format the fast initial price-only movers report (Phase 1)."""
    from ..core.numbers import format_money

    lines = [header]
    any_today_tag = False
    for index, (symbol, data) in enumerate(rows, 1):
        change = data["change_pct"]
        price = data.get("price")
        sign = "+" if change >= 0 else ""
        tag, shown = _vs_prev_close_tag(data)
        any_today_tag = any_today_tag or shown
        lines.append(
            f"{index}. {_move_icon(change)} <b>{escape(symbol)}</b>  "
            f"{format_money(price, 'USD' if is_us else 'INR')}  "
            f"<b>{sign}{change:.2f}%</b>{tag}"
        )
    if any_today_tag:
        lines.append("")
        lines.append("\U0001F4A1 <i>vs prev close = today's move from yesterday's close.</i>")
    lines.append("")
    lines.append(
        f"\u23f3 Price data loaded for {len(rows)} stocks. "
        "Fetching 52W range, RSI, P/E &amp; fundamentals... "
        "Updated report coming in a few seconds."
    )
    return "\n".join(lines)


def format_enriched_movers_report(rows: list, header: str, fund_by_symbol: dict,
                                  is_us: bool = False) -> str:
    """Format the full enriched fundamentals movers report with spacious card layout."""
    from ..core.numbers import format_money

    enriched_lines = [header, ""]
    any_today_tag = False
    for index, (symbol, data) in enumerate(rows, 1):
        change = data["change_pct"]
        price = data.get("price")
        fund = fund_by_symbol.get(symbol)
        sign = "+" if change >= 0 else ""
        change_str = f"{sign}{change:.2f}%"
        tag, shown = _vs_prev_close_tag(data)
        any_today_tag = any_today_tag or shown
        sig_emoji, _ = _wk52_signal(price, fund)
        sig_prefix = f" {sig_emoji}" if sig_emoji else ""
        enriched_lines.append(
            f"{index}. {_move_icon(change)}{sig_prefix} <b>{escape(symbol)}</b>  "
            f"{format_money(price, 'USD' if is_us else 'INR')}  <b>{change_str}</b>{tag}"
        )
        fund_lines = _us_movers_lines(fund, price) if is_us else _fundamentals_lines(fund, price)
        for fund_line in fund_lines:
            enriched_lines.append("   " + fund_line)
        enriched_lines.append("")
    if any_today_tag:
        enriched_lines.append("\U0001F4A1 <i>vs prev close = today's move from yesterday's close.</i>")
        enriched_lines.append("")
    return "\n".join(enriched_lines)


_LAST_SCREEN: dict[int, dict] = {}


def handle_market_screen(chat_id, parts, default_direction="all",
                         default_period=("intraday", 60), default_count=15,
                         default_universe="nifty100") -> None:
    """Screen an index universe by price movement over a time window."""
    period, direction, count, universe, target_date = parse_screen_parts(
        parts, default_period, default_direction, default_count,
        default_universe)
    is_us = universe in ("nasdaq100", "sp500")
    exchange = "US" if is_us else "NSE"

    universe_label = {
        "nifty500": "NIFTY 500",
        "nasdaq100": "NASDAQ 100",
        "sp500": "S&P 500",
    }.get(universe, "NIFTY 100")
    period_label_text = (
        f"on {format_date(target_date.isoformat())}" if target_date
        else period_label(*period)
    )
    started_at = monotonic()
    log.info(
        "screen %s: period=%s direction=%s count=%d universe=%s",
        parts[0], period_label_text, direction, count, universe_label,
    )

    # Phase 0 - acknowledge immediately so the user never waits blind while
    # the universe + quotes are fetched (NIFTY 500 can take a minute or two).
    reply(
        chat_id,
        f"Scanning {universe_label} {period_label_text} for {direction}... "
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
        parts[0], len(symbols), monotonic() - started_at,
    )

    def _fetch(symbol):
        if target_date:
            return symbol, get_daily_change_on_date(exchange, symbol, target_date)
        return symbol, fetch_period_change(symbol, period, exchange=exchange)

    fetched = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = {executor.submit(_fetch, symbol): symbol for symbol in symbols}
        done = 0
        for future in as_completed(futures):
            done += 1
            symbol = futures[future]
            try:
                data = future.result()[1]
            except Exception as error:
                data = None
                log.info(
                    "screen %s: change fetch failed for %s: %s",
                    parts[0], symbol, error,
                )
            fetched.append((symbol, data))
            if done % 100 == 0 or done == len(symbols):
                log.info(
                    "screen %s: change fetch progress %d/%d symbols",
                    parts[0], done, len(symbols),
                )
    log.info(
        "screen %s: change fetch complete (%d symbols) in %.1fs",
        parts[0], len(symbols), monotonic() - started_at,
    )

    rows = [(symbol, data) for symbol, data in fetched if data and data.get("change_pct") is not None]
    if direction == "gainers":
        rows = [row for row in rows if row[1]["change_pct"] > 0]
        rows.sort(key=lambda row: row[1]["change_pct"], reverse=True)  # highest first
        title = f"<b>Top Gainers - {period_label_text}</b>"
    elif direction == "losers":
        rows = [row for row in rows if row[1]["change_pct"] < 0]
        rows.sort(key=lambda row: row[1]["change_pct"])  # most negative first
        title = f"<b>Top Losers - {period_label_text}</b>"
    else:
        rows.sort(key=lambda row: row[1]["change_pct"])  # lower -> higher
        title = f"<b>Movers - {period_label_text}</b> · {direction} (lower \u2192 higher)"

    if count:
        rows = rows[:count]
    failed = len(fetched) - sum(
        1 for _, data in fetched if data and data.get("change_pct") is not None
    )
    if not rows:
        success = len(fetched) - failed
        log.warning(
            "screen %s: no %s in %s over %s (universe=%d, quotes ok=%d/%d) - "
            "market may be closed or everything moved the other way",
            parts[0], direction, universe_label, period_label_text,
            len(symbols), success, len(fetched),
        )
        reply(chat_id, f"No movement data found for {period_label_text} ({universe_label}).")
        return

    header = f"{title} · {universe_label} (Top {len(rows)})"

    # Phase 1 - the initial report: movers and their current price only, so
    # the user gets actionable numbers now instead of waiting for the slower
    # fundamentals enrichment.
    phase1_lines = format_price_movers_report(rows, header, is_us=is_us)
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
            "us": is_us,
        }
        phase1_markup = fundamentals_button()
    reply_messages(chat_id, split_messages(phase1_lines.split("\n")), reply_markup=phase1_markup)
    log.info(
        "screen %s: initial report sent (%d rows) in %.1fs (fundamentals=%s)",
        parts[0], len(rows), monotonic() - started_at, fund_mode,
    )
    if fund_mode == "button":
        return

    send_screen_fundamentals(chat_id, rows, header, failed, len(symbols), parts[0], started_at, is_us=is_us)


def send_screen_fundamentals(chat_id, rows, header, failed, symbols_total, screen_cmd, started_at, is_us=False) -> None:
    """Fetch fundamentals and send the enriched movers report.

    Used by the "Get Fundamentals" button (button mode) and as Phase 2 of a
    normal screen run (auto mode). Rows come from the live screen context so
    the button always enriches the exact report the user just saw. For US
    screens (NASDAQ 100) the US fundamentals + USD formatter are used.
    """
    # Phase 2 - fetch fundamentals (Screener + Yahoo Finance) and send the
    # enriched report. To protect against screener.in's aggressive rate
    # limiting, the slow screener.in part is only fetched for the first
    # FUND_MAX_ROWS rows; the rest get the fast Yahoo-only fundamentals.
    def _fund_fetch(symbol, with_screener):
        if is_us:
            return symbol, get_us_fundamentals(symbol)
        return symbol, get_fundamentals(symbol, with_screener=with_screener)

    t_fund = monotonic()
    fund_by_symbol = {}
    tasks = [
        (symbol, index < FUND_MAX_ROWS)
        for index, (symbol, _) in enumerate(rows)
    ]
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fund_fetch, symbol, with_screener): (symbol, with_screener)
            for symbol, with_screener in tasks
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            symbol, _ = futures[future]
            try:
                fund_by_symbol[symbol] = future.result()[1]
            except Exception as error:  # fundamentals are best-effort
                fund_by_symbol[symbol] = None
                log.info(
                    "screen %s: fundamentals failed for %s: %s",
                    screen_cmd, symbol, error,
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

    enriched_report = format_enriched_movers_report(rows, header, fund_by_symbol, is_us=is_us)
    if len(rows) > FUND_MAX_ROWS:
        enriched_report += (
            f"\n(fundamentals detail shown for the first "
            f"{FUND_MAX_ROWS} stocks)"
        )
    if failed:
        enriched_report += f"\n({failed} of {symbols_total} stocks could not be loaded)"
    # Cross-link: one tappable button per top symbol -> deep fundamentals
    tap_symbols = [symbol for symbol, _ in rows[:10]]
    reply_messages(
        chat_id,
        split_messages(enriched_report.split("\n")),
        reply_markup=symbol_buttons(tap_symbols, "fund") if tap_symbols else None,
    )
    log.info(
        "screen %s: final report sent (%d rows) in %.1fs (total %.1fs), "
        "quote failures=%d",
        screen_cmd, len(rows), monotonic() - t_fund, monotonic() - started_at, failed,
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
