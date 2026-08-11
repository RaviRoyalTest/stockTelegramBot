"""Telegram Bot API client: send, poll, acknowledge callbacks, set the menu.

The only module that talks to api.telegram.org - everything else goes through
send_message() / get_updates() so the protocol stays in one place.
"""
from __future__ import annotations

import logging

import requests

from .. import config
from ..core.text import strip_html

log = logging.getLogger(__name__)


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
            plain_text = strip_html(text)
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


def get_updates(offset=None):
    """Long-poll Telegram for updates (getUpdates)."""
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    log.info("getUpdates(offset=%s) -> %d update(s)", offset, len(updates))
    return updates


def answer_callback_query(callback_id) -> None:
    """Acknowledge an inline-button tap so Telegram clears the loading spinner."""
    if not callback_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=config.HTTP_TIMEOUT,
        )
    except Exception as exc:
        log.info("answerCallbackQuery failed: %s", config.redact(exc))


def set_my_commands(menu: list[dict]) -> bool:
    """Publish the bot's command menu via Telegram setMyCommands."""
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/setMyCommands"
    try:
        resp = requests.post(url, json={"commands": menu}, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        ok = bool(resp.json().get("ok"))
        log.info("setMyCommands %s", "ok" if ok else "failed")
        return ok
    except Exception as exc:
        log.warning("setMyCommands failed: %s", config.redact(exc))
        return False
