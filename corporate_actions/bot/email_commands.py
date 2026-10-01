"""Email (mail-id) commands: /setemail + /emailreport + /dailyemail.

/setemail you@example.com  -> store the address + send a test mail
/setemail off               -> forget the address
/emailreport RELIANCE       -> mail the deep fundamental report
/dailyemail on|off          -> daily snapshot digest to the mail id
"""
from __future__ import annotations

import logging
import re

from .. import storage
from ..core.text import escape, split_messages
from ..email import GMAIL_SETUP_GUIDE
from ..email import is_configured as email_configured
from ..email import parse_recipients, send_custom, send_email
from ..formatting.stock_india import _fund_report_lines
from ..formatting.stock_us import _us_stock_lines
from .fundamentals_commands import _resolve_fund, _resolve_quote
from .helpers import reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EMAIL_USAGE = (
    "<b>/setemail</b> - get bot reports in your mailbox\n"
    "/setemail you@gmail.com  \u2192 save + test-mail the address\n"
    "/setemail off             \u2192 forget the address\n"
    "<b>/emailreport SYMBOL [to@mail] [custom subject...]</b> - mail the deep report\n"
    "/emailreport RELIANCE  \u2192 full report to your saved mail id\n"
    "/emailreport RELIANCE friend@gmail.com  \u2192 same report to a friend\n"
    "<b>/email to@mail | subject | message</b> - send any custom mail\n"
    "/email friend@gmail.com | Hello | My watchlist is up 2% today\n"
    "<b>/dailyemail on|off</b> - daily snapshot digest to your mail id\n"
    "Gmail users: it needs an App Password (not the login password) - "
    "see /emailhelp. Easiest: admin sets RESEND_API_KEY once."
)


def _server_not_ready_text() -> str:
    lines = [
        "The server has no email sender configured yet, so I cannot send mail.",
        "Admin fix (pick ONE):",
        "1. Easiest - set RESEND_API_KEY from resend.com (free, no passwords).",
        "2. Gmail - " + GMAIL_SETUP_GUIDE.replace("\n", " "),
    ]
    return "\n".join(lines)


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
        reply(chat_id, _server_not_ready_text())
        return
    settings["email"] = raw
    storage.save_user_settings(chat_id, settings)
    ok, info = send_email(
        raw,
        "Royal Stock: test mail ✅",
        [
            "✅ <b>Test mail OK</b> - reports will arrive here.",
            "",
            "Customise every mail from here:<br>"
            "• <code>/emailreport RELIANCE</code> - deep report to this address<br>"
            "• <code>/emailreport RELIANCE friend@gmail.com My title</code> - same report elsewhere with your subject<br>"
            "• <code>/email friend@gmail.com | Subject | your message</code> - any custom note<br>"
            "• <code>/dailyemail on</code> - automatic daily digest",
        ],
    )
    if ok:
        # info may carry "resend id: ..." - point the user at the resend.com
        # dashboard and the spam folder, the two places a "sent" mail hides.
        extra = f" ({escape(info)})" if info else ""
        reply(
            chat_id,
            f"Mail id saved as <b>{escape(raw)}</b> - test mail accepted by the sender. ✅{extra}\n"
            "Not in your inbox in ~2 min? Check <b>Spam</b> (sender "
            "<code>onboarding@resend.dev</code>) and the Emails tab on "
            "resend.com for that message's delivery status.",
        )
    else:
        reply(chat_id, f"Mail id saved as <b>{escape(raw)}</b>, but the test mail failed: {escape(info)}")


def handle_emailreport(chat_id, parts) -> None:
    """Mail the deep fundamental report for one symbol.

    Customizable: /emailreport SYMBOL [to@mail, ...] [subject words...].
    Without extras it goes to the saved /setemail address with the default
    subject; with extras the user picks the destination + subject per mail.
    """
    settings = storage.get_user_settings(chat_id) or {}
    saved = (settings.get("email") or "").strip()
    if len(parts) < 2:
        reply(
            chat_id,
            "Usage: <code>/emailreport SYMBOL [to@mail] [custom subject]</code><br>"
            "e.g. <code>/emailreport RELIANCE</code> or "
            "<code>/emailreport RELIANCE friend@gmail.com My view</code>",
        )
        return
    if not email_configured():
        reply(chat_id, _server_not_ready_text())
        return
    raw_symbol = parts[1].upper().strip().removesuffix(".NS").removesuffix(".BO")
    # Optional override: first extra token that looks like a mail id (or a
    # comma list) becomes the destination; the rest becomes a custom subject.
    recipient = saved
    custom_subject = ""
    if len(parts) >= 3:
        maybe_to = " ".join(parts[2:]).split("|")[0]
        valid, invalid = parse_recipients(maybe_to.replace(" ", ","))
        if valid:
            recipient = ", ".join(valid)
            # Anything after the address tokens is the custom subject.
            remainder = " ".join(parts[2:])
            for addr in valid:
                remainder = remainder.replace(addr, " ")
            remainder = remainder.replace(",", " ").strip(" |")
            custom_subject = " ".join(remainder.split())
        else:
            # No address given - the whole tail is a custom subject.
            custom_subject = " ".join(parts[2:]).strip()
            if invalid and not saved:
                reply(chat_id, f"<code>{escape(parts[2])}</code> is not a valid mail id.")
                return
    if not recipient:
        reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
        return
    subject = custom_subject or f"Royal Stock report: {raw_symbol}"
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
    if custom_subject:
        lines = [f"<i>{escape(custom_subject)}</i>", ""] + lines
    # Recipient is the user's stored mail id (or their explicit override) -
    # never the symbol (a past bug mailed "RELIANCE" instead of the user).
    ok, info = send_email(recipient, subject, lines)
    if ok:
        extra = f"<br><i>{escape(info)}</i>" if info and "resend id" in info.lower() else ""
        reply_messages(chat_id, split_messages(
            [f"📧 Mailed <b>{escape(raw_symbol)}</b> to <b>{escape(recipient)}</b> ✅{extra}"]
        ))
    else:
        reply(chat_id, f"📧 Mail failed: {escape(info)}")


def handle_emailsend(chat_id, parts, raw_text: str = "") -> None:
    """Send any custom mail: /email to@mail | subject | message.

    Separators `|` (or `;` for the first split) keep subject/body apart.
    Subject is optional - only `to` + message are required.
    """
    if not email_configured():
        reply(chat_id, _server_not_ready_text())
        return
    body = (raw_text or "").strip()
    # Strip the command token itself ("/email", "/emailsend", "/mail", ...).
    if body.startswith("/"):
        body = body.split(None, 1)[1] if len(body.split(None, 1)) > 1 else ""
    chunks = [chunk.strip() for chunk in body.replace(";", "|").split("|")]
    chunks = [chunk for chunk in (chunks or []) if chunk != ""]
    if len(chunks) < 2:
        reply(
            chat_id,
            "Usage: <code>/email to@mail | subject (optional) | your message</code><br>"
            "e.g. <code>/email friend@gmail.com | Watchlist | RELIANCE looks strong today</code>",
        )
        return
    to_part = chunks[0]
    if len(chunks) == 2:
        subject, message = "Royal Stock note", chunks[1]
    else:
        subject, message = chunks[1] or "Royal Stock note", " ".join(chunks[2:])
    recipients, invalid = parse_recipients(to_part)
    if invalid:
        reply(chat_id, f"<code>{escape(invalid[0])}</code> is not a valid mail id.")
        return
    if not recipients:
        reply(chat_id, "Give at least one destination mail id.")
        return
    if len(message) > 20000:
        reply(chat_id, "Message too long (max 20000 characters).")
        return
    reply(chat_id, f"📧 Sending your note to <b>{escape(', '.join(recipients))}</b>...")
    ok, info = send_custom(", ".join(recipients), subject, message)
    if ok:
        extra = f"<br><i>{escape(info)}</i>" if info and "resend id" in info.lower() else ""
        reply(chat_id, f"📧 Sent to <b>{escape(', '.join(recipients))}</b> ✅{extra}")
    else:
        reply(chat_id, f"📧 Mail failed: {escape(info)}")


def handle_emailhelp(chat_id, _parts=None) -> None:
    """Gmail App-Password walkthrough + all mail commands in one place."""
    reply(
        chat_id,
        "<b>📧 Email help</b>\n"
        + EMAIL_USAGE
        + "\n\n<b>Gmail App Password (must-do for @gmail senders):</b>\n"
        + escape(GMAIL_SETUP_GUIDE),
    )


def handle_dailyemail(chat_id, parts) -> None:
    """Toggle the everyday snapshot digest (/dailyemail on|off)."""
    settings = storage.get_user_settings(chat_id) or {}
    if len(parts) < 2:
        state = "ON" if settings.get("daily_email") else "OFF"
        reply(
            chat_id,
            f"Daily mail digest: <b>{state}</b>\n"
            "Usage: <code>/dailyemail on</code> (needs <code>/setemail</code> first) "
            "or <code>/dailyemail off</code>",
        )
        return
    raw = parts[1].lower()
    if raw in ("on", "enable", "start", "yes"):
        if not (settings.get("email") or "").strip():
            reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
            return
        settings["daily_email"] = True
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            f"📧 Daily digest <b>ON</b> - the recorded session (gap-downs, "
            f"movers, actions) lands in <b>{escape(settings['email'])}</b> every day.",
        )
    elif raw in ("off", "disable", "stop", "no"):
        settings["daily_email"] = False
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "📧 Daily digest <b>OFF</b>.")
    else:
        reply(chat_id, "Usage: <code>/dailyemail on</code> or <code>/dailyemail off</code>")
