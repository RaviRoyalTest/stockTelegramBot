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
from .helpers import has_stock_data, reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Single source of truth for the email command family. Every surface that
# lists mail commands (the bare /email hub, /emailhelp, the README, the web
# dashboard) draws from this so they never drift apart.
EMAIL_COMMANDS_OVERVIEW = [
    "<b>/setemail you@gmail.com</b> \u2014 save your address + send a test mail (\u201coff\u201d forgets it).",
    "<b>/email to | subject | message</b> \u2014 send any custom note. Aliases: /emailsend, /sendmail, /mail",
    "<b>/emailreport SYMBOL [to] [subject]</b> \u2014 mail the deep fundamental report. Alias: /emailfund",
    "<b>/dailyemail on|both|open|close|scheduled|off</b> \u2014 automatic opening/closing/EOD mails. Aliases: /dailymail, /emaildaily",
    "<b>/emailsections [add|remove|all ...]</b> \u2014 choose WHAT each mail contains. Aliases: /emailsection, /mailsections, /mailcontent",
    "<b>/emailopen [section]</b> \u2014 morning mail now (whole digest or one section). Aliases: /openmail, /morningmail",
    "<b>/emailclose</b> \u2014 closing screener + EOD stores mail now. Aliases: /closemail, /eodmail, /eod",
    "<b>/emailboth</b> \u2014 open + close + EOD in ONE mail. Aliases: /bothmail, /emailfull, /sessionmail",
    "<b>/emailstatus</b> \u2014 last sends: \u2705 success or \u274c failed + reason. Aliases: /maillog, /mailstatus",
    "<b>/emailhelp</b> \u2014 this Gmail/SMTP setup walkthrough",
]

EMAIL_USAGE = (
    "<b>\U0001F4E7 Email commands</b> \u2014 type <code>/email</code> for your live mail setup + this full list.\n"
    + "\n".join("\u2022 " + line for line in EMAIL_COMMANDS_OVERVIEW)
    + "\n\nGmail users: it needs an App Password (not the login password) - "
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
        kind="test", chat_id=chat_id,
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
    if not has_stock_data(raw_symbol, quote, fund):
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
    ok, info = send_email(recipient, subject, lines, kind="report", chat_id=chat_id)
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
    ok, info = send_custom(", ".join(recipients), subject, message, kind="custom", chat_id=chat_id)
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
    """Toggle the everyday mails: opening screener + closing/EOD stores."""
    from ..email.daily import get_scope

    settings = storage.get_user_settings(chat_id) or {}
    if len(parts) < 2:
        scope = get_scope(settings)
        state = "OFF" if scope == "off" else f"ON ({scope})"
        reply(
            chat_id,
            f"Daily mails: <b>{state}</b>\n"
            "Usage: <code>/dailyemail on</code> or <code>/dailyemail both</code> (opening + closing/EOD), "
            "<code>/dailyemail open</code> (mornings only), "
            "<code>/dailyemail close</code> (evenings only), "
            "<code>/dailyemail scheduled</code> (sharp separate mails at fixed times), "
            "or <code>/dailyemail off</code> (needs <code>/setemail</code> first)",
        )
        return
    raw = parts[1].lower()
    if raw in ("on", "enable", "start", "yes", "both", "all"):
        if not (settings.get("email") or "").strip():
            reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
            return
        settings["daily_email"] = True
        settings["email_scope"] = "both"
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            f"📧 Daily mails <b>ON (both)</b> in <b>{escape(settings['email'])}</b> every day.<br>"
            "🌅 Morning (07:30-12:00 IST, market open): open screener + last close, "
            "consolidated 52-week range, Nifty actions with buy-by dates, U.S. tables, "
            "overnight gappers, watchlist actions.<br>"
            "🌇 Evening (after 15:45 IST): close screener + 52-week range + EOD stores.",
        )
    elif raw in ("open", "opening", "morning"):
        if not (settings.get("email") or "").strip():
            reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
            return
        settings["daily_email"] = True
        settings["email_scope"] = "open"
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "📧 Daily mails <b>ON (opening only)</b> - morning screener lands every day.")
    elif raw in ("close", "closing", "eod", "evening"):
        if not (settings.get("email") or "").strip():
            reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
            return
        settings["daily_email"] = True
        settings["email_scope"] = "close"
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "📧 Daily mails <b>ON (closing/EOD only)</b> - evening close + stored details land every day.")
    elif raw in ("scheduled", "sharp", "timetable"):
        if not (settings.get("email") or "").strip():
            reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
            return
        settings["daily_email"] = True
        settings["email_scope"] = "scheduled"
        storage.save_user_settings(chat_id, settings)
        reply(
            chat_id,
            "📧 Daily mails <b>ON (scheduled)</b> - poll-timed mails OFF, sharp separate "
            "mails instead:<br>"
            "• <code>/schedule add at 10:10 /emailopen screener</code> + "
            "<code>10:14 nifty</code> + <code>10:18 gappers</code> + "
            "<code>10:22 watchlist</code> (even 4-min spacing)<br>"
            "• <code>/schedule add at 15:50 /emailclose</code> - close + EOD on time",
        )
    elif raw in ("off", "disable", "stop", "no"):
        settings["daily_email"] = False
        settings["email_scope"] = "off"
        storage.save_user_settings(chat_id, settings)
        reply(chat_id, "📧 Daily mails <b>OFF</b>.")
    else:
        reply(chat_id, "Usage: <code>/dailyemail on|both|open|close|scheduled|off</code>")


def _morning_usage() -> str:
    return (
        "Usage: <code>/emailopen</code> (full morning digest) or "
        "<code>/emailopen screener|nifty|us|gappers|watchlist</code> "
        "(that section alone, as its own mail)"
    )


def handle_emailopen(chat_id, parts) -> None:
    """Send the morning mail now: whole digest, or one section separately.

    /emailopen             -> all five sections in one mail (as before)
    /emailopen nifty       -> only the Nifty-actions mail
    /emailopen us|gappers|watchlist|screener|wk52 -> only that section's mail
    Section bodies come from the same builders as the digest, so combined
    and separate mails can never disagree. Empty sections are skipped with
    a note instead of sending an empty mail.
    """
    from ..email.daily import (
        _recorded_openclose,
        build_morning_digest_lines,
        build_morning_section_lines,
        normalize_morning_section,
    )

    settings = storage.get_user_settings(chat_id) or {}
    recipient = (settings.get("email") or "").strip()
    if not recipient:
        reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
        return
    if not email_configured():
        reply(chat_id, _server_not_ready_text())
        return
    section = normalize_morning_section(parts[1] if len(parts) > 1 else None)
    if section is None:
        reply(chat_id, _morning_usage())
        return
    report, label = _recorded_openclose()
    if section == "all":
        reply(chat_id, f"🌅 Preparing the morning digest - mailing to <b>{escape(recipient)}</b> shortly.")
        if not report.get("sections"):
            # No recorded session: build a light snapshot fallback instead of a
            # 60-90s live scan inside the Telegram handler.
            from ..email.daily import build_daily_lines

            snapshot = storage.load_snapshots() or {}
            if not snapshot:
                reply(chat_id, "No recorded session yet - run /openreport or /snap first, then retry.")
                return
            lines = build_daily_lines(snapshot)
        else:
            lines = build_morning_digest_lines(chat_id, report, label)
        ok, info = send_email(recipient, f"Royal Stock opening: {label}", lines,
                              kind="open", chat_id=chat_id)
        reply(chat_id, f"🌅 Morning digest mailed ✅" if ok else f"📧 Mail failed: {escape(info)}")
        return
    title, lines = build_morning_section_lines(chat_id, section, report, label)
    if not lines:
        reply(chat_id, f"No {escape(title.lower())} data available right now - nothing mailed.")
        return
    reply(chat_id, f"🌅 Preparing {escape(title.lower())} - mailing to <b>{escape(recipient)}</b> shortly.")
    ok, info = send_email(recipient, f"Royal Stock opening: {label} — {title}", lines,
                          kind=f"open-{section}", chat_id=chat_id)
    reply(chat_id, f"🌅 {escape(title)} mailed ✅" if ok else f"📧 Mail failed: {escape(info)}")


def handle_emailclose(chat_id, parts) -> None:
    """Force-send the closing digest + EOD stores mail right now.

    Delegates to build_close_digest_lines for the screener + action sections
    so the scheduled-command path always delivers the same content as the
    auto-mailer. EOD stored details (watchlist quotes etc.) are appended after.
    """
    from ..email.daily import (
        _recorded_openclose,
        build_close_digest_lines,
        build_eod_store_lines,
        fetch_watchlist_quotes,
    )

    settings = storage.get_user_settings(chat_id) or {}
    recipient = (settings.get("email") or "").strip()
    if not recipient:
        reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
        return
    if not email_configured():
        reply(chat_id, _server_not_ready_text())
        return
    reply(chat_id, f"🌇 Preparing the close + EOD summary - mailing it to <b>{escape(recipient)}</b> shortly.")
    report, label = _recorded_openclose()
    if report.get("sections"):
        lines = build_close_digest_lines(chat_id, report, label)
    else:
        from ..email.daily import build_daily_lines

        snapshot = storage.load_snapshots() or {}
        lines = list(build_daily_lines(snapshot)) if snapshot else []
        label = str(snapshot.get("session") or label) if snapshot else label
    # The stored-details block is its own section - a chat that removed "eod"
    # from /emailsections close should not get it back here.
    try:
        from ..email.sections import CLOSE_SECTIONS, sections_for
        want_eod = "eod" in sections_for(settings, "close") and "eod" in CLOSE_SECTIONS
    except Exception:
        want_eod = True
    if want_eod:
        try:
            from ..email.daily import _chat_watchlist, _owner_chat

            watchlist = storage.load_watchlist() if str(chat_id) == str(_owner_chat()) else _chat_watchlist(chat_id)
            quotes = fetch_watchlist_quotes(watchlist)
        except Exception:
            quotes = []
        lines.extend(build_eod_store_lines(chat_id, quotes))
    ok, info = send_email(recipient, f"Royal Stock close + EOD: {label}", lines, kind="close", chat_id=chat_id)
    reply(chat_id, f"🌇 Close + EOD mailed ✅" if ok else f"📧 Mail failed: {escape(info)}")


def handle_emailboth(chat_id, parts) -> None:
    """Force-send ONE mail with open + close session + EOD stores right now."""
    from ..email.daily import _recorded_openclose, build_combined_lines
    from ..email.daily import fetch_watchlist_quotes

    settings = storage.get_user_settings(chat_id) or {}
    recipient = (settings.get("email") or "").strip()
    if not recipient:
        reply(chat_id, "Set your mail id first: <code>/setemail you@gmail.com</code>")
        return
    if not email_configured():
        reply(chat_id, _server_not_ready_text())
        return
    reply(chat_id, f"🌅🌇 Preparing open + close + EOD in one mail - sending to <b>{escape(recipient)}</b> shortly.")
    report, label = _recorded_openclose()
    if not report.get("sections"):
        # Same honest fallback as /emailclose: snapshot digest + stores
        # instead of refusing, so one command always delivers one mail.
        from ..email.daily import build_daily_lines

        snapshot = storage.load_snapshots() or {}
        if not snapshot:
            reply(chat_id, "No recorded session yet - run /openreport or /snap first, then retry.")
            return
        label = str(snapshot.get("session") or label)
        report = {}
        lines = list(build_daily_lines(snapshot))
    else:
        lines = None
    try:
        from ..email.daily import _chat_watchlist, _owner_chat

        watchlist = storage.load_watchlist() if str(chat_id) == str(_owner_chat()) else _chat_watchlist(chat_id)
        quotes = fetch_watchlist_quotes(watchlist)
    except Exception:
        quotes = []
    from ..email.daily import build_eod_store_lines

    if lines is None:
        lines = build_combined_lines(chat_id, report, label, quotes)
    else:
        lines = lines + build_eod_store_lines(chat_id, quotes)
    ok, info = send_email(
        recipient, f"Royal Stock open + close + EOD: {label}",
        lines,
        kind="both", chat_id=chat_id,
    )
    reply(chat_id, f"🌅🌇 Open + close + EOD mailed ✅" if ok else f"📧 Mail failed: {escape(info)}")


def handle_emailstatus(chat_id, parts) -> None:
    """Show the last mail sends: success or failed + reason."""
    from .. import config as _config

    try:
        entries = storage.load_mail_log(20) or []
    except Exception:
        entries = []
    is_owner = str(chat_id) == str(_config.TELEGRAM_CHAT_ID or "local")
    # Unattributed ("-") entries can carry another chat's address - only the
    # owner ever sees those; everyone else sees strictly their own sends.
    mine = [entry for entry in entries
            if str(entry.get("chat")) == str(chat_id)
            or (is_owner and str(entry.get("chat")) == "-")]
    if is_owner:
        mine = entries  # owner sees every chat's sends
    if not mine:
        reply(chat_id, "No mails sent yet - try <code>/emailboth</code> first.")
        return
    lines = ["<b>📧 Last mail sends</b>"]
    for entry in mine[:6]:
        mark = "✅" if entry.get("ok") else "❌"
        at = escape(str(entry.get("at") or "?")[:16].replace("T", " "))
        kind = escape(str(entry.get("kind") or "?"))
        subject = escape(str(entry.get("subject") or "?"))
        to = escape(str(entry.get("to") or "?"))
        lines.append(f"{mark} <code>{at}</code> [{kind}] <b>{subject}</b> → {to}")
        if not entry.get("ok") and entry.get("info"):
            lines.append(f"   └ failed: <i>{escape(str(entry['info'])[:160])}</i>")
    reply(chat_id, "<br>".join(lines))


def handle_emailsections(chat_id, parts) -> None:
    """Choose WHICH sections your morning / evening mails contain.

    /emailsections                       -> show what you currently get
    /emailsections nifty,gappers         -> morning mail: only those two
    /emailsections all                   -> every section again
    /emailsections add us                -> add one, keep the rest
    /emailsections remove us             -> drop one, keep the rest
    /emailsections close nifty,eod       -> same for the evening mail
    /emailsections close add eod         -> add one to the evening mail

    Sections always render in the mail's own order, so "gappers,nifty" looks
    exactly like "nifty,gappers". A chat with no preference gets everything -
    there is no way to accidentally silence your own mail by clearing it.
    """
    from ..email.sections import (
        CLOSE_SECTION_TITLES,
        MORNING_SECTION_TITLES,
        add_section,
        default_sections,
        describe_sections,
        normalize_section_list,
        remove_section,
        sections_for,
    )

    tokens = [str(t).strip() for t in (parts or [])[1:] if str(t).strip()]
    kind = "morning"
    if tokens and tokens[0].lower() in ("close", "evening", "eodmail", "close-mail"):
        kind = "close"
        tokens = tokens[1:]

    settings = storage.get_user_settings(chat_id) or {}
    key = "email_sections" if kind == "morning" else "email_close_sections"
    current = sections_for(settings, kind)
    catalogue = default_sections(kind)

    # ---- show current -------------------------------------------------
    if not tokens:
        titles = MORNING_SECTION_TITLES if kind == "morning" else CLOSE_SECTION_TITLES
        rows = []
        for name in catalogue:
            rows.append(
                ("✅" if name in current else "➖")
                + " "
                + escape(titles.get(name, name))
                + f"  <code>{escape(name)}</code>"
            )
        # Examples must be copy-pasteable: the evening mail needs the
        # "close" keyword, the morning mail must not carry it.
        prefix = "close " if kind == "close" else ""
        other = "morning" if kind == "close" else "close"
        other_hint = (
            f"<code>/emailsections nifty,gappers</code> — the morning mail"
            if kind == "close" else
            "<code>/emailsections close nifty,eod</code> — the evening mail"
        )
        body = (
            f"<b>Mail sections · {kind}</b>\n"
            + "\n".join(rows)
            + "\n\nChange them:\n"
            + f"<code>/emailsections {prefix}nifty,gappers</code> — only those\n"
            + f"<code>/emailsections {prefix}add us</code> / "
            + f"<code>/emailsections {prefix}remove us</code>\n"
            + f"<code>/emailsections {prefix}all</code> — everything again\n"
            + other_hint
        )
        return reply(chat_id, body)

    # ---- add / remove one --------------------------------------------
    action = tokens[0].lower()
    if action in ("add", "remove", "+", "-") and len(tokens) >= 2:
        helper = add_section if action in ("add", "+") else remove_section
        updated: list[str] = list(current)
        errors: list[str] = []
        for word in tokens[1:]:
            updated, err = helper(updated, word, kind)
            if err:
                errors.append(err)
        if not updated:
            updated = catalogue  # never let a mail become empty
        settings[key] = updated
        storage.save_user_settings(chat_id, settings)
        note = (" " + "; ".join(errors)) if errors else ""
        return reply(
            chat_id,
            f"✅ <b>{kind.capitalize()} mail</b> sections now: "
            f"<b>{escape(describe_sections(updated, kind))}</b>{escape(note)}",
        )

    # ---- replace the whole list ---------------------------------------
    chosen, unknown = normalize_section_list(tokens, kind)
    if not chosen:
        chosen = catalogue
    settings[key] = chosen
    storage.save_user_settings(chat_id, settings)
    note = ""
    if unknown:
        note = "\n⚠️ ignored unknown: " + escape(", ".join(unknown))
    reply(
        chat_id,
        f"✅ <b>{kind.capitalize()} mail</b> will contain: "
        f"<b>{escape(describe_sections(chosen, kind))}</b>{note}",
    )
