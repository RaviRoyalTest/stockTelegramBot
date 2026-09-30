"""Opening/closing session screener command (/openreport, /closereport).

One command family, one module. Thin: it builds the report via the
opening_report package and sends it chunked. The heavy lifting (universes,
regular-session fetches, table rendering) lives in corporate_actions.opening_report.
"""
from __future__ import annotations

import logging

from ..core.dates import parse_date_token
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
    A market token is only honoured when it is the FIRST argument; anything
    later (e.g. the date in /openreport 18-09-2026 in) is left to the caller.
    """
    if len(parts) > 1:
        token = normalise_market(parts[1])
        if token in ("in", "us") and parts[1].lower() in ("in", "us", "india", "united states"):
            return (token,)
    return ("in", "us")


def _requested_date(parts):
    """Optional historical date from the command args (datetime.date or None).

    Accepts the repo's standard flexible tokens (18-09-2026, 2026-09-18,
    yesterday, 18sep, ...). 'yesterday'/'yday' resolve to the day before the
    bot's local today. Anything unparseable raises ValueError so the caller
    can show usage instead of silently reporting the wrong day.
    """
    for arg in parts[1:]:
        lowered = arg.lower().strip()
        if lowered in ("yesterday", "yday", "yd"):
            from ..market.hours import local_now
            import datetime as _dt

            return local_now("in").date() - _dt.timedelta(days=1)
        if lowered in ("in", "us", "india", "united states", "auto", "all"):
            continue
        parsed = parse_date_token(arg)
        if parsed is None and arg.strip() and not arg.startswith("/"):
            raise ValueError(arg)
        if parsed is not None:
            return parsed
    return None


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
    try:
        target_date = _requested_date(parts)
    except ValueError as bad:
        reply(
            chat_id,
            f"\u274c Could not read the date <code>{bad.args[0]}</code>. "
            "Try <code>/openreport 18-09-2026</code>, <code>/openreport yesterday</code>, "
            "or <code>/openreport in 18-09-2026</code>.",
        )
        return
    markets = _requested_markets(parts)
    labels = ", ".join(
        f"{'India' if market == 'in' else 'US'} "
        f"({'OPEN' if is_market_open(market) else 'CLOSED'})"
        for market in markets
    )
    if target_date is not None:
        head = (
            "\U0001F4DC <b>Building the historical session report</b>\n"
            f"Date: <b>{target_date.strftime('%d-%b-%Y')}</b> \u00b7 Markets: {labels}\n"
            "Completed regular-session closes only - stocks that did not trade "
            "that day are reported unavailable, never ranked against another "
            "session. This takes about a minute."
        )
    else:
        head = (
            "\U0001F680 <b>Building the opening/closing session screener</b>\n"
            f"Markets: {labels}\n"
            "Fetching regular-session price &amp; volume across the official "
            "universes (Nifty 100 / Nifty 500 ex-100 / Nifty Microcap 250 and the "
            "US Mega/Large-cap sets). This takes about a minute - the report "
            "arrives in this chat when ready."
        )
    reply(chat_id, head)
    try:
        # Single fetch shared by the shown report and the recorded file, so
        # the saved open+close details can never disagree with this chat.
        # Live AND historical builds are saved under their session date, so
        # /openmarket (and the web page) can replay them instantly later -
        # built once, reused forever.
        from ..opening_report import collect_and_render
        from ..opening_report.report import save_openclose_doc

        lines, report = collect_and_render(markets, target_date)
        try:
            save_openclose_doc(report, markets, target_date, recorded_by="telegram")
        except Exception as error:
            log.info("openclose record skipped: %s", error)
    except Exception as error:
        log.warning("opening report failed: %s", error, exc_info=True)
        reply(chat_id, f"Could not build the report: {error}. Please try again shortly.")
        return
    reply_messages(chat_id, split_messages(lines))
    log.info(
        "opening/closing report sent for chat %s (%d market(s), date=%s)",
        chat_id, len(markets), target_date or "live",
    )


def handle_openmarket(chat_id, parts) -> None:
    """Instant open/close details from the recorded file (/openmarket).

    The heavy scan runs once (via /openmarket now, /openreport, the schedule
    or the web) and is saved under its session date; this command replays the
    saved file instantly - no refetch, no wait. Only an explicit
    "/openmarket now" spends the ~1-minute live scan.

    /openmarket            -> latest recorded report (both markets)
    /openmarket now        -> fresh live scan, both markets (~1 min), saved
    /openmarket in | us    -> latest recorded, that market only
    /openmarket 18-09-2026 -> that session's recorded report (if built before)
    """
    from .. import storage as _storage

    if len(parts) > 1 and parts[1].lower() in ("now", "fresh", "run", "update", "new"):
        handle_opening_report(chat_id, ["/openreport", *parts[2:]])
        return
    market = None
    date_token = None
    for arg in parts[1:]:
        lowered = arg.lower().strip()
        if lowered in ("in", "india"):
            market = "in"
        elif lowered in ("us", "united states"):
            market = "us"
        elif lowered in ("yesterday", "yday", "yd"):
            from ..market.hours import local_now
            import datetime as _dt

            date_token = (local_now("in").date() - _dt.timedelta(days=1)).isoformat()
        else:
            parsed = parse_date_token(arg)
            if parsed is not None:
                date_token = parsed.isoformat()
    try:
        doc = _storage.load_openclose(date_token) or {}
    except Exception:
        doc = {}
    report = doc.get("report") if isinstance(doc.get("report"), dict) else {}
    sections = report.get("sections") or []
    if not sections:
        if date_token:
            reply(
                chat_id,
                f"\U0001F4C1 No recorded session for <b>{date_token}</b> yet.\n"
                f"Build it once with <code>/openreport {date_token}</code> (~1 min) - "
                "after that <code>/openmarket</code> replays it instantly.",
            )
        else:
            reply(
                chat_id,
                "\U0001F4C1 <b>No recorded open/close report yet.</b>\n"
                "Run <code>/openmarket now</code> (or <code>/openreport</code>) once - "
                "about a minute - and every <code>/openmarket</code> after that "
                "shows the saved details instantly.",
            )
        return
    if market:
        sections = [s for s in sections if s.get("market") == market]
        if not sections:
            reply(
                chat_id,
                f"The recorded report covers only: "
                f"{', '.join(sorted({s.get('market') for s in report.get('sections') or []}))}. "
                f"Run <code>/openmarket now {'us' if market == 'us' else 'in'}</code> to add it.",
            )
            return
    shown = dict(report)
    shown["sections"] = sections
    # Recompute the summary for the shown subset (the stored totals cover
    # every market in the file, not necessarily the requested slice).
    shown["total_verified"] = sum(
        u.get("verified", 0) for s in sections for u in s.get("universes", [])
    )
    shown["total_target"] = sum(
        u.get("target", 0) for s in sections for u in s.get("universes", [])
    )
    shown["volume_computed"] = sum(s.get("volume_computed", 0) for s in sections)
    scope = ("India" if market == "in" else "US") if market else "India + US"
    head = (
        "\U0001F4D6 <b>Recorded open/close report</b>\n"
        f"{scope} \u00b7 recorded <b>{doc.get('recorded_at') or '?'}</b> "
        f"by {doc.get('recorded_by') or '?'}\n"
        "Served instantly from the saved file - "
        "<code>/openmarket now</code> re-scans fresh."
    )
    reply(chat_id, head)
    from ..opening_report import report as openreport

    reply_messages(chat_id, split_messages(openreport.render_telegram(shown)))
    log.info(
        "recorded open/close report sent for chat %s (market=%s, date=%s)",
        chat_id, market or "all", date_token or "latest",
    )


def market_session_note() -> str:
    """Short OPEN/CLOSED line for both markets in their own wall clocks."""
    return (
        f"India: <b>{'OPEN' if is_market_open('in') else 'CLOSED'}</b> "
        f"({market_tz_tag('in')}) \u00b7 "
        f"US: <b>{'OPEN' if is_market_open('us') else 'CLOSED'}</b> "
        f"({market_tz_tag('us')})"
    )


# Daily open + close anchors for each market (times are in that market's own
# wall clock: IST for India, ET for the US). Deliberately NO window keys: an
# explicit window makes the scheduler build its own grid from the window
# edges and IGNORE run_at, so the reports would fire at the wrong times.
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
        # No window keys here on purpose: a window_start/window_end pair makes
        # the scheduler build its own grid and IGNORE run_at (see _DAILY_PLANS).
        storage.add_schedule_entry(
            1440,
            [plan["command"]],
            str(chat_id),
            run_at=plan["run_at"],
            market=plan["market"],
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