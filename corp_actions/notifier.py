"""Telegram notification sending and message formatting."""
import html
import logging
import re
from datetime import date, datetime, timedelta

import requests

from . import config, sources

log = logging.getLogger(__name__)


def escape(text) -> str:
    """Escape text for Telegram HTML parse mode.

    quote=True also escapes " and ' so escaped values are safe inside HTML
    attributes too (e.g. the href of a news link).
    """
    return html.escape(str(text or ""), quote=True)


def _fmt_date(value) -> str:
    """Pretty-print an ISO date as '07-Aug-2026' (raw string if unparsable)."""
    s = str(value or "")
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").strftime("%d-%b-%Y")
    except (ValueError, TypeError):
        return s


def _fmt_ts(ts) -> str:
    """Format a unix timestamp as '07-Aug 14:30' (or empty when absent)."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d-%b %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def fmt_money(price, currency: str = "INR") -> str:
    """Format a price with a currency symbol and thousands separators."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return f"{price}"
    if currency == "INR":
        symbol = "\u20b9"
    elif currency == "USD":
        symbol = "$"
    else:
        symbol = f" {currency}"
    return f"{symbol}{value:,.2f}"


def _fmt_price(action) -> str:
    """Compact price string from an attached quote, or '' when unavailable."""
    quote = action.get("quote")
    if not quote or quote.get("price") is None:
        return ""
    price = quote["price"]
    currency = quote.get("currency", "INR")
    change = quote.get("change_pct")
    money = fmt_money(price, currency)
    if change is not None:
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
        sign = "+" if change >= 0 else ""
        return f"{money} {color_icon}{arrow} ({sign}{change:.2f}%)"
    return money


def quick_menu_markup() -> dict:
    """Persistent one-tap reply keyboard of the main commands.

    Each button label IS the command text (Telegram sends the label verbatim
    when tapped), so a tap runs the command through the normal dispatcher -
    no callback handling, no typing. Attach with send_message(reply_markup=...).
    """
    return {
        "keyboard": [
            ["/corpactionsformylist"],
            ["/myfavourites run", "/corpactions"],
            ["/corpactionssummary", "/topgainers 1h", "/toplosers 1h"],
            ["/watchlist", "/all", "/help"],
            ["/menu off"],
        ],
        "resize_keyboard": True,
    }


def hide_keyboard_markup() -> dict:
    """Remove the persistent quick-menu keyboard (ReplyKeyboardRemove)."""
    return {"remove_keyboard": True}


class NotifierError(Exception):
    """Raised when Telegram rejects a message."""


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def send_message(
    text: str,
    parse_mode: str = "HTML",
    chat_id: str | None = None,
    reply_markup: dict | None = None,
) -> dict:
    """Send a text message to a chat. Returns Telegram response.

    `reply_markup` is a Telegram keyboard dict (e.g. a ReplyKeyboardMarkup),
    sent as-is in the payload so buttons work without extra processing.
    """
    target = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not target:
        raise NotifierError(
            "Telegram not configured: TELEGRAM_BOT_TOKEN or target chat_id is missing."
        )
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise NotifierError(f"Telegram API error: {data.get('description')}")
        log.info(
            "Telegram send to chat %s: %d chars (parse=%s) -> %s",
            target, len(text), parse_mode or "plain",
            text[:100].replace("\n", " "),
        )
        return data
    except requests.RequestException as exc:
        # Fall back to plain text if HTML parsing failed (HTTP 400)
        if parse_mode and ("400" in str(exc) or "parse" in str(exc).lower()):
            log.warning("Telegram HTML parse failed for chat %s — retrying plain text fallback", target)
            plain_text = re.sub(r"<[^>]+>", "", text)
            plain_payload = {"chat_id": target, "text": plain_text}
            if reply_markup is not None:
                plain_payload["reply_markup"] = reply_markup
            try:
                resp2 = requests.post(url, json=plain_payload, timeout=config.HTTP_TIMEOUT)
                resp2.raise_for_status()
                log.info("Telegram plain text fallback succeeded for chat %s", target)
                return resp2.json()
            except Exception as exc2:
                log.warning("Telegram plain text fallback also failed: %s", exc2)
        log.warning("Telegram send to chat %s failed: %s", target, config.redact(exc))
        # redact() strips the bot token - requests embeds the full request URL
        # (which contains the token) in HTTP-error exceptions, and this message
        # can surface in the dashboard, Telegram replies and logs.
        raise NotifierError(config.redact(f"Telegram send failed: {exc}")) from exc


# Emoji map for corporate action types
_TYPE_EMOJI = {
    "dividend": "\U0001F4B0",   # 💰
    "bonus": "\U0001F381",       # 🎁
    "split": "\u2702\ufe0f",     # ✂️
    "rights": "\U0001F4DC",      # 📜
    "buyback": "\U0001F501",     # 🔁
    "other": "\U0001F4CB",       # 📋
}


def format_mover_alert(symbol: str, quote: dict, change_pct: float) -> str:
    """Compact sudden-move alert for the /watcher background scanner.

    e.g. a stock moving >=5% in the session from its previous close:

        \U0001F6A8 BIG MOVER
        **INFY** (NSE) - Infosys Limited
        Current Price: \u20b91,183.00  \U0001F534\u25bc -5.62%
        (session move vs previous close)
    """
    price = quote.get("price")
    name = quote.get("name") or symbol
    arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
    color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
    sign = "+" if change_pct >= 0 else ""
    lines = [
        "\U0001F6A8 <b>BIG MOVER</b>",
        f"<b>{escape(symbol)}</b> (NSE) - {escape(name)}",
    ]
    if price is not None:
        lines.append(f"Current Price: <b>{fmt_money(price)}</b>  {color_icon}{arrow} <b>{sign}{change_pct:.2f}%</b>")
    lines.append("Session move vs previous close - tap below for deep fundamentals.")
    return "\n".join(lines)


def symbol_buttons(symbols: list[str], prefix: str = "fund", per_row: int = 4) -> dict:
    """Inline keyboard of one button per symbol, e.g. tap PFC -> deep report.

    prefix is the callback prefix: "fund" opens /fundamentalreport,
    "ana" opens /fundamentalanalyze. Buttons make every symbol in a report
    tappable so users jump straight to fundamentals instead of typing.
    """
    rows = []
    for i in range(0, len(symbols), per_row):
        rows.append([
            {"text": sym, "callback_data": f"{prefix}:{sym}"}
            for sym in symbols[i:i + per_row]
        ])
    return {"inline_keyboard": rows}


def fundamentals_button(label: str = "Get Fundamentals") -> dict:
    """One-tap button that enriches the current movers report with fundamentals.

    callback_data "mfund" tells the bot to fetch fundamentals for the last
    screen the user ran and send the full enriched report - so price-only
    movers reports stay fast and fundamentals are fetched only on demand.
    """
    return {"inline_keyboard": [[{"text": label, "callback_data": "mfund"}]]}


def format_corporate_action(action: dict) -> str:
    """Render a corporate action record as an HTML Telegram message."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    ex_date = action.get("ex_date") or "-"
    record_date = action.get("record_date") or "-"
    exchange = action.get("exchange") or "-"

    typ = sources.action_type(subject)
    type_emoji = _TYPE_EMOJI.get(typ, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>Corporate Action Alert</b>",
        f"<b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    if typ != "other":
        lines.append(f"Type: {sources.TYPE_LABELS.get(typ, typ)}")
    quote = action.get("quote")
    if quote and quote.get("price") is not None:
        price = quote["price"]
        currency = quote.get("currency", "INR")
        change = quote.get("change_pct")
        if change is not None:
            arrow = "\u25b2" if change >= 0 else "\u25bc"
            color_icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
            sign = "+" if change >= 0 else ""
            lines.append(
                f"Current Price: <b>{fmt_money(price, currency)}</b>  "
                f"{color_icon}{arrow} <b>{sign}{change:.2f}%</b>"
            )
        else:
            lines.append(f"Current Price: <b>{fmt_money(price, currency)}</b>")
    if ex_date and ex_date != "-":
        lines.append(f"\U0001F4C5 Ex-Date: <b>{_fmt_date(ex_date)}</b>")
    if record_date and record_date != "-":
        lines.append(f"Record Date: {_fmt_date(record_date)}")
    isin = action.get("isin")
    if isin and isin != "-":
        lines.append(f"ISIN: {escape(isin)}")
    rs, re_ = action.get("rights_start"), action.get("rights_end")
    if rs and str(rs).strip() not in ("", "-") and re_ and str(re_).strip() not in ("", "-"):
        lines.append(f"Offer Window: <b>{_fmt_date(rs)}</b> \u2192 <b>{_fmt_date(re_)}</b>")
    dot, tag = status_tag(action)
    lines.append(f"Status: {dot} {escape(tag)}")
    return "\n".join(lines)


def format_reminder(action: dict) -> str:
    """Render an 'ex-date approaching' reminder as an HTML Telegram message."""
    symbol = escape(action.get("symbol") or "-")
    company = escape(action.get("company") or "-")
    subject = escape(action.get("subject") or "-")
    ex_date = action.get("ex_date") or "-"
    record_date = action.get("record_date") or "-"
    exchange = escape(action.get("exchange") or "-")

    lines = [
        "\u23f0 <b>Ex-date reminder</b>",
        f"<b>{symbol}</b> ({exchange}) - {company}",
        f"Subject: {subject}",
        f"Ex-Date: <b>{ex_date}</b>",
    ]
    if record_date and record_date != "-":
        lines.append(f"Record Date: {record_date}")
    rs, re_ = action.get("rights_start"), action.get("rights_end")
    if rs and str(rs).strip() not in ("", "-") and re_ and str(re_).strip() not in ("", "-"):
        lines.append(f"Offer Window: <b>{_fmt_date(rs)}</b> \u2192 <b>{_fmt_date(re_)}</b>")
    quote = action.get("quote")
    if quote and quote.get("price") is not None:
        currency = quote.get("currency", "INR")
        lines.append(f"Current Price: <b>{quote['price']:.2f} {currency}</b>")
    return "\n".join(lines)


def format_price_alert(item: dict, quote: dict, threshold: float) -> str:
    """Render a price-move alert for a watched stock."""
    symbol = item.get("symbol") or "-"
    exchange = item.get("exchange") or "-"
    company = item.get("company") or "-"
    price = quote.get("price")
    change = quote.get("change_pct")
    currency = quote.get("currency", "INR")

    if change is not None and change >= 0:
        header_icon = "\U0001F7E2\u25b2"  # 🟢▲ up
        sign = "+"
    elif change is not None:
        header_icon = "\U0001F534\u25bc"  # 🔴▼ down
        sign = ""
    else:
        header_icon = "\u26a0\ufe0f"      # ⚠️ unknown
        sign = ""

    price_txt = fmt_money(price, currency) if price is not None else "n/a"
    if change is not None:
        change_txt = f"{sign}{change:.2f}%"
        detail_line = f"Price: <b>{price_txt}</b>  {header_icon} <b>{change_txt}</b> today"
    else:
        detail_line = f"Price: <b>{price_txt}</b>"

    return "\n".join(
        [
            f"{header_icon} <b>Price Alert \u2014 {escape(symbol)}</b>",
            f"({escape(exchange)}) {escape(company)}",
            detail_line,
            f"Crossed your \u00b1{threshold:g}% daily alert threshold.",
        ]
    )


def format_upcoming_list(actions: list[dict]) -> str:
    """Render a compact list of upcoming ex-dates for Telegram (/next)."""
    if not actions:
        return "No upcoming ex-dates in the reminder window."
    lines = ["<b>Upcoming ex-dates</b>"]
    for action in sorted(actions, key=lambda a: a.get("ex_date") or "9999-99-99"):
        typ = sources.action_type(action.get("subject"))
        lines.append(
            f"\u2022 <b>{escape(action.get('symbol'))}</b> ({escape(action.get('exchange'))}) - "
            f"{escape(action.get('ex_date'))} [{sources.TYPE_LABELS.get(typ, typ)}]"
        )
    return "\n".join(lines)


def _parse_iso_date(value) -> date | None:
    """Parse an ISO date string, returning None when unset/invalid."""
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None





def action_status(action: dict, today: date | None = None) -> str:
    """Derived one-line status for a corporate action, based on its dates.

    The NSE/BSE feeds carry announcement, ex and record dates but never a
    payment status, so this derives an honest, checkable status from them:
    announced-but-undated, upcoming, or ex-date passed with type-specific
    settlement guidance (rights subscription window, dividend payment
    window, bonus/split credit).
    """
    today = today or config.today_ist()
    typ = sources.action_type(action.get("subject"))
    ex = _parse_iso_date(action.get("ex_date"))
    rec = _parse_iso_date(action.get("record_date"))
    ann = _parse_iso_date(action.get("announcement_date"))

    if ex is None:
        return "Announced - ex-date not fixed yet (check the company notice for dates)"
    if ex >= today:
        return f"Upcoming - ex-date on {_fmt_date(ex)}"

    days_ago = (today - ex).days
    if typ == "rights":
        return (
            f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - rights "
            "subscription window open; check the company notice for the last "
            "date to apply"
        )
    if typ == "dividend":
        if rec:
            due = rec + timedelta(days=30)
            if today > due:
                return (
                    f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - payment "
                    f"window (30 days from record date {_fmt_date(rec)}) passed; "
                    "contact broker if not credited"
                )
            return (
                f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - payment due "
                f"by {_fmt_date(due)} (30 days from record date); contact broker "
                "if not credited"
            )
        return (
            f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - payment normally "
            "follows within days-weeks; contact broker if not credited"
        )
    if typ == "bonus":
        return (
            f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - bonus shares "
            "usually credited within ~2 weeks of the record date"
        )
    if typ == "split":
        return (
            f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - shares "
            "re-denominated to the new face value"
        )
    if typ == "buyback":
        return (
            f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago) - buyback offer "
            "window ongoing; check the offer notice"
        )
    return f"Ex-date passed {_fmt_date(ex)} ({days_ago}d ago)"


def status_tag(action: dict, today: date | None = None) -> tuple[str, str]:
    """(colored-dot emoji, short status tag) for a corporate action.

    Used to colour-code the alert blocks and /corpactionsformylist report at
    a glance: green = on track (upcoming, settled), yellow = in progress
    (subscription/payment window open), red = needs attention (payment
    window passed).
    """
    today = today or config.today_ist()
    typ = sources.action_type(action.get("subject"))
    ex = _parse_iso_date(action.get("ex_date"))
    rec = _parse_iso_date(action.get("record_date"))
    if ex is None:
        return "\U0001F7E1", "Announced - dates pending"
    if ex >= today:
        return "\U0001F7E2", "Upcoming"
    if typ == "rights":
        rs, re_ = action.get("rights_start"), action.get("rights_end")
        s = _parse_iso_date(rs) if rs else None
        e = _parse_iso_date(re_) if re_ else None
        if s and e:
            if today < s:
                return "\U0001F7E1", f"Offer opens {_fmt_date(s)}"
            if s <= today <= e:
                return "\U0001F7E2", f"Offer open until {_fmt_date(e)}"
            return "\U0001F534", f"Offer closed {_fmt_date(e)}"
        return "\U0001F7E1", "Subscription window open"
    if typ == "dividend":
        if rec:
            due = rec + timedelta(days=30)
            if today > due:
                return "\U0001F534", "Payment window passed"
            return "\U0001F7E1", f"Payment due by {_fmt_date(due)}"
        return "\U0001F7E1", "Payment pending"
    if typ == "bonus":
        return "\U0001F7E1", "Credit within ~2 wks"
    if typ == "split":
        return "\U0001F7E2", "Re-denominated"
    if typ == "buyback":
        return "\U0001F7E1", "Offer open"
    return "\U0001F7E1", "Ex-date passed"


def format_action_block(action: dict) -> str:
    """Full colorful block for ONE action, as used by /corpactionsformylist.

    Matches the alert layout: bold symbol + company, subject, current price
    with a green/red direction arrow, and the ex/record dates.
    """
    symbol = action.get("symbol") or "-"
    exchange = action.get("exchange") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    typ = sources.action_type(subject)
    type_emoji = _TYPE_EMOJI.get(typ, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    quote = action.get("quote")
    if quote and quote.get("price") is not None:
        price = quote["price"]
        currency = quote.get("currency", "INR")
        change = quote.get("change_pct")
        if change is not None:
            arrow = "\u25b2" if change >= 0 else "\u25bc"
            color_icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
            sign = "+" if change >= 0 else ""
            lines.append(
                f"Current Price: <b>{fmt_money(price, currency)}</b>  "
                f"{color_icon}{arrow} <b>{sign}{change:.2f}%</b>"
            )
        else:
            lines.append(f"Current Price: <b>{fmt_money(price, currency)}</b>")
    ex = action.get("ex_date")
    if ex and str(ex).strip() not in ("", "-"):
        lines.append(f"Ex-Date: <b>{escape(ex)}</b>")
    rec = action.get("record_date")
    if rec and str(rec).strip() not in ("", "-"):
        lines.append(f"Record Date: {escape(rec)}")
    ann = action.get("announcement_date")
    if ann and str(ann).strip() not in ("", "-"):
        lines.append(f"Announced: {_fmt_date(ann)}")
    bc_start, bc_end = action.get("bc_start"), action.get("bc_end")
    if (bc_start and str(bc_start).strip() not in ("", "-")) or (
        bc_end and str(bc_end).strip() not in ("", "-")
    ):
        span = " \u2013 ".join(
            _fmt_date(d) for d in (bc_start, bc_end)
            if d and str(d).strip() not in ("", "-")
        )
        lines.append(f"Book Closure: {span}")
    rs, re_ = action.get("rights_start"), action.get("rights_end")
    if rs and str(rs).strip() not in ("", "-") and re_ and str(re_).strip() not in ("", "-"):
        lines.append(f"Offer Window: <b>{_fmt_date(rs)}</b> \u2192 <b>{_fmt_date(re_)}</b>")
    dot, tag = status_tag(action)
    lines.append(f"Status: {dot} {escape(tag)}")
    return "\n".join(lines)


def format_next_report(
    upcoming: list[dict], recent: list[dict], pending: list[dict] | None = None
) -> str:
    """Render /corpactionsformylist: a full colorful block per action.

    Every action - upcoming, recently passed / in-progress, and announced-
    but-undated - is shown as a detail block (symbol + company, subject,
    current price with direction arrow, ex/record dates), the same layout
    as the push alerts, so the whole report reads clearly with dates.
    """
    sections = []
    if upcoming:
        blocks = [
            format_action_block(a)
            for a in sorted(upcoming, key=lambda a: a.get("ex_date") or "9999-99-99")
        ]
        sections.append(
            "<b>\U0001F4C5 Upcoming ex-dates</b>\n\n" + "\n\n".join(blocks)
        )
    else:
        sections.append(
            "<b>\U0001F4C5 Upcoming ex-dates</b>\nNone in the reminder window."
        )

    if pending:
        blocks = [format_action_block(a) for a in pending]
        sections.append(
            "<b>\U0001F4E2 Announced - ex-date not fixed yet</b>\n\n"
            + "\n\n".join(blocks)
        )

    if recent:
        blocks = [
            format_action_block(a)
            for a in sorted(
                recent, key=lambda a: a.get("ex_date") or "0000-01-01", reverse=True
            )
        ]
        sections.append(
            "<b>\U0001F504 Recently passed / in progress</b> (past 30 days)\n\n"
            + "\n\n".join(blocks)
        )

    if not sections:
        return "No corporate actions for your watchlist right now."
    return "\n\n".join(sections)


def format_action_entry(action: dict) -> str:
    """Two-line entry used by /ca, /exdate and /summary query results.

    Line 1: symbol, exchange, ex-date (pretty-printed).
    Line 2: type, subject, current price, record date - clear and skimmable.
    """
    symbol = action.get("symbol") or "-"
    exchange = action.get("exchange") or "-"
    subject = action.get("subject") or "-"
    typ = sources.action_type(subject)
    label = sources.TYPE_LABELS.get(typ, typ)
    company = action.get("company")
    head = f"\u2022 <b>{escape(symbol)}</b> ({escape(exchange)})"
    if company and str(company).strip() not in ("", "-"):
        head += f" - {escape(company)}"
    head += f"  Ex-date: <b>{_fmt_date(action.get('ex_date'))}</b>"
    lines = [head]
    bits = []
    if subject != "-":
        bits.append(f"{label}: {escape(subject)}")
    price = _fmt_price(action)
    if price:
        bits.append(f"Price: <b>{price}</b>")
    rec = action.get("record_date")
    if rec and str(rec).strip() not in ("", "-"):
        bits.append(f"Record: {_fmt_date(rec)}")
    if bits:
        lines.append("   " + " | ".join(bits))
    return "\n".join(lines)


def format_action_detail(action: dict) -> str:
    """Full detail block for a single corporate action query result.

    Mirrors the alert layout so /corpactions SYMBOL reads exactly like the
    push notifications: symbol line, subject, type, price, key dates, and a
    compact derived status.
    """
    symbol = action.get("symbol") or "-"
    exchange = action.get("exchange") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    typ = sources.action_type(subject)
    type_emoji = _TYPE_EMOJI.get(typ, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    if typ != "other":
        lines.append(f"Type: {sources.TYPE_LABELS.get(typ, typ)}")
    price = _fmt_price(action)
    if price:
        lines.append(f"Current Price: <b>{price}</b>")
    for label, key, fmt in (
        ("Ex-Date", "ex_date", "date"),
        ("Record Date", "record_date", "date"),
        ("Announced", "announcement_date", "date"),
        ("Face Value", "face_value", None),
        ("Series", "series", None),
        ("ISIN", "isin", None),
    ):
        val = action.get(key)
        if val and str(val).strip() and str(val).strip() != "-":
            display = _fmt_date(val) if fmt == "date" else escape(val)
            lines.append(f"{label}: {display}")
    rs, re_ = action.get("rights_start"), action.get("rights_end")
    if rs and str(rs).strip() not in ("", "-") and re_ and str(re_).strip() not in ("", "-"):
        lines.append(f"Offer Window: <b>{_fmt_date(rs)}</b> \u2192 <b>{_fmt_date(re_)}</b>")
    bc_start, bc_end = action.get("bc_start"), action.get("bc_end")
    if (bc_start and str(bc_start).strip() not in ("", "-")) or (
        bc_end and str(bc_end).strip() not in ("", "-")
    ):
        span = " \u2013 ".join(
            _fmt_date(d) for d in (bc_start, bc_end)
            if d and str(d).strip() not in ("", "-")
        )
        lines.append(f"Book Closure: {span}")
    lines.append(f"Status: {action_status(action)}")
    return "\n".join(lines)


def format_news_item(item: dict, index: int = 1) -> str:
    """Render one news headline with source and publish time."""
    title = escape(item.get("title") or "-")
    link = (item.get("link") or "").strip()
    pub = _fmt_ts(item.get("published_ts"))
    publisher = escape(item.get("publisher") or "")
    meta = " | ".join(p for p in (publisher, pub) if p)
    head = f"<a href=\"{escape(link)}\">{title}</a>" if link else title
    return "\n".join([f"{index}. {head}"] + ([f"   {meta}"] if meta else []))


def format_news_list(symbol: str, exchange: str, items: list[dict]) -> str:
    """Render the latest news for one stock as a Telegram HTML message."""
    header = f"<b>Latest news - {escape(symbol)}</b> ({escape(exchange)})"
    if not items:
        return header + "\nNo recent news found."
    body = [format_news_item(item, i) for i, item in enumerate(items, 1)]
    return "\n".join([header] + body)
