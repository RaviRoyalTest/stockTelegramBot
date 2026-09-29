"""Daily mail digest: snapshot + corporate actions, once per day per chat.

Opt-in per chat with /dailyemail on (needs /setemail first). Called from
the poller's per-chat loop; sends at most one mail per IST date and never
raises - every failure degrades to a skip so the poll cycle is unaffected.
"""
from __future__ import annotations

import logging

from .. import storage
from ..core.dates import today_ist
from ..core.text import escape
from .client import is_configured as email_configured
from .client import send_email

log = logging.getLogger(__name__)

_LAST_SENT_KEY = "last_daily_email"


def _money(value) -> str:
    try:
        return f"\u20b9{float(value):,.1f}"
    except (TypeError, ValueError):
        return "-"


def build_daily_lines(snapshot: dict) -> list[str]:
    """Digest lines from a snapshot doc (pure - no network, no disk)."""
    session = snapshot.get("session") or "last session"
    lines = [
        f"\U0001F4C5 <b>Daily market digest \u00b7 {escape(str(session))}</b> "
        f"({escape(str(snapshot.get('universe') or 'nifty500').upper())})",
        "",
    ]
    gaps = (snapshot.get("gap_downs") or [])[:10]
    if gaps:
        lines.append("\U0001F53B <b>Overnight gap-downs</b>")
        for row in gaps:
            lines.append(
                f"  \u2022 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"{row.get('gap_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        lines.append("")
    gainers = (snapshot.get("top_gainers") or [])[:5]
    losers = (snapshot.get("top_losers") or [])[:5]
    if gainers or losers:
        lines.append("\U0001F4C8 <b>Session movers</b>")
        for row in gainers:
            lines.append(
                f"  \U0001F7E2 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"+{row.get('change_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        for row in losers:
            lines.append(
                f"  \U0001F534 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"{row.get('change_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        lines.append("")
    actions = (snapshot.get("corporate_actions") or [])[:15]
    if actions:
        lines.append("\U0001F4CB <b>Corporate actions</b>")
        for row in actions:
            lines.append(
                f"  \u2022 <b>{escape(str(row.get('symbol') or '?'))}</b> \u2014 "
                f"{escape(str(row.get('action') or ''))} "
                f"<i>ex {escape(str(row.get('ex_date') or '?'))}</i>"
            )
        lines.append("")
    lines.append(
        "<i>From your recorded session file - open the web Sessions tab "
        "for the full tables. Manage with /dailyemail off.</i>"
    )
    return lines


def maybe_send_daily_email(chat_id) -> bool:
    """Send today's digest if due. Returns True when a mail went out."""
    try:
        settings = storage.get_user_settings(chat_id) or {}
        recipient = (settings.get("email") or "").strip()
        if not recipient or not settings.get("daily_email"):
            return False
        today = today_ist().isoformat()
        if settings.get(_LAST_SENT_KEY) == today:
            return False
        if not email_configured():
            return False
        snapshot = storage.load_snapshots() or {}
        if not snapshot.get("gap_downs") and not snapshot.get("top_gainers") \
                and not snapshot.get("corporate_actions"):
            log.info("daily mail: no snapshot recorded yet - skipping chat %s", chat_id)
            return False
        session = snapshot.get("session") or today
        ok, error = send_email(
            recipient,
            f"Royal Stock daily: {session}",
            build_daily_lines(snapshot),
        )
        if not ok:
            log.info("daily mail failed for chat %s: %s", chat_id, error)
            return False
        settings[_LAST_SENT_KEY] = today
        storage.save_user_settings(chat_id, settings)
        log.info("daily mail sent to chat %s (%s)", chat_id, session)
        return True
    except Exception as error:
        log.info("daily mail skipped for chat %s: %s", chat_id, error)
        return False
