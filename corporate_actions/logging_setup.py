"""Centralized logging configuration for every entry point.

All entry points (run_bot.py, bot_server.py, dashboard_server.py,
dashboard.py, run_dashboard.py) call :func:`setup_logging` instead of each
carrying its own ``basicConfig`` — so console output, persistent log files
and rotation behave identically everywhere.

Design:
- Console always mirrors Render/PaaS stdout expectations: INFO level by
  default, flushed immediately (the deploy's log viewer must show lines).
- Persistent logs go to ``LOG_DIR`` (default ``logs/``): ``application.log``
  rotates at 5 MB keeping 5 backups; ``error.log`` (ERROR and above) rotates
  at 2 MB keeping 5 backups. On Render the container filesystem is ephemeral —
  the files are a local-diagnosis aid, not the system of record.
- Levels: ``LOG_LEVEL`` (default INFO) for the console; file handlers stay at
  DEBUG so a short console window never loses diagnostics.
- The bot token never reaches any handler: config.redact() is applied at the
  call sites that embed URLs, and a filter scrubs it again as a backstop.

Safe to call more than once (idempotent on the root logger).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from . import config

__all__ = ["ImmediateStreamHandler", "setup_logging"]

# Module-level so tests (and embedders) can reset configuration explicitly.
_configured = False

LOG_DIR = Path(os.getenv("LOG_DIR", str(Path(__file__).resolve().parent.parent / "logs")))
LOG_FILE = LOG_DIR / "application.log"
ERROR_LOG_FILE = LOG_DIR / "error.log"

_CONSOLE_MAX_BYTES = 5 * 1024 * 1024
_CONSOLE_BACKUPS = 3
_ERROR_MAX_BYTES = 2 * 1024 * 1024
_ERROR_BACKUPS = 5

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_CONSOLE_FORMAT = "%(levelname)s %(message)s"


class ImmediateStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every record.

    When stdout is piped (the norm on Render), Python block-buffers it, so a
    default StreamHandler's lines appear long after the event. Flushing per
    record keeps PaaS log viewers live.
    """

    def emit(self, record):
        super().emit(record)
        self.flush()


class RedactTokenFilter(logging.Filter):
    """Backstop scrub of the bot token from any record reaching a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        if config.TELEGRAM_BOT_TOKEN and record.getMessage():
            record.msg = config.redact(record.msg)
            if record.args:
                record.args = tuple(
                    config.redact(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def setup_logging(
    *,
    level: int | str | None = None,
    log_dir: Path | None = None,
    console: bool = True,
) -> logging.Logger:
    """Configure root logging once; return the root logger.

    ``level`` overrides the ``LOG_LEVEL`` env var; ``log_dir`` overrides the
    ``LOG_DIR`` env var. File handlers are best-effort: a read-only or
    unwritable directory logs a warning and console logging continues (this
    must never break app startup).
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    configured_level = level if level is not None else os.getenv("LOG_LEVEL", "INFO")
    try:
        console_level = (
            configured_level
            if isinstance(configured_level, int)
            else logging.getLevelNamesMapping().get(
                str(configured_level).upper(), logging.INFO
            )
        )
    except AttributeError:  # Python < 3.11
        console_level = logging.getLevelName(str(configured_level).upper())
        if not isinstance(console_level, int):
            console_level = logging.INFO

    token_filter = RedactTokenFilter()
    global _configured
    if _configured:
        # Already configured by an earlier call/entry point - refresh the
        # console level only and keep the existing handlers.
        for handler in root.handlers:
            if isinstance(handler, ImmediateStreamHandler):
                handler.setLevel(console_level)
        return root
    _configured = True

    if console:
        stream_handler = ImmediateStreamHandler(sys.stdout)
        stream_handler.setLevel(console_level)
        stream_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        stream_handler.addFilter(token_filter)
        root.addHandler(stream_handler)

    target_dir = Path(log_dir) if log_dir is not None else LOG_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            target_dir / LOG_FILE.name,
            maxBytes=_CONSOLE_MAX_BYTES,
            backupCount=_CONSOLE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        file_handler.addFilter(token_filter)
        root.addHandler(file_handler)

        error_handler = logging.handlers.RotatingFileHandler(
            target_dir / ERROR_LOG_FILE.name,
            maxBytes=_ERROR_MAX_BYTES,
            backupCount=_ERROR_BACKUPS,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(logging.Formatter(_FORMAT))
        error_handler.addFilter(token_filter)
        root.addHandler(error_handler)
    except OSError as error:
        logging.getLogger(__name__).warning(
            "Persistent logging disabled (cannot write %s): %s", target_dir, error
        )

    return root
