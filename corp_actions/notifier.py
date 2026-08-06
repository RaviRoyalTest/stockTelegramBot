"""Telegram notification sending and message formatting."""
import logging

import requests

from . import config

log = logging.getLogger(__name__)


class NotifierError(Exception):
    """Raised when Telegram rejects a message."""


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    """Send a text message to the configured chat. Returns Telegram response."""
    if not is_configured():
        raise NotifierError(
            "Telegram not configured: set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in the .env file."
        )
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
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

    lines = [
        f"<b>Corporate Action Alert</b>",
        f"<b>{symbol}</b> ({exchange}) - {company}",
        f"Subject: {subject}",
    ]
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
