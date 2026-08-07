"""Telegram notification sending and message formatting."""
import html
import logging
from datetime import datetime, timedelta

import requests

from . import config, sources

log = logging.getLogger(__name__)


def escape(text) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(str(text or ""), quote=False)


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
        sign = "+" if change >= 0 else ""
        return f"{money} ({sign}{change:.2f}%)"
    return money


class NotifierError(Exception):
    """Raised when Telegram rejects a message."""


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def send_message(text: str, parse_mode: str = "HTML", chat_id: str | None = None) -> dict:
    """Send a text message to a chat. Returns Telegram response."""
    target = chat_id or config.TELEGRAM_CHAT_ID
    if not config.TELEGRAM_BOT_TOKEN or not target:
        raise NotifierError(
            "Telegram not configured: TELEGRAM_BOT_TOKEN or target chat_id is missing."
        )
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
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
        log.warning("Telegram send to chat %s failed: %s", target, exc)
        raise NotifierError(f"Telegram send failed: {exc}") from exc


# Emoji map for corporate action types
_TYPE_EMOJI = {
    "dividend": "\U0001F4B0",   # 💰
    "bonus": "\U0001F381",       # 🎁
    "split": "\u2702\ufe0f",     # ✂️
    "rights": "\U0001F4DC",      # 📜
    "buyback": "\U0001F501",     # 🔁
    "other": "\U0001F4CB",       # 📋
}


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
        f"<b>{escape(symbol)}</b> ({escape(exchange)}) \u2014 {escape(company)}",
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
                f"Price: <b>{fmt_money(price, currency)}</b>  "
                f"{color_icon}{arrow} <b>{sign}{change:.2f}%</b>"
            )
        else:
            lines.append(f"Price: <b>{fmt_money(price, currency)}</b>")
    if ex_date and ex_date != "-":
        lines.append(f"\U0001F4C5 Ex-Date: <b>{_fmt_date(ex_date)}</b>")
    if record_date and record_date != "-":
        lines.append(f"Record Date: {_fmt_date(record_date)}")
    isin = action.get("isin")
    if isin and isin != "-":
        lines.append(f"ISIN: {escape(isin)}")
    return "\n".join(lines)


def format_reminder(action: dict) -> str:
    """Render an 'ex-date approaching' reminder as an HTML Telegram message."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    ex_date = action.get("ex_date") or "-"
    record_date = action.get("record_date") or "-"
    exchange = action.get("exchange") or "-"

    lines = [
        "\u23f0 <b>Ex-date reminder</b>",
        f"<b>{symbol}</b> ({exchange}) - {company}",
        f"Subject: {subject}",
        f"Ex-Date: <b>{ex_date}</b>",
    ]
    if record_date and record_date != "-":
        lines.append(f"Record Date: {record_date}")
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
    lines = [
        f"\u2022 <b>{escape(symbol)}</b> ({escape(exchange)})  "
        f"Ex-date: <b>{_fmt_date(action.get('ex_date'))}</b>"
    ]
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
    """Full detail block for a single corporate action query result."""
    symbol = action.get("symbol") or "-"
    exchange = action.get("exchange") or "-"
    company = action.get("company") or "-"
    lines = [
        f"<b>{escape(symbol)}</b> ({escape(exchange)}) - {escape(company)}",
    ]
    subject = action.get("subject")
    if subject:
        lines.append(f"Subject: {escape(subject)}")
        typ = sources.action_type(subject)
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
