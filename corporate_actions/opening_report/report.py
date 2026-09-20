"""Opening/closing session report: data collection + rendering.

The engine is split so Telegram and the web dashboard share one code path:

* ``collect(markets)``   -> structured, JSON-friendly report dict (no HTML)
* ``render_telegram()``  -> the same dict rendered as Telegram HTML chunks
* ``build_report()``     -> convenience wrapper used by the bot command

Applies the market-closed rule: on a weekend or an exchange holiday
(detected from the latest regular-session bar) it emits a MARKET CLOSED
notice with the next session instead of substituting old data.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime

from ..market.hours import is_market_open, local_now, next_open_after
from . import data as _data
from . import tables as _tables

log = logging.getLogger(__name__)

INDIA_MARKET = "in"
US_MARKET = "us"

# Daily bars fetched per stock for a historical run: the snapshot session plus
# enough earlier sessions for a healthy reference-bar scan (Yahoo period-mode
# responses can contain phantom null-close bars, so a short window risks
# losing the previous session entirely).
HISTORY_WINDOW_DAYS = 21


# ---------------------------------------------------------------- snapshot --

def _snapshot(market: str, session_date=None) -> dict:
    """Date/timestamp/state for one market (IST conversion for the US).

    With ``session_date`` the snapshot describes a completed historical
    session: its date, mode='historical' and no wall-clock time (the report
    is that session's close, not a live tick).
    """
    if session_date is not None:
        return {
            "market": market,
            "mode": "historical",
            "date": session_date.strftime("%d-%b-%Y"),
            "time_local": "Session close",
            "tz_label": "ET" if market == US_MARKET else "IST",
            "state": "CLOSED",  # a past session is never 'open'
            "time_ist": None,
        }
    local = local_now(market)
    state = "OPEN" if is_market_open(market) else "CLOSED"
    out = {
        "market": market,
        "mode": "live",
        "date": local.strftime("%d-%b-%Y"),
        "time_local": local.strftime("%H:%M"),
        "tz_label": "ET" if market == US_MARKET else "IST",
        "state": state,
    }
    if market == US_MARKET:
        try:
            from zoneinfo import ZoneInfo
            out["time_ist"] = datetime.fromtimestamp(
                local.timestamp(), ZoneInfo("Asia/Kolkata")
            ).strftime("%H:%M")
        except Exception:
            out["time_ist"] = None
    return out


def _closed_block(market: str, reason: str, next_session: str | None = None) -> dict:
    label = "Indian" if market == INDIA_MARKET else "U.S."
    if next_session is None:
        try:
            next_dt = datetime.fromtimestamp(next_open_after(market), local_now(market).tzinfo)
            nxt = next_dt.strftime("%a %d-%b-%Y %H:%M")
            suffix = " IST" if market == INDIA_MARKET else " ET"
            next_session = f"{nxt}{suffix}"
        except Exception:
            next_session = "the next session"
    return {
        "market": market,
        "closed": True,
        "reason": reason,
        "next_session": next_session,
        "label": label,
    }


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


# ----------------------------------------------------------------- collect --

def _collect_universe(
    title: str, symbols: list[str], exchange: str, key: str,
    start: float | None = None, end: float | None = None,
) -> dict:
    """One universe's gainers/losers as plain dicts (empty when unusable)."""
    if not symbols:
        return {"key": key, "title": title, "unavailable": True,
                "gainers": [], "losers": [], "verified": 0, "target": 20}
    rows = _data.fetch_universe_moves(exchange, symbols, start=start, end=end)
    gainers = _data.top_gainers(rows)
    losers = _data.top_losers(rows)
    return {
        "key": key,
        "title": title,
        "unavailable": False,
        "gainers": [dict(r) for r in gainers],
        "losers": [dict(r) for r in losers],
        "verified": len(gainers) + len(losers),
        "target": 20,
        "_rows": rows,  # consumed for volume analysis; stripped before output
    }


def _collect_india(start: float | None = None, end: float | None = None) -> dict:
    universes = [
        _collect_universe("NIFTY 100", _data.get_nifty100(), INDIA_MARKET, "in100", start, end),
        _collect_universe("NIFTY 500 EX-NIFTY 100", _data.get_nifty500_ex_100(), INDIA_MARKET, "in500x", start, end),
        _collect_universe("NIFTY MICROCAP 250", _data.get_microcap250(), INDIA_MARKET, "inmicro", start, end),
    ]
    all_rows: list[dict] = []
    for universe in universes:
        all_rows.extend(universe.pop("_rows", []))
    historical = start is not None and end is not None
    return {
        "market": INDIA_MARKET,
        "snapshot": _snapshot(
            INDIA_MARKET,
            session_date=datetime.fromtimestamp(end).date() if historical else None,
        ),
        "universes": universes,
        "indices": _data.get_index_levels(INDIA_MARKET, start, end),
        "volume_buckets": _tables.volume_buckets_payload(all_rows),
        "catalysts": [] if historical else _tables.catalyst_items(
            _data.top_gainers(all_rows, 4) + _data.top_losers(all_rows, 4), "NSE",
        ),
        "volume_computed": sum(
            1 for r in all_rows if r.get("volume_change_pct") is not None
        ),
    }


def _collect_us(start: float | None = None, end: float | None = None) -> dict:
    symbols = _data.get_us_universe()
    if not symbols:
        return {
            "market": US_MARKET,
            "snapshot": _snapshot(US_MARKET),
            "unavailable": True,
            "universes": [],
            "indices": [],
            "volume_buckets": [],
            "catalysts": [],
            "volume_computed": 0,
            "unclassified": 0,
        }
    rows = _data.fetch_universe_moves(US_MARKET, symbols, start=start, end=end)
    # Market cap does NOT ride on the chart payload - it comes from Yahoo's
    # batched v7/quote endpoint (about six calls for the whole universe).
    # Tickers with no reliable cap stay unclassified; nothing is guessed.
    caps = _data.get_us_market_caps([row["symbol"] for row in rows])
    mega_rows, large_rows, unclassified = _data.split_us_by_cap(rows, caps)
    universes = [_collect_from_rows("MEGA CAP ($200B+)", mega_rows, "usmega"),
                 _collect_from_rows("LARGE CAP ($10B-$200B)", large_rows, "uslarge")]
    scanned = mega_rows + large_rows
    historical = start is not None and end is not None
    return {
        "market": US_MARKET,
        "snapshot": _snapshot(
            US_MARKET,
            session_date=datetime.fromtimestamp(end).date() if historical else None,
        ),
        "universes": universes,
        "indices": _data.get_index_levels(US_MARKET, start, end),
        "volume_buckets": _tables.volume_buckets_payload(scanned),
        "catalysts": [] if historical else _tables.catalyst_items(
            _data.top_gainers(scanned, 4) + _data.top_losers(scanned, 4), "US",
        ),
        "volume_computed": sum(
            1 for r in scanned if r.get("volume_change_pct") is not None
        ),
        "unclassified": unclassified,
    }


def _collect_from_rows(title: str, rows: list[dict], key: str) -> dict:
    """Universe block from already-fetched rows (US cap buckets)."""
    gainers = _data.top_gainers(rows)
    losers = _data.top_losers(rows)
    return {
        "key": key,
        "title": title,
        "unavailable": False,
        "gainers": [dict(r) for r in gainers],
        "losers": [dict(r) for r in losers],
        "verified": len(gainers) + len(losers),
        "target": 20,
    }


def collect(
    markets: tuple[str, ...] = (INDIA_MARKET, US_MARKET),
    target_date=None,
) -> dict:
    """Structured report: one section per requested market + a final summary.

    Sections whose market is closed carry closed=True with reason/next
    session and no stock data - never old data dressed up as today's.

    With ``target_date`` (datetime.date) the report describes that completed
    historical session instead of the live market. The date is refused with a
    closed block when that market did not trade on it (weekend/holiday/before
    the benchmark's history), and each stock present is individually verified
    to have traded that day - a delisted/renamed symbol is unavailable rather
    than ranked against a different session.
    """
    historical = target_date is not None
    window_start = window_end = None
    if historical:
        window_end = _time.mktime(
            (target_date.year, target_date.month, target_date.day, 0, 0, 0, 0, 0, -1)
        ) + 86400 - 1  # target date 23:59:59 local
        window_start = window_end - HISTORY_WINDOW_DAYS * 86400
    report: dict = {
        "sections": [], "total_verified": 0, "total_target": 0,
        "mode": "historical" if historical else "live",
    }
    if historical:
        report["target_date"] = target_date.strftime("%d-%b-%Y")
    volume_total = 0
    for market in markets:
        if historical:
            traded = _data.has_session_on(market, target_date)
            if traded is False:
                block = _closed_block(
                    market,
                    f"No regular session on {target_date.strftime('%d-%b-%Y')} "
                    "(weekend or exchange holiday).",
                    next_session="",
                )
                block["no_report"] = True
                report["sections"].append(block)
                continue
            # traded True or None (probe down): attempt the real build; the
            # per-stock date verification keeps phantom data out either way.
        else:
            trading, reason = _market_status(market)
            if not trading:
                report["sections"].append(_closed_block(market, reason))
                continue
        section = (
            _collect_india(window_start, window_end)
            if market == INDIA_MARKET else _collect_us(window_start, window_end)
        )
        for universe in section.get("universes", []):
            report["total_verified"] += universe["verified"]
            report["total_target"] += universe["target"]
        volume_total += section.get("volume_computed", 0)
        report["sections"].append(section)
    report["volume_computed"] = volume_total
    report["source"] = (
        "Yahoo Finance regular-session bars (includePrePost=false); "
        "Indian universes: official NSE index constituent CSVs."
        + (" Historical mode: completed daily closes for the requested session."
           if historical else "")
    )
    return report


# ------------------------------------------------------------ telegram out --

def _snapshot_line(snap: dict) -> str:
    if snap.get("mode") == "historical":
        label = "Indian" if snap["market"] == INDIA_MARKET else "U.S."
        flag = "\U0001F1EE\U0001F1F3" if snap["market"] == INDIA_MARKET else "\U0001F1FA\U0001F1F8"
        suffix = "IST" if snap["market"] == INDIA_MARKET else "ET"
        return (
            f"\U0001F4C5 <b>{snap['date']}</b> | \u23F1 <b>Session close {suffix}</b> | "
            f"{flag} {label} Market: <b>COMPLETED SESSION</b> (historical)"
        )
    stamp = snap["time_local"]
    if snap["market"] == US_MARKET:
        ist = snap.get("time_ist") or stamp
        return (
            f"\U0001F4C5 <b>{snap['date']}</b> | \u23F1 <b>{stamp} ET</b> | "
            f"\u23F1 <b>{ist} IST</b> | \U0001F1FA\U0001F1F8 U.S. Market: <b>{snap['state']}</b>"
        )
    return (
        f"\U0001F4C5 <b>{snap['date']}</b> | \u23F1 <b>{stamp} IST</b> | "
        f"\U0001F1EE\U0001F1F3 Indian Market: <b>{snap['state']}</b>"
    )


def _closed_lines(block: dict) -> list[str]:
    flag = "\U0001F1EE\U0001F1F3" if block["market"] == INDIA_MARKET else "\U0001F1FA\U0001F1F8"
    if block.get("no_report"):
        tail = (
            "A session report for this date is not possible - the exchange did "
            "not trade, and no other session's data may be substituted."
        )
    else:
        tail = (
            f"Next regular trading session: <b>{block['next_session']}</b>\n"
            "No opening-session report is generated while the market is closed - "
            "previous-session data is never presented as today's opening data."
        )
    return [
        f"{flag} <b>{block['label']} market: \U0001F534 MARKET CLOSED</b>",
        f"Reason: {block['reason']}",
        tail,
    ]


def _index_lines(market: str, levels: list[dict]) -> list[str]:
    title = ("\U0001F1EE\U0001F1F3 Indian indices" if market == INDIA_MARKET
             else "\U0001F1FA\U0001F1F8 US indices")
    lines = [f"<b>{title}</b>", "<code>Index                 Level        Chg      Chg%</code>"]
    for row in levels:
        level = row.get("level")
        level_text = f"{level:,.2f}" if isinstance(level, (int, float)) else "N/A"
        lines.append(
            "<code>"
            f"{row['label'][:20]:<20} {level_text:>12} "
            f"{_tables._amount(row.get('change')):>9} {_tables._pct(row.get('change_pct')):>7}"
            "</code>"
        )
    return lines


def _render_section(section: dict) -> list[str]:
    if section.get("closed"):
        return _closed_lines(section)
    market = section["market"]
    flag = "\U0001F1EE\U0001F1F3" if market == INDIA_MARKET else "\U0001F1FA\U0001F1F8"
    part = "PART 1 - INDIAN STOCK MARKET" if market == INDIA_MARKET else "PART 2 - U.S. STOCK MARKET"
    currency = "INR" if market == INDIA_MARKET else "USD"
    lines = ["", f"{flag} <b>{part}</b>", _snapshot_line(section["snapshot"])]
    if section.get("unavailable"):
        lines.append("US universe unavailable - no rows fabricated.")
    for universe in section.get("universes", []):
        lines.append(f"<b>{flag} {universe['title']}</b>")
        if universe.get("unavailable"):
            lines.append("Universe unavailable from the official NSE index CSV - no rows fabricated.")
            continue
        lines.append("\U0001F7E2 <i>Top 10 gainers</i>")
        lines.extend(_tables.table("Gainers", universe["gainers"], currency))
        lines.append("\U0001F534 <i>Top 10 losers</i>")
        lines.extend(_tables.table("Losers", universe["losers"], currency))
    if section.get("unclassified"):
        lines.append(
            f"<i>{section['unclassified']} US stock(s) excluded: market cap unavailable or "
            "below $10B (not guessed into a bucket).</i>"
        )
    lines.append("<b>\U0001F4CA Market overview</b>")
    lines.extend(_index_lines(market, section.get("indices") or []))
    lines.append("<b>\U0001F525 Volume analysis</b>")
    lines.extend(_tables.volume_analysis_payload(section.get("volume_buckets") or []))
    lines.extend(_tables.catalyst_lines_from_items(
        section.get("catalysts") or [],
        "\U0001F4F0 Catalysts - " + ("India (news-sourced)" if market == INDIA_MARKET else "U.S. (news-sourced)"),
    ))
    return lines


def render_telegram(report: dict) -> list[str]:
    """Telegram HTML message chunks for a collected report."""
    lines: list[str] = []
    for section in report.get("sections", []):
        lines.extend(_render_section(section))
    lines.append("")
    lines.extend(_final_summary(report))
    lines.append(f"<i>Source: {report.get('source', '')}</i>")
    return lines


def _final_summary(report: dict) -> list[str]:
    lines = ["<b>\U0001F4CB Final summary</b>", "<code>Market  Universe                     Verified/Target</code>"]
    titles = {
        "in100": "\U0001F1EE\U0001F1F3 India Nifty 100",
        "in500x": "\U0001F1EE\U0001F1F3 India Nifty 500 ex-Nifty 100",
        "inmicro": "\U0001F1EE\U0001F1F3 India Nifty Microcap 250",
        "usmega": "\U0001F1FA\U0001F1F8 U.S. Mega Cap $200B+",
        "uslarge": "\U0001F1FA\U0001F1F8 U.S. Large Cap $10B-$200B",
    }
    for section in report.get("sections", []):
        for universe in section.get("universes", []):
            name = titles.get(universe["key"], universe["key"])
            lines.append(
                f"<code>{name:<32} {universe['verified']}/{universe['target']}</code>"
            )
        if section.get("closed"):
            flag = "\U0001F1EE\U0001F1F3" if section["market"] == INDIA_MARKET else "\U0001F1FA\U0001F1F8"
            lines.append(f"<code>{flag} {'Market closed':<31} 0/20</code>")
    lines.append("")
    lines.append(
        f"<b>Total verified: {report.get('total_verified', 0)}/{report.get('total_target', 0)}</b> "
        "(target 100 = 60 India + 40 U.S.)"
    )
    lines.append(
        f"Volume Change % successfully computed for <b>{report.get('volume_computed', 0)}</b> stock(s)."
    )
    return lines


def build_report(
    markets: tuple[str, ...] = (INDIA_MARKET, US_MARKET), target_date=None,
) -> list[str]:
    """Build the full opening/closing screener as Telegram HTML message chunks.

    With ``target_date`` (datetime.date) the report describes that completed
    historical session - see collect().
    """
    return render_telegram(collect(markets, target_date))
