"""Recorded opening/closing session report (data/openclose.json).

Holds the LAST built open+close screener (both markets, all universes)
so it survives redeploys and can be re-shown without re-fetching hundreds
of symbols. Written automatically whenever a live report is built (bot
/openreport incl. the daily auto entries, web /openreport page) - never
for historical-date builds, which must not clobber the live record.
"""
from __future__ import annotations

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json


def load_openclose() -> dict:
    """Return the recorded report doc ({} when never recorded)."""
    with _lock, _file_lock(config.OPENREPORT_FILE):
        data = read_json(config.OPENREPORT_FILE, {})
    return data if isinstance(data, dict) else {}


def save_openclose(doc: dict) -> None:
    """Persist one report doc, atomically."""
    with _lock, _file_lock(config.OPENREPORT_FILE):
        write_json(config.OPENREPORT_FILE, doc or {})
