"""Corporate-action message renderers (alerts, reminders, lists, reports)."""
from __future__ import annotations

from datetime import timedelta

from ..core.dates import format_date, parse_iso_date, today_ist
from ..core.numbers import format_money
from ..core.text import escape
from ..sources.types import TYPE_LABELS, action_type

# Emoji map for corporate action types
_TYPE_EMOJI = {
    "dividend": "\U0001F4B0",   # 💰
    "bonus": "\U0001F381",       # 🎁
    "split": "\u2702\ufe0f",     # ✂️
    "rights": "\U0001F4DC",      # 📜
    "buyback": "\U0001F501",     # 🔁
    "other": "\U0001F4CB",       # 📋
}


def _format_price(action) -> str:
    """Compact price string from an attached quote, or '' when unavailable."""
    quote = action.get("quote")
    if not quote or quote.get("price") is None:
        return ""
    price = quote["price"]
    currency = quote.get("currency", "INR")
    change = quote.get("change_pct")
    money = format_money(price, currency)
    if change is not None:
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
        sign = "+" if change >= 0 else ""
        return f"{money} {color_icon}{arrow} ({sign}{change:.2f}%)"
    return money


def _price_line(action) -> str | None:
    """The 'Current Price: ...' line for an action with an attached quote."""
    quote = action.get("quote")
    if not quote or quote.get("price") is None:
        return None
    price = quote["price"]
    currency = quote.get("currency", "INR")
    change = quote.get("change_pct")
    if change is not None:
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        color_icon = "\U0001F7E2" if change >= 0 else "\U0001F534"
        sign = "+" if change >= 0 else ""
        return (
            f"Current Price: <b>{format_money(price, currency)}</b>  "
            f"{color_icon}{arrow} <b>{sign}{change:.2f}%</b>"
        )
    return f"Current Price: <b>{format_money(price, currency)}</b>"


def _offer_window_line(action) -> str | None:
    rights_start, rights_end = action.get("rights_start"), action.get("rights_end")
    if rights_start and str(rights_start).strip() not in ("", "-") and rights_end and str(rights_end).strip() not in ("", "-"):
        return f"Offer Window: <b>{format_date(rights_start)}</b> \u2192 <b>{format_date(rights_end)}</b>"
    return None


def _book_closure_line(action) -> str | None:
    bc_start, bc_end = action.get("book_closure_start"), action.get("book_closure_end")
    if (bc_start and str(bc_start).strip() not in ("", "-")) or (
        bc_end and str(bc_end).strip() not in ("", "-")
    ):
        span = " \u2013 ".join(
            format_date(date) for date in (bc_start, bc_end)
            if date and str(date).strip() not in ("", "-")
        )
        return f"Book Closure: {span}"
    return None


def format_corporate_action(action: dict) -> str:
    """Render a corporate action record as an HTML Telegram message."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    ex_date = action.get("ex_date") or "-"
    record_date = action.get("record_date") or "-"
    exchange = action.get("exchange") or "-"

    type_name = action_type(subject)
    type_emoji = _TYPE_EMOJI.get(type_name, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>Corporate Action Alert</b>",
        f"<b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    if type_name != "other":
        lines.append(f"Type: {TYPE_LABELS.get(type_name, type_name)}")
    price_line = _price_line(action)
    if price_line:
        lines.append(price_line)
    if ex_date and ex_date != "-":
        lines.append(f"\U0001F4C5 Ex-Date: <b>{format_date(ex_date)}</b>")
    if record_date and record_date != "-":
        lines.append(f"Record Date: {format_date(record_date)}")
    isin = action.get("isin")
    if isin and isin != "-":
        lines.append(f"ISIN: {escape(isin)}")
    offer = _offer_window_line(action)
    if offer:
        lines.append(offer)
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
    offer = _offer_window_line(action)
    if offer:
        lines.append(offer)
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

    price_txt = format_money(price, currency) if price is not None else "n/a"
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


def format_mover_alert(symbol: str, quote: dict, change_pct: float) -> str:
    """Compact sudden-move alert for the /watcher background scanner.

    e.g. a stock moving >=5% in the session from its previous close:

        🚨 BIG MOVER
        **INFY** (NSE) - Infosys Limited
        Current Price: ₹1,183.00  🔴▼ -5.62%
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
        lines.append(f"Current Price: <b>{format_money(price)}</b>  {color_icon}{arrow} <b>{sign}{change_pct:.2f}%</b>")
    lines.append("Session move vs previous close - tap below for deep fundamentals.")
    return "\n".join(lines)


def format_upcoming_list(actions: list[dict]) -> str:
    """Render a compact list of upcoming ex-dates for Telegram (/next)."""
    if not actions:
        return "No upcoming ex-dates in the reminder window."
    lines = ["<b>Upcoming ex-dates</b>"]
    for action in sorted(actions, key=lambda action: action.get("ex_date") or "9999-99-99"):
        type_name = action_type(action.get("subject"))
        lines.append(
            f"\u2022 <b>{escape(action.get('symbol'))}</b> ({escape(action.get('exchange'))}) - "
            f"{escape(action.get('ex_date'))} [{TYPE_LABELS.get(type_name, type_name)}]"
        )
    return "\n".join(lines)


def action_status(action: dict, today=None) -> str:
    """Derived one-line status for a corporate action, based on its dates.

    The NSE/BSE feeds carry announcement, ex and record dates but never a
    payment status, so this derives an honest, checkable status from them:
    announced-but-undated, upcoming, or ex-date passed with type-specific
    settlement guidance (rights subscription window, dividend payment
    window, bonus/split credit).
    """
    today = today or today_ist()
    type_name = action_type(action.get("subject"))
    ex_date = parse_iso_date(action.get("ex_date"))
    record_date = parse_iso_date(action.get("record_date"))
    announcement_date = parse_iso_date(action.get("announcement_date"))

    if ex_date is None:
        return "Announced - ex-date not fixed yet (check the company notice for dates)"
    if ex_date >= today:
        return f"Upcoming - ex-date on {format_date(ex_date)}"

    days_ago = (today - ex_date).days
    if type_name == "rights":
        return (
            f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - rights "
            "subscription window open; check the company notice for the last "
            "date to apply"
        )
    if type_name == "dividend":
        if record_date:
            due_date = record_date + timedelta(days=30)
            if today > due_date:
                return (
                    f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - payment "
                    f"window (30 days from record date {format_date(record_date)}) passed; "
                    "contact broker if not credited"
                )
            return (
                f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - payment due "
                f"by {format_date(due_date)} (30 days from record date); contact broker "
                "if not credited"
            )
        return (
            f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - payment normally "
            "follows within days-weeks; contact broker if not credited"
        )
    if type_name == "bonus":
        return (
            f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - bonus shares "
            "usually credited within ~2 weeks of the record date"
        )
    if type_name == "split":
        return (
            f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - shares "
            "re-denominated to the new face value"
        )
    if type_name == "buyback":
        return (
            f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago) - buyback offer "
            "window ongoing; check the offer notice"
        )
    return f"Ex-date passed {format_date(ex_date)} ({days_ago}d ago)"


def status_tag(action: dict, today=None) -> tuple[str, str]:
    """(colored-dot emoji, short status tag) for a corporate action.

    Used to colour-code the alert blocks and /corpactionsformylist report at
    a glance: green = on track (upcoming, settled), yellow = in progress
    (subscription/payment window open), red = needs attention (payment
    window passed).
    """
    today = today or today_ist()
    type_name = action_type(action.get("subject"))
    ex_date = parse_iso_date(action.get("ex_date"))
    record_date = parse_iso_date(action.get("record_date"))
    if ex_date is None:
        return "\U0001F7E1", "Announced - dates pending"
    if ex_date >= today:
        return "\U0001F7E2", "Upcoming"
    if type_name == "rights":
        rights_start, rights_end = action.get("rights_start"), action.get("rights_end")
        start_date = parse_iso_date(rights_start) if rights_start else None
        end_date = parse_iso_date(rights_end) if rights_end else None
        if start_date and end_date:
            if today < start_date:
                return "\U0001F7E1", f"Offer opens {format_date(start_date)}"
            if start_date <= today <= end_date:
                return "\U0001F7E2", f"Offer open until {format_date(end_date)}"
            return "\U0001F534", f"Offer closed {format_date(end_date)}"
        return "\U0001F7E1", "Subscription window open"
    if type_name == "dividend":
        if record_date:
            due_date = record_date + timedelta(days=30)
            if today > due_date:
                return "\U0001F534", "Payment window passed"
            return "\U0001F7E1", f"Payment due by {format_date(due_date)}"
        return "\U0001F7E1", "Payment pending"
    if type_name == "bonus":
        return "\U0001F7E1", "Credit within ~2 wks"
    if type_name == "split":
        return "\U0001F7E2", "Re-denominated"
    if type_name == "buyback":
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
    type_name = action_type(subject)
    type_emoji = _TYPE_EMOJI.get(type_name, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    price_line = _price_line(action)
    if price_line:
        lines.append(price_line)
    ex_date = action.get("ex_date")
    if ex_date and str(ex_date).strip() not in ("", "-"):
        lines.append(f"Ex-Date: <b>{escape(ex_date)}</b>")
    record_date = action.get("record_date")
    if record_date and str(record_date).strip() not in ("", "-"):
        lines.append(f"Record Date: {escape(record_date)}")
    announcement_date = action.get("announcement_date")
    if announcement_date and str(announcement_date).strip() not in ("", "-"):
        lines.append(f"Announced: {format_date(announcement_date)}")
    book_closure = _book_closure_line(action)
    if book_closure:
        lines.append(book_closure)
    offer = _offer_window_line(action)
    if offer:
        lines.append(offer)
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
            format_action_block(action)
            for action in sorted(upcoming, key=lambda action: action.get("ex_date") or "9999-99-99")
        ]
        sections.append(
            "<b>\U0001F4C5 Upcoming ex-dates</b>\n\n" + "\n\n".join(blocks)
        )
    else:
        sections.append(
            "<b>\U0001F4C5 Upcoming ex-dates</b>\nNone in the reminder window."
        )

    if pending:
        blocks = [format_action_block(action) for action in pending]
        sections.append(
            "<b>\U0001F4E2 Announced - ex-date not fixed yet</b>\n\n"
            + "\n\n".join(blocks)
        )

    if recent:
        blocks = [
            format_action_block(action)
            for action in sorted(
                recent, key=lambda action: action.get("ex_date") or "0000-01-01", reverse=True
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
    type_name = action_type(subject)
    label = TYPE_LABELS.get(type_name, type_name)
    company = action.get("company")
    head = f"\u2022 <b>{escape(symbol)}</b> ({escape(exchange)})"
    if company and str(company).strip() not in ("", "-"):
        head += f" - {escape(company)}"
    head += f"  Ex-date: <b>{format_date(action.get('ex_date'))}</b>"
    lines = [head]
    bits = []
    if subject != "-":
        bits.append(f"{label}: {escape(subject)}")
    price = _format_price(action)
    if price:
        bits.append(f"Price: <b>{price}</b>")
    record_date = action.get("record_date")
    if record_date and str(record_date).strip() not in ("", "-"):
        bits.append(f"Record: {format_date(record_date)}")
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
    type_name = action_type(subject)
    type_emoji = _TYPE_EMOJI.get(type_name, _TYPE_EMOJI["other"])
    lines = [
        f"{type_emoji} <b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
        f"Subject: {escape(subject)}",
    ]
    if type_name != "other":
        lines.append(f"Type: {TYPE_LABELS.get(type_name, type_name)}")
    price = _format_price(action)
    if price:
        lines.append(f"Current Price: <b>{price}</b>")
    for label, key, format_spec in (
        ("Ex-Date", "ex_date", "date"),
        ("Record Date", "record_date", "date"),
        ("Announced", "announcement_date", "date"),
        ("Face Value", "face_value", None),
        ("Series", "series", None),
        ("ISIN", "isin", None),
    ):
        value = action.get(key)
        if value and str(value).strip() and str(value).strip() != "-":
            display = format_date(value) if format_spec == "date" else escape(value)
            lines.append(f"{label}: {display}")
    offer = _offer_window_line(action)
    if offer:
        lines.append(offer)
    book_closure = _book_closure_line(action)
    if book_closure:
        lines.append(book_closure)
    lines.append(f"Status: {action_status(action)}")
    return "\n".join(lines)
