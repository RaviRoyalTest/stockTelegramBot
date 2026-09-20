"""Opening/closing session screener command (/openreport, /closereport).

One command family, one module. Thin: it builds the report via the
opening_report package and sends it chunked. The heavy lifting (universes,
regular-session fetches, table rendering) lives in corporate_actions.opening_report.
"""
from __future__ import annotations

import logging

from ..core.text import split_messages
from ..market.hours import is_market_open, market_tz_tag, normalise_market
from ..opening_report import build_report
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def _requested_markets(parts) -> tuple[str, ...]:
    """Which markets to report on: default both, or a single one if asked.

    /openreport in   -> India only
    /openreport us   -> US only
    /openreport      -> both (each marked OPEN/CLOSED independently)
    """
    if len(parts) > 1:
        token = normalise_market(parts[1])
        if token == "in":
            return ("in",)
        if token == "us":
            return ("us",)
    return ("in", "us")


def handle_opening_report(chat_id, parts) -> None:
    """Build and send the opening/closing session screener.

    /openreport         -> both markets (India + US), each marked OPEN/CLOSED
    /openreport in|us   -> one market
    /openreport auto on|off -> install/remove the daily open + close reports

    Regular-session data only. Whichever market is closed on a weekend or
    exchange holiday is reported as MARKET CLOSED with its next session -
    stale data is never dressed up as today's opening report.
    """
    if len(parts) > 1 and parts[1].lower() in ("auto", "schedule", "daily", "everyday"):
        handle_openreport_auto(chat_id, parts)
        return
    markets = _requested_markets(parts)
    labels = ", ".join(
        f"{'India' if market == 'in' else 'US'} "
        f"({'OPEN' if is_market_open(market) else 'CLOSED'})"
        for market in markets
    )
    reply(
        chat_id,
        "\U0001F680 <b>Building the opening/closing session screener</b>\n"
        f"Markets: {labels}\n"
        "Fetching regular-session price &amp; volume across the official "
        "universes (Nifty 100 / Nifty 500 ex-100 / Nifty Microcap 250 and the "
        "US Mega/Large-cap sets). This takes about a minute - the report "
        "arrives in this chat when ready.",
    )
    try:
        lines = build_report(markets)
    except Exception as error:
        log.warning("opening report failed: %s", error, exc_info=True)
        reply(chat_id, f"Could not build the report: {error}. Please try again shortly.")
        return
    reply_messages(chat_id, split_messages(lines))
    log.info("opening/closing report sent for chat %s (%d market(s))", chat_id, len(markets))


def market_session_note() -> str:
    """Short OPEN/CLOSED line for both markets in their own wall clocks."""
    return (
        f"India: <b>{'OPEN' if is_market_open('in') else 'CLOSED'}</b> "
        f"({market_tz_tag('in')}) \u00b7 "
        f"US: <b>{'OPEN' if is_market_open('us') else 'CLOSED'}</b> "
        f"({market_tz_tag('us')})"
    )


# Daily open + close anchors for each market (times are in that market's own
# wall clock: IST for India, ET for the US). The explicit run windows keep the
# close-of-session run alive even though it fires just after the bell, and stop
# any other time of day from firing the report.
_DAILY_PLANS = (
    # No explicit window: run_at wins. (A window_start/window_end pair makes
    # the scheduler build its own grid from the window edges and IGNORE these
    # clock times; without one, run_at fires exactly at the listed times and
    # the scheduler's 10-minute anchor grace lets the close reports - fired a
    # few minutes after the 15:30/16:00 bell - through the market-hours gate.)
    {
        "command": "/openreport in",
        "market": "in",
        "run_at": "10:00,15:35",
        "label": "India open (10:00 IST) + close (15:35 IST)",
    },
    {
        "command": "/openreport us",
        "market": "us",
        "run_at": "09:40,16:05",
        "label": "US open (09:40 ET) + close (16:05 ET)",
    },
)


def _own_entry_indices(chat_id, commands_of_interest: set) -> list[int]:
    """Indices (in the chat's own entry list) of opening-report schedule rows."""
    from .. import storage

    indices = []
    for index, entry in enumerate(storage.load_schedule_for(chat_id)):
        commands = [command for command in entry.get("commands") or [] if command.strip()]
        if commands and commands[0].lower().split()[0] in commands_of_interest:
            indices.append(index)
    return indices


def _remove_auto_entries(chat_id) -> int:
    """Remove this chat's opening-report schedule rows; returns how many went."""
    from .. import storage

    indices = _own_entry_indices(chat_id, {"/openreport", "/closereport", "/sessionreport"})
    removed = 0
    for index in sorted(indices, reverse=True):  # reverse keeps indices valid
        storage.remove_schedule_entry(chat_id, index)
        removed += 1
    return removed


def handle_openreport_auto(chat_id, parts) -> None:
    """Install/remove the daily open+close Telegram reports (/openreport auto)."""
    from .. import storage

    sub = parts[2].lower() if len(parts) > 2 else "on"
    if sub in ("off", "stop", "remove", "disable", "clear"):
        removed = _remove_auto_entries(chat_id)
        reply(
            chat_id,
            f"\U0001F515 <b>Daily open/close reports removed</b> ({removed} schedule "
            "entry(s) deleted).\nSend <code>/openreport auto</code> to turn them back on.",
        )
        return
    if sub not in ("on", "start", "enable", "add", "yes"):
        reply(
            chat_id,
            "Usage: <code>/openreport auto on</code> or "
            "<code>/openreport auto off</code>.",
        )
        return
    _remove_auto_entries(chat_id)  # idempotent - never stack duplicates
    for plan in _DAILY_PLANS:
        storage.add_schedule_entry(
            1440,
            [plan["command"]],
            str(chat_id),
            run_at=plan["run_at"],
            market=plan["market"],
            window_start=plan["window_start"],
            window_end=plan["window_end"],
        )
    lines = [
        "\U0001F514 <b>Daily open + close reports ENABLED</b>",
        "Every trading day, automatically:",
    ]
    lines.extend(f"  \u2022 {plan['label']}" for plan in _DAILY_PLANS)
    lines.extend([
        "",
        "Weekends and exchange holidays send a MARKET CLOSED notice with the "
        "next session instead of a stale report.",
        f"Markets right now \u2014 {market_session_note()}",
        "Turn off anytime with <code>/openreport auto off</code>.",
    ])
    reply(chat_id, "\n".join(lines))
    log.info("chat %s enabled daily open/close reports", chat_id)