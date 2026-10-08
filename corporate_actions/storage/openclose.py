"""Recorded opening/closing session reports, one file per session date.

Layout: ``data/openclose/YYYY-MM-DD.json`` - a full report doc per trading
session, so history accumulates instead of each record clobbering the last.
``load_openclose()`` serves the latest date (falling back to the legacy
single ``data/openclose.json`` file on hosts that predate the move);
``save_openclose()`` always writes the dated file derived from the doc.
"""
from __future__ import annotations

import logging

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def _doc_date(doc: dict) -> str:
    """Session date string for a doc: explicit target, else IST record day."""
    target = (doc.get("target_date") or "").strip() if isinstance(doc, dict) else ""
    if target:
        return target
    try:
        from datetime import datetime as _dt

        try:
            from zoneinfo import ZoneInfo as _ZoneInfo
            return (
                _dt.fromisoformat(
                    str(doc.get("recorded_at") or "").replace("Z", "+00:00")
                )
                .astimezone(_ZoneInfo("Asia/Kolkata"))
                .date()
                .isoformat()
            )
        except Exception as error:
            log.debug("_doc_date: %s", error)
            pass
    except Exception as error:
        log.debug("_doc_date: %s", error)
        pass
    try:
        from ..core.dates import today_ist

        return today_ist().isoformat()
    except Exception as error:
        log.debug("_doc_date: %s", error)
        return "unknown"


def _dated_path(date_str: str):
    from pathlib import Path

    return Path(config.OPENREPORT_DIR) / f"{date_str}.json"


def list_openclose_dates() -> list[str]:
    """Sorted session dates with a recorded file (oldest first)."""
    try:
        directory = config.OPENREPORT_DIR
        if not directory.is_dir():
            return []
        return sorted(
            path.stem for path in directory.glob("*.json") if path.is_file()
        )
    except Exception as error:
        log.debug("list_openclose_dates: %s", error)
        return []


def load_openclose(date: str | None = None) -> dict:
    """Return one recorded doc: the requested date, else the latest.

    A requested date that was never recorded returns {} (no silent
    substitution - the caller decides between 404 and fallback). With no
    date, the latest dated file wins, falling back to the legacy single
    data/openclose.json file (pre-move hosts) so nothing ever reads empty
    during the transition.
    """
    requested = (date or "").strip()
    with _lock:
        if requested:
            path = _dated_path(requested)
            if path.is_file():
                with _file_lock(path):
                    data = read_json(path, {})
                if isinstance(data, dict) and data:
                    return data
            return {}
        for day in list_openclose_dates()[::-1]:
            path = _dated_path(day)
            if not path.is_file():
                continue
            with _file_lock(path):
                data = read_json(path, {})
            if isinstance(data, dict) and data:
                return data
        legacy = config.OPENREPORT_FILE
        if legacy.is_file():
            with _file_lock(legacy):
                data = read_json(legacy, {})
            if isinstance(data, dict) and data:
                return data
    return {}


def _capture_versioned(doc: dict) -> None:
    """Best-effort timestamped audit capture of one report build.

    The dated store keeps its reuse contract (one file per session, newest
    wins); this adds an append-only capture
    ``data/realtime/openclose_report/{Y}/{M}/{D}/openclose_report_<IST-timestamp>.json``
    per BUILD so same-day rebuilds never overwrite each other. Never raises:
    a capture failure must not affect the canonical save (it is logged as a
    warning instead). The directory is derived from OPENREPORT_DIR so tests
    that patch it into a temp dir keep captures hermetic too.
    """
    try:
        from .realtime import save_json_snapshot

        save_json_snapshot(
            "openclose_report",
            doc,
            source=str(doc.get("recorded_by") or "unknown"),
            base_dir=config.OPENREPORT_DIR.parent / "realtime",
        )
    except Exception as error:
        log.warning("openclose versioned capture skipped: %s", error)


def merge_sections(base_report: dict, new_sections: list) -> dict:
    """Recorded report with sections replaced per-market (pure).

    One dated file accumulates every market built for that session: a fresh
    India scan replaces only the India section(s) while the US block stays
    until the US plan rebuilds it (and vice versa). Without this, each save
    wiped the other market and readers saw single-market files flip-flopping
    through the day. Totals recompute over all sections; an empty `new`
    never wipes existing content.
    """
    base = base_report if isinstance(base_report, dict) else {}
    fresh = [dict(s) for s in (new_sections or []) if isinstance(s, dict)]
    if not fresh:
        return dict(base)
    kept = [dict(s) for s in (base.get("sections") or [])
            if isinstance(s, dict) and s.get("market") not in
            {s.get("market") for s in fresh}]
    order = {"in": 0, "us": 1}
    sections = sorted(kept + fresh,
                      key=lambda s: order.get(s.get("market"), 99))
    merged = dict(base)
    merged["sections"] = sections
    merged["total_verified"] = sum(
        u.get("verified", 0) for s in sections for u in s.get("universes", []))
    merged["total_target"] = sum(
        u.get("target", 0) for s in sections for u in s.get("universes", []))
    merged["volume_computed"] = sum(s.get("volume_computed", 0) or 0 for s in sections)
    return merged


def save_openclose(doc: dict) -> None:
    """Persist one report doc under its session date, atomically.

    Merges (never overwrites): the incoming build's sections replace only
    their own markets in the dated file, so an India scan at 10:00 and a US
    scan at 19:10 accumulate into one both-markets file instead of wiping
    each other. Wrapper fields (recorded_at/by, markets union) come from the
    latest writer.
    """
    if not isinstance(doc, dict) or not doc:
        return
    path = _dated_path(_doc_date(doc) or "unknown")
    with _lock, _file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_json(path, {})
        base_report = existing.get("report") if isinstance(existing, dict) else {}
        incoming_report = doc.get("report") if isinstance(doc.get("report"), dict) else {}
        incoming_sections = (incoming_report or {}).get("sections") or []
        base_sections = (base_report.get("sections") or []) if isinstance(base_report, dict) else []
        if incoming_sections and base_sections:
            merged_report = merge_sections(base_report, incoming_sections)
        elif incoming_sections:
            # First tables of the day: keep byte-exact old behavior.
            merged_report = incoming_report
        elif base_sections:
            # Never wipe recorded tables with an empty/failed build.
            merged_report = base_report
        else:
            merged_report = incoming_report
        merged = dict(doc)
        merged["report"] = merged_report
        base_markets = (existing.get("markets") or []) if isinstance(existing, dict) else []
        merged["markets"] = sorted({*(base_markets or []), *((doc.get("markets") or []))})
        write_json(path, merged)
    _capture_versioned(doc)


def migrate_openclose_file() -> str | None:
    """One-time seeding from the legacy single data/openclose.json file.

    Copies it to its session-dated file (never overwrites) and removes the
    legacy file so exactly one layout exists. Returns the date seeded, if any.
    Idempotent and safe to call on every import.
    """
    try:
        legacy = config.OPENREPORT_FILE
        if not legacy.is_file():
            return None
        with _lock, _file_lock(legacy):
            data = read_json(legacy, {})
        if not isinstance(data, dict) or not data:
            try:
                legacy.unlink()
            except OSError:
                pass
            return None
        day = _doc_date(data)
        target = _dated_path(day)
        if not target.is_file():
            with _lock, _file_lock(target):
                target.parent.mkdir(parents=True, exist_ok=True)
                write_json(target, data)
        try:
            legacy.unlink()
        except OSError as error:
            log.warning("could not remove legacy %s: %s", legacy, error)
            return None
        log.info("migrated legacy openclose file -> %s", day)
        return day
    except Exception as error:
        log.warning("openclose migration skipped: %s", error)
        return None
