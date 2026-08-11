"""On-demand corporate-action queries across ALL NSE + BSE stocks.

Every query fetches the live feed and filters it in memory, so the results
are always fresh. Replies are split into Telegram-sized chunks.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from time import monotonic

from .. import config
from ..core.text import escape, split_messages
from ..formatting import format_action_detail, format_action_entry
from ..poller.events import parse_ex_date
from ..poller.fetchers import fetch_all_actions
from ..sources.types import INCREASE_TYPES, TYPE_LABELS, action_type
from .helpers import MAX_QUERY_ITEMS, attach_quotes, close_symbols
from .help_texts import CA_HELP
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def _norm_type(token: str) -> str | None:
    """Normalize an action-type token (handles plurals like 'splits')."""
    from ..sources.types import ACTION_TYPES

    type_name = token.strip().lower()
    if type_name in ACTION_TYPES:
        return type_name
    if type_name.endswith("s") and type_name[:-1] in ACTION_TYPES:
        return type_name[:-1]
    return None


def parse_ca_arg(argument: str) -> dict | None:
    """Map one /ca argument to a query descriptor, or None when unclear."""
    raw = (argument or "").strip()
    token = raw.lower()
    if not raw:
        return None
    if token in ("increase", "shareholder", "shareholders", "shares", "share-holder"):
        return {"mode": "types", "types": list(INCREASE_TYPES)}
    if (type_name := _norm_type(token)):
        return {"mode": "types", "types": [type_name]}
    if token in ("all", "list", "overview", "everything"):
        return {"mode": "overview"}
    if token in ("today", "tomorrow"):
        return {"mode": "exdate", "days": 0}
    try:
        return {"mode": "exdate", "days": max(0, int(token))}
    except ValueError:
        pass
    if "," in raw:  # e.g. /corpactions dividend,bonus
        wanted = [type_name for part in token.split(",") if (type_name := _norm_type(part))]
        if wanted:
            return {"mode": "types", "types": wanted}
    return {"mode": "term", "term": raw}


def _footnote(warnings: list, errors: list) -> list[str]:
    """BSE/network warnings shown as a note on overview queries."""
    notes = [note for note in (warnings or [])] + [note for note in (errors or [])]
    return [f"\u26a0\ufe0f {note}" for note in notes if note]


def _symbol_query(chat_id, term: str, all_actions: list[dict]) -> bool:
    """Handle the exact-symbol query path of run_ca_query.

    Fetches the symbol's full NSE history, merges any BSE rows from the global
    fetch, and replies with detail blocks. Returns True when handled.
    """
    from ..sources.nse import get_nse_corporate_actions

    symbol_matches = []
    try:
        nse_actions = get_nse_corporate_actions(symbol=term.upper())
        for action in nse_actions:
            action["exchange"] = "NSE"
        symbol_matches.extend(nse_actions)
    except Exception as error:
        log.info("Failed to fetch NSE corporate actions for %s: %s", term, error)

    # Include matching symbols from the fetched global actions (e.g. BSE)
    for action in all_actions:
        if (action.get("symbol") or "").upper() == term.upper():
            # Avoid duplicates
            if not any(
                search_action.get("exchange") == action.get("exchange")
                and search_action.get("subject") == action.get("subject")
                and search_action.get("ex_date") == action.get("ex_date")
                for search_action in symbol_matches
            ):
                symbol_matches.append(action)

    if not symbol_matches:
        return False
    attach_quotes(symbol_matches)
    messages = [f"<b>Corporate actions for {escape(term.upper())}</b>"]
    for action in sorted(
        symbol_matches, key=lambda item: item.get("ex_date") or "9999-99-99", reverse=True
    ):
        messages.append(format_action_detail(action))
    reply_messages(chat_id, messages)
    return True


def run_ca_query(chat_id, descriptor: dict) -> bool:
    """Fetch all NSE+BSE actions, filter per descriptor, and reply."""
    log.info("ca_query: chat %s mode=%s", chat_id, descriptor.get("mode"))
    started_at = monotonic()
    try:
        all_actions, errors, warnings = fetch_all_actions()
    except Exception as error:
        reply(chat_id, f"Could not fetch corporate actions: {config.redact(error)}")
        return True
    log.info(
        "ca_query: fetched %d corporate action(s) in %.1fs (errors=%d, warnings=%d)",
        len(all_actions), monotonic() - started_at, len(errors), len(warnings),
    )
    if not all_actions:
        note = "\n" + "\n".join(_footnote(warnings, errors)) if (warnings or errors) else ""
        reply(chat_id, "No corporate actions found right now." + note)
        return True

    mode = descriptor.get("mode")
    title = "<b>Corporate Actions</b> (all NSE + BSE)"
    results = None

    if mode == "overview":
        by_exchange, by_type = {}, {}
        for action in all_actions:
            exchange = action.get("exchange") or "?"
            by_exchange[exchange] = by_exchange.get(exchange, 0) + 1
            type_name = action_type(action.get("subject"))
            by_type[type_name] = by_type.get(type_name, 0) + 1
        lines = [title]
        lines.append("Count by exchange: " + " | ".join(f"{exchange}: {count}" for exchange, count in by_exchange.items()))
        type_summary = ", ".join(
            f"{TYPE_LABELS.get(type_name, type_name)} {by_type.get(type_name, 0)}"
            for type_name in ("dividend", "bonus", "split", "rights", "buyback", "other")
            if by_type.get(type_name)
        )
        lines.append("Count by type: " + (type_summary or "none"))
        dated = sorted(
            (action for action in all_actions if parse_ex_date(action.get("ex_date"))),
            key=lambda action: action.get("ex_date"),
        )
        if dated:
            lines.append("\n<b>Next ex-dates:</b>")
            attach_quotes(dated[:15])
            for action in dated[:15]:
                lines.append(format_action_entry(action))
        else:
            lines.append("\nNo ex-dates in the current feed.")
        messages = split_messages(lines)
        if warnings or errors:
            messages.append("\n".join(_footnote(warnings, errors)))
        reply_messages(chat_id, messages)
        return True

    if mode == "types":
        wanted = set(descriptor.get("types") or [])
        if len(wanted) == 1:
            label = TYPE_LABELS.get(next(iter(wanted)), "Action")
        else:
            label = " + ".join(TYPE_LABELS.get(type_name, type_name) for type_name in wanted)
        title = f"<b>{label} actions</b> (NSE + BSE)"
        results = [action for action in all_actions if action_type(action.get("subject")) in wanted]

    elif mode == "exdate":
        days = int(descriptor.get("days", config.REMINDER_DAYS))
        today = config.today_ist()
        cutoff = today + timedelta(days=days)
        results = [
            action for action in all_actions
            if (ex_date := parse_ex_date(action.get("ex_date"))) and today <= ex_date <= cutoff
        ]
        label = "today" if days == 0 else f"within {days} day(s)"
        title = f"<b>Ex-date {label}</b> (NSE + BSE)"

    else:  # mode == "term": exact symbol first, then keyword search
        term = descriptor.get("term", "").strip()
        if _symbol_query(chat_id, term, all_actions):
            return True
        query = term.lower()
        results = [
            action for action in all_actions
            if query in (action.get("company") or "").lower()
            or query in (action.get("subject") or "").lower()
        ]
        if not results:
            close = close_symbols(term)
            if close:
                lines = [
                    f"No corporate actions match '{escape(term)}'. "
                    "Did you mean (NSE):"
                ]
                lines += [f"  /ca {close_match}" for close_match in close]
                reply(chat_id, "\n".join(lines))
                return True
        title = f"<b>Search results for '{escape(term)}'</b> (NSE + BSE)"

    if not results:
        reply(chat_id, f"No corporate actions match this query.\n\n{CA_HELP}")
        return True

    ordered = sorted(
        results,
        key=lambda action: (action.get("ex_date") or "9999-99-99", (action.get("symbol") or "").upper()),
    )
    shown = ordered[:MAX_QUERY_ITEMS]
    attach_quotes(shown)
    lines = [title, f"{len(ordered)} action(s) found."]
    for action in shown:
        lines.append(format_action_entry(action))
    if len(ordered) > MAX_QUERY_ITEMS:
        lines.append(
            f"... and {len(ordered) - MAX_QUERY_ITEMS} more (limit "
            f"{MAX_QUERY_ITEMS}). Narrow it down with /corpactions dividend, "
            "/corpactions 7, or /corpactions SYMBOL."
        )
    reply_messages(chat_id, split_messages(lines))
    return True
