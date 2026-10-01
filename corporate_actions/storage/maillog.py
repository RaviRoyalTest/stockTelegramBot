"""Mail send log (data/mail_log.json): success/failed record per send.

Every mail the bot sends - Telegram commands, web composer, automatic daily
mails - appends one entry here (best-effort, never raises) so /emailstatus
and the web Email page can show ✅/❌ with the failure reason. Only the last
50 entries are kept.
"""
from __future__ import annotations

import logging

from .. import config
from ..core.dates import now_ist
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)

MAX_ENTRIES = 50


def load_mail_log(limit: int = 20) -> list[dict]:
    """Newest-first send entries (at most `limit`). Never raises."""
    try:
        with _lock, _file_lock(config.MAIL_LOG_FILE):
            data = read_json(config.MAIL_LOG_FILE, [])
        if not isinstance(data, list):
            return []
        return [entry for entry in reversed(data) if isinstance(entry, dict)][: max(1, limit)]
    except Exception as error:
        log.debug("load_mail_log: %s", error)
        return []


def record_mail(chat_id, kind: str, to: str, subject: str,
               ok: bool, info: str = "") -> None:
    """Append one send entry, trimming to MAX_ENTRIES. Never raises."""
    try:
        entry = {
            "at": now_ist().isoformat(timespec="seconds"),
            "chat": str(chat_id if chat_id not in (None, "") else "-"),
            "kind": str(kind or "manual"),
            "to": str(to or "")[:200],
            "subject": str(subject or "")[:200],
            "ok": bool(ok),
            "info": str(info or "")[:300],
        }
        with _lock, _file_lock(config.MAIL_LOG_FILE):
            data = read_json(config.MAIL_LOG_FILE, [])
            if not isinstance(data, list):
                data = []
            data.append(entry)
            write_json(config.MAIL_LOG_FILE, data[-MAX_ENTRIES:])
    except Exception as error:
        log.debug("record_mail: %s", error)
