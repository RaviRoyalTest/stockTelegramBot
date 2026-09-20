"""Opening/closing session report builder (India + US).

Orchestrates the data layer and the table renderers into Telegram-ready
message chunks. Applies the market-closed rule: on a weekend or an exchange
holiday (detected from the latest regular-session bar) it emits a
MARKET CLOSED notice with the next session instead of substituting old data.
"""
from __future__ import annotations

import logging
from datetime import datetime

from ..market.hours import is_market_open, local_now, next_open_after
from . import data as _data
from . import tables as _tables

log = logging.getLogger(__name__)

INDIA_MARKET = "in"
US_MARKET = "us"


def _snapshot_line(market: str) -> str:
    """Date plus snapshot timestamp in the market tz (and IST for the US)."""
    local = local_now(market)
    stamp = local.strftime("%H:%M")
    date_text = local.strftime("%d-%b-%Y")
    state = "OPEN" if is_market_open(market) else "CLOSED"
    if market == US_MARKET:
        ist_text = stamp
        try:
            from zoneinfo import ZoneInfo
            ist_text = datetime.fromtimestamp(local.timestamp(), ZoneInfo("Asia/Kolkata")).strftime("%H:%M")
        except Exception:
            pass
        return (
            f"\U0001F4C5 <b>{date_text}</b> | \u23F1 <b>{stamp} ET</b> | "
            f"\u23F1 <b>{ist_text} IST</b> | \U0001F1FA\U0001F1F8 U.S. Market: <b>{state}</b>"
        )
    return (
        f"\U0001F4C5 <b>{date_text}</b> | \u23F1 <b>{stamp} IST</b> | "
        f"\U0001F1EE\U0001F1F3 Indian Market: <b>{state}</b>"
    )


def _closed_lines(market: str, reason: str) -> list[str]:
    label = "Indian" if market == INDIA_MARKET else "U.S."
    flag = "\U0001F1EE\U0001F1F3" if market == INDIA_MARKET else "\U0001F1FA\U0001F1F8"
    try:
        next_dt = datetime.fromtimestamp(next_open_after(market), local_now(market).tzinfo)
        nxt = next_dt.strftime("%a %d-%b-%Y %H:%M")
        suffix = " IST" if market == INDIA_MARKET else " ET"
    except Exception:
        nxt, suffix = "the next session", ""
    return [
        f"{flag} <b>{label} market: \U0001F534 MARKET CLOSED</b>",
        f"Reason: {reason}",
        f"Next regular trading session: <b>{nxt}{suffix}</b>",
        "No opening-session report is generated while the market is closed - "
        "previous-session data is never presented as today's opening data.",
    ]


def _market_status(market: str) -> tuple[bool, str]:
    """(is_trading_today, reason_if_closed)."""
    local = local_now(market)
    if local.weekday() >= 5:
        return False, "Weekend - the exchange does not trade on Saturday/Sunday."
    session_date = _data.latest_session_date(market)
    if session_date is not None and session_date != local.date():
        return False, (
            "No regular session today (exchange holiday). "
            f"Last completed session: {session_date.strftime('%d-%b-%Y')}."
        )
    return True, ""


def _final_summary(verified: dict) -> list[str]:
    lines = ["<b>\U0001F4CB Final summary</b>", "<code>Market  Universe                     Verified/Target</code>"]
    order = [
        ("\U0001F1EE\U0001F1F3 India", "Nifty 100", "in100"),
        ("\U0001F1EE\U0001F1F3 India", "Nifty 500 ex-Nifty 100", "in500x"),
        ("\U0001F1EE\U0001F1F3 India", "Nifty Microcap 250", "inmicro"),
        ("\U0001F1FA\U0001F1F8 U.S.", "Mega Cap $200B+", "usmega"),
        ("\U0001F1FA\U0001F1F8 U.S.", "Large Cap $10B-$200B", "uslarge"),
    ]
    total_verified = 0
    total_target = 0
    for flag, universe, key in order:
        count, target = verified.get(key, (0, 20))
        total_verified += count
        total_target += target
        lines.append(f"<code>{flag:<7} {universe:<26} {count}/{target}</code>")
    lines.append("")
    lines.append(
        f"<b>Total verified: {total_verified}/{total_target}</b> "
        "(target 100 = 60 India + 40 U.S.)"
    )
    lines.append(
        f"Volume Change % successfully computed for <b>{verified.get('volume_computed', 0)}</b> stock(s)."
    )
    return lines


def _india_section() -> tuple[list[str], dict]:
    """Renders all three Indian universes; returns (lines, verified counts)."""
    verified: dict = {}
    lines: list[str] = [
        "\U0001F1EE\U0001F1F3 <b>PART 1 - INDIAN STOCK MARKET</b>",
        _snapshot_line(INDIA_MARKET),
    ]
    universes = [
        ("NIFTY 100", _data.get_nifty100(), "in100"),
        ("NIFTY 500 EX-NIFTY 100", _data.get_nifty500_ex_100(), "in500x"),
        ("NIFTY MICROCAP 250", _data.get_microcap250(), "inmicro"),
    ]
    all_rows: list[dict] = []
    for title, symbols, key in universes:
        lines.append(f"<b>\U0001F1EE\U0001F1F3 {title}</b>")
        if not symbols:
            lines.append("Universe unavailable from the official NSE index CSV - no rows fabricated.")
            verified[key] = (0, 20)
            continue
        rows = _data.fetch_universe_moves(INDIA_MARKET, symbols)
        all_rows.extend(rows)
        gainers = _data.top_gainers(rows)
        losers = _data.top_losers(rows)
        verified[key] = (len(gainers) + len(losers), 20)
        lines.append("\U0001F7E2 <i>Top 10 gainers</i>")
        lines.extend(_tables.table("Gainers", gainers, "INR"))
        lines.append("\U0001F534 <i>Top 10 losers</i>")
        lines.extend(_tables.table("Losers", losers, "INR"))
    lines.append("<b>\U0001F4CA Market overview - India</b>")
    lines.extend(_tables.index_table(INDIA_MARKET, _data.get_index_levels(INDIA_MARKET)))
    lines.append("<b>\U0001F525 Volume analysis - India</b>")
    lines.extend(_tables.volume_analysis(all_rows))
    lines.extend(_tables.catalyst_lines(
        _data.top_gainers(all_rows, 4) + _data.top_losers(all_rows, 4),
        "NSE", "\U0001F4F0 Catalysts - India (news-sourced)",
    ))
    verified["volume_computed"] = sum(
        1 for r in all_rows if r.get("volume_change_pct") is not None
    )
    return lines, verified


def _us_section() -> tuple[list[str], dict]:
    """Renders the US Mega/Large-cap tables; returns (lines, verified counts)."""
    verified: dict = {}
    lines: list[str] = [
        "",
        "\U0001F1FA\U0001F1F8 <b>PART 2 - U.S. STOCK MARKET</b>",
        _snapshot_line(US_MARKET),
    ]
    symbols = _data.get_us_universe()
    if not symbols:
        lines.append("US universe unavailable - no rows fabricated.")
        verified["usmega"] = (0, 20)
        verified["uslarge"] = (0, 20)
        verified["volume_computed"] = 0
        return lines, verified
    rows = _data.fetch_universe_moves(US_MARKET, symbols)
    # Market cap does NOT ride on the chart payload - it comes from Yahoo's
    # batched v7/quote endpoint (about six calls for the whole universe).
    # Tickers with no reliable cap stay unclassified; nothing is guessed.
    caps = _data.get_us_market_caps([row["symbol"] for row in rows])
    mega_rows, large_rows, unclassified = _data.split_us_by_cap(rows, caps)
    for title, group, key in (
        ("MEGA CAP ($200B+)", mega_rows, "usmega"),
        ("LARGE CAP ($10B-$200B)", large_rows, "uslarge"),
    ):
        gainers = _data.top_gainers(group)
        losers = _data.top_losers(group)
        verified[key] = (len(gainers) + len(losers), 20)
        lines.append(f"<b>\U0001F1FA\U0001F1F8 {title}</b>")
        lines.append("\U0001F7E2 <i>Top 10 gainers</i>")
        lines.extend(_tables.table("Gainers", gainers, "USD"))
        lines.append("\U0001F534 <i>Top 10 losers</i>")
        lines.extend(_tables.table("Losers", losers, "USD"))
    if unclassified:
        lines.append(
            f"<i>{unclassified} US stock(s) excluded: market cap unavailable or below $10B "
            "(not guessed into a bucket).</i>"
        )
    lines.append("<b>\U0001F4CA Market overview - U.S.</b>")
    lines.extend(_tables.index_table(US_MARKET, _data.get_index_levels(US_MARKET)))
    lines.append("<b>\U0001F525 Volume analysis - U.S.</b>")
    lines.extend(_tables.volume_analysis(mega_rows + large_rows))
    lines.extend(_tables.catalyst_lines(
        _data.top_gainers(mega_rows + large_rows, 4) + _data.top_losers(mega_rows + large_rows, 4),
        "US", "\U0001F4F0 Catalysts - U.S. (news-sourced)",
    ))
    verified["volume_computed"] = sum(
        1 for r in (mega_rows + large_rows) if r.get("volume_change_pct") is not None
    )
    return lines, verified


def build_report(markets: tuple[str, ...] = ("in", "us")) -> list[str]:
    """Build the full opening/closing screener as Telegram HTML message chunks."""
    lines: list[str] = []
    verified: dict = {}
    volume_total = 0
    for market in markets:
        trading, reason = _market_status(market)
        if not trading:
            lines.append("")
            lines.extend(_closed_lines(market, reason))
            continue
        if market == INDIA_MARKET:
            block, counts = _india_section()
        else:
            block, counts = _us_section()
        lines.extend(block)
        volume_total += counts.pop("volume_computed", 0)
        verified.update(counts)
    verified["volume_computed"] = volume_total
    lines.append("")
    lines.extend(_final_summary(verified))
    lines.append(
        "<i>Source: Yahoo Finance regular-session bars (includePrePost=false); "
        "Indian universes: official NSE index constituent CSVs. "
        "Prices/volumes are a point-in-time regular-session snapshot; "
        "Volume Change % compares today's cumulative regular volume with the "
        "previous session's total regular volume and is N/A when that is unavailable.</i>"
    )
    return lines
