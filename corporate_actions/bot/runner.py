"""Bot runner: update-polling loop, diagnostics and the CLI entry point.

The always-on server (bot_server.py) long-polls Telegram itself; this module
owns the cron-style flow: process pending commands once, run one poll cycle,
push state - which is what `python run_bot.py` does on a GitHub Actions cron.
"""
from __future__ import annotations

import logging
import os
import sys

from .. import config, scheduler
from ..github import main_check, pending_state_changes, push_state
from ..poller import poller
from ..telegram.client import get_updates, is_configured
from . import dispatch
from .registry import register_commands

log = logging.getLogger(__name__)


class ImmediateStreamHandler(logging.StreamHandler):
    """Flush after every record so Render / PaaS logs appear immediately.

    When stdout is piped (not a TTY - the norm on Render), Python enables
    block buffering, so logs written with the default StreamHandler sit in
    the buffer and Render shows nothing for a long time. Flushing on every
    emit makes each log line appear in the dashboard right away.
    """

    def emit(self, record):
        super().emit(record)
        self.flush()


def process_commands() -> str | None:
    """Process any pending Telegram command updates.

    Returns the chat_id that requested /checknow, or None.
    """
    if not config.PROCESS_COMMANDS:
        log.info("PROCESS_COMMANDS=false - skipping Telegram command processing")
        return None
    if not is_configured():
        return None
    try:
        updates = get_updates()
    except Exception as error:  # broad on purpose: never let a getUpdates hiccup kill the run
        log.warning("getUpdates failed: %s", config.redact(error), exc_info=True)
        return None

    checknow_chat = None
    max_offset = 0
    for update in updates:
        update_id = update.get("update_id", 0)
        max_offset = max(max_offset, update_id)
        callback = update.get("callback_query")
        if callback:
            try:
                dispatch.handle_callback_query(callback)
            except Exception as error:  # one bad tap must not break the run
                log.warning("callback query failed: %s", config.redact(error))
            continue
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat_id = (message.get("chat") or {}).get("id")
        if not text.startswith("/"):
            try:
                dispatch.handle_query_text(chat_id, text)
            except Exception as error:  # a bad query must never break the loop
                log.warning("natural query failed: %s", config.redact(error))
            continue
        if text.strip().lower() == "/checknow":
            checknow_chat = str(chat_id)
        try:
            dispatch.handle_command(chat_id, text)
        except Exception as error:  # one bad command must not kill the cron run
            log.warning("command failed for chat %s: %s", chat_id, config.redact(error), exc_info=True)
    if max_offset:
        # Mark updates as consumed.
        get_updates(offset=max_offset + 1)
    return checknow_chat


def start_scheduled_reports() -> None:
    """Start scheduled reports in a daemon thread.

    Delegates to corporate_actions.scheduler, injecting the bot's command handler
    as the runner (dependency inversion - the scheduler never imports the bot
    package, so there is no circular import). See corporate_actions/scheduler.py
    for the loop, timing and per-user schedule logic.
    """
    scheduler.start_scheduled_reports(run_command=dispatch.handle_command)


def main():
    if any(argument.lower() == "--check" for argument in sys.argv[1:]):
        sys.exit(main_check())
    log.info("Processing Telegram commands...")
    register_commands()
    checknow_chat = process_commands()
    log.info("Running poll cycle%s...", f" (forced for {checknow_chat})" if checknow_chat else "")
    sent = poller.run_once(force=bool(checknow_chat), only_chat=checknow_chat)
    log.info("Pushing state if changed...")
    push_state()
    log.info("Done. Sent %s alert(s).", sent)
