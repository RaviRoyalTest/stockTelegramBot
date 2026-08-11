"""Reply helpers: send one message or a chunked series to a chat."""
from __future__ import annotations

import logging

from .. import config
from ..core.text import split_messages
from ..telegram.client import NotifierError, send_message

log = logging.getLogger(__name__)


def reply(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Send a message to a chat, splitting into chunks if text exceeds Telegram limits.

    `reply_markup` is an optional Telegram keyboard dict (see /menu).
    """
    if len(text) > 3800:
        msgs = split_messages(text.split("\n"))
        reply_messages(chat_id, msgs, reply_markup=reply_markup)
        return
    try:
        send_message(text, parse_mode=parse_mode, chat_id=chat_id, reply_markup=reply_markup)
    except NotifierError as error:
        log.warning(
            "reply to chat %s failed: %s (text: %s)",
            chat_id, config.redact(error), text[:100].replace("\n", " "),
        )


def reply_messages(
    chat_id, messages: list[str], reply_markup: dict | None = None
) -> None:
    """Send a list of pre-chunked messages; the keyboard rides on the first."""
    for index, message in enumerate(messages):
        try:
            # The keyboard rides on the first chunk; it persists in the chat
            # regardless of which message it is attached to.
            send_message(
                message,
                chat_id=chat_id,
                reply_markup=reply_markup if index == 0 else None,
            )
        except NotifierError as error:
            log.warning("query reply failed for chat %s: %s", chat_id, error)
            return
