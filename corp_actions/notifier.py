"""Telegram notification sending and message formatting."""
import html
import logging

import requests

from . import config, sources

log = logging.getLogger(__name__)


def escape(text) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(str(text or ""), quote=False)


class NotifierError(Exception):
    """Raised when Telegram rejects a message."""


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str, parse_mode: str = "HTML", chat_id: str | None = None) -> dict:
    """Send a text message to a chat. Returns Telegram response."""
    if not is_configured():
        raise NotifierError(
            "Telegram not configured: set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in the .env file."
        )
    target = chat_id or config.TELEGRAM_CHAT_ID
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
        return data
    except requests.RequestException as exc:
        raise NotifierError(f"Telegram send failed: {exc}") from exc


def format_corporate_action(action: dict) -> str:
    """Render a corporate action record as an HTML Telegram message."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    ex_date = action.get("ex_date") or "-"
    record_date = action.get("record_date") or "-"
    exchange = action.get("exchange") or "-"

    typ = sources.action_type(subject)
    lines = [
        f"<b>Corporate Action Alert</b>",
        f"<b>{symbol}</b> ({exchange}) - {company}",
        f"Subject: {subject}",
    ]
    if typ != "other":
        lines.append(f"Type: {sources.TYPE_LABELS.get(typ, typ)}")
    quote = action.get("quote")
    if quote and quote.get("price") is not None:
        price = quote["price"]
        currency = quote.get("currency", "INR")
        change = quote.get("change_pct")
        if change is not None:
            sign = "+" if change >= 0 else ""
            lines.append(f"Current Price: <b>{price:.2f} {currency}</b> ({sign}{change:.2f}%)")
        else:
            lines.append(f"Current Price: <b>{price:.2f} {currency}</b>")
    if ex_date and ex_date != "-":
        lines.append(f"Ex-Date: <b>{ex_date}</b>")
    if record_date and record_date != "-":
        lines.append(f"Record Date: {record_date}")
    isin = action.get("isin")
    if isin and isin != "-":
        lines.append(f"ISIN: {isin}")
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

    if price is None:
        price_txt = "n/a"
    else:
        price_txt = f"{price:.2f} {currency}"
    arrow = ""
    if change is not None:
        sign = "+" if change >= 0 else ""
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        change_txt = f"({sign}{change:.2f}% today) {arrow}"
    else:
        change_txt = ""

    return "\n".join(
        [
            f"<b>Price Alert</b> {arrow}".strip(),
            f"<b>{symbol}</b> ({exchange}) - {company}",
            f"Price: <b>{price_txt}</b> {change_txt}".strip(),
            f"Moved beyond your {threshold:g}% alert threshold.",
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
    """Compact one-line entry used by /ca and /exdate query results."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    ex_date = action.get("ex_date") or "-"
    typ = sources.action_type(subject)
    label = sources.TYPE_LABELS.get(typ, typ)
    return (
        f"\u2022 <b>{escape(symbol)}</b> ({escape(action.get('exchange'))}) "
        f"{escape(ex_date)} [{label}] - {escape(company)}"
        + (f" | {escape(subject)}" if subject != "-" else "")
    )


def format_action_detail(action: dict) -> str:
    """Full detail block for a single corporate action query result."""
    lines = [
        f"<b>{escape(action.get('symbol') or '-')}</b> "
        f"({escape(action.get('exchange'))}) - {escape(action.get('company') or '-')}",
    ]
    subject = action.get("subject")
    if subject:
        lines.append(f"Subject: {escape(subject)}")
        typ = sources.action_type(subject)
        if typ != "other":
            lines.append(f"Type: {sources.TYPE_LABELS.get(typ, typ)}")
    for label, key in (
        ("Ex-Date", "ex_date"),
        ("Record Date", "record_date"),
        ("Announced", "announcement_date"),
        ("Face Value", "face_value"),
        ("Series", "series"),
        ("ISIN", "isin"),
    ):
        val = action.get(key)
        if val and str(val).strip() and str(val).strip() != "-":
            lines.append(f"{label}: {escape(val)}")
    quote = action.get("quote")
    if quote and quote.get("price") is not None:
        price = quote["price"]
        currency = quote.get("currency", "INR")
        change = quote.get("change_pct")
        if change is not None:
            sign = "+" if change >= 0 else ""
            lines.append(
                f"Current Price: <b>{price:.2f} {currency}</b> ({sign}{change:.2f}%)"
            )
        else:
            lines.append(f"Current Price: <b>{price:.2f} {currency}</b>")
    return "\n".join(lines)
