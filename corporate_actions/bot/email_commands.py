"""Email (mail-id) commands: /setemail + /emailreport.

/setemail you@example.com  -> store the address + send a test mail
/setemail off               -> forget the address
/emailreport RELIANCE       -> mail the deep fundamental report
"""
from __future__ import annotations

import logging
import re

from .. import storage
from ..core.text import escape, split_messages
from ..email import is_configured as email_configured
from ..email import send_email
from ..formatting.stock_india import _fund_report_lines
from ..formatting.stock_us import _us_stock_lines
from .fundamentals_commands import _resolve_fund, _resolve_quote
from .helpers import reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EMAIL_USAGE = (
    "<b>/setemail</b> - get bot reports in your mailbox\n"
    "/setemail you@example.com  \u2192 save + test-mail the address\n"
    "/setemail off              \u2192 forget the address\n"
    "<b>/emailreport SYMBOL</b> - mail the deep fundamental report\n"
    "/emailreport RELIANCE  \u2192 full report + snapshot in your inbox\n"
    "Needs server SMTP settings (ask the admin for SMTP_HOST/USER/PASS)."
)


def handle_setemail(chat_id, parts) -> None:
    """Store/clear the chat's mail id, with an immediate test mail."""
    settings = storage.get_user_settings(chat_id) or {}
    if len(parts) < 2:
        current = settings.get("email")
        reply(
            chat_id,
            (f"Mail id: <b>{escape(current)}</b>\n" if current else "No mail id set.\n")
            + "Usage: <code>/setemail you@example.com</code> or <code>/setemail off</code>",
        )
        return
    raw = parts[1].strip().lower()
    if raw in ("off", "none", "clear", "remove"):
        settings.pop("email", None)
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "Mail id removed - I will only reply here now.")
        return
    if not _EMAIL_RE.match(raw):
        reply(chat_id, f"<code>{escape(parts[1])}</code> is not a valid mail id.")
        return
    if not email_configured():
        reply(
            chat_id,
            "The server has no SMTP settings yet, so I cannot send mail. "
            "Ask the admin to set SMTP_HOST / SMTP_USER / SMTP_PASS, then retry.",
        )
        return
    settings["email"] = raw
    storage.save_user_settings(chat_id, settings)
    ok, error = send_email(raw, "Royal Stock: test mail", ["✅ <b>Test mail OK</b> - reports will arrive here."])
    if ok:
        reply(chat_id, f"Mail id saved as <b>{escape(raw)}</b> - test mail sent. ✅")
    else:
        reply(chat_id, f"Mail id saved as <b>{escape(raw)}</b>, but the test mail failed: {escape(error)}")


def handle_emailreport(chat_id, parts) -> None:
    """Mail the deep fundamental report for one symbol."""
    settings = storage.get_user_settings(chat_id) or {}
    recipient = (settings.get("email") or "").strip()
    if not recipient:
        reply(chat_id, "Set your mail id first: <code>/setemail you@example.com</code>")
        return
    if len(parts) < 2:
        reply(chat_id, "Usage: <code>/emailreport SYMBOL</code> (e.g. <code>/emailreport RELIANCE</code>)")
        return
    if not email_configured():
        reply(chat_id, "The server has no SMTP settings yet - ask the admin to configure them.")
        return
    raw_symbol = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    reply(chat_id, f"Preparing <b>{escape(raw_symbol)}</b> - mailing it to <b>{escape(recipient)}</b> shortly.")
    quote, is_us = _resolve_quote(raw_symbol)
    fund = _resolve_fund(raw_symbol, is_us, quote)
    if quote.get("price") is None and not fund:
        reply_suggestions(chat_id, raw_symbol, "fundamentalreport")
        return
    if is_us:
        lines = _us_stock_lines(raw_symbol, quote, fund, include_tip=False)
    else:
        lines = _fund_report_lines(raw_symbol, quote, fund, include_tip=False)
    ok, error = send_email(raw_symbol, f"Royal Stock report: {raw_symbol}", lines)
    if ok:
        reply_messages(chat_id, split_messages(
            [f"📧 Mailed <b>{escape(raw_symbol)}</b> to <b>{escape(recipient)}</b> ✅"]
        ))
    else:
        reply(chat_id, f"📧 Mail failed: {escape(error)}")
