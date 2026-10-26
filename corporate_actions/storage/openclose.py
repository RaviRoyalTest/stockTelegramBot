"""Recorded opening/closing session reports, one file per session date.

Layout: ``data/openclose/YYYY-MM-DD.json`` - a full report doc per trading
session, so history accumulates instead of each record clobbering the last.
``load_openclose()`` serves the latest date (falling back to the legacy
single ``data/openclose.json`` file on hosts that predate the move);
``save_openclose()`` always writes the dated file derived from the doc.
"""
from __future__ import annotations

import logging
from datetime import date as Date, datetime, time, timezone
from zoneinfo import ZoneInfo

from .. import config
from .json_file import _file_lock, _lock, read_json, write_json

log = logging.getLogger(__name__)


def _iso_date(value) -> str | None:
    for fmt in ("%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(value), fmt).date().isoformat()
        except (TypeError, ValueError):
            pass
    return None


def _aware_datetime(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def section_has_data(section: dict) -> bool:
    """Whether a section has usable stock tables, not just benchmark indices."""
    if section.get("unavailable") or section.get("closed") or section.get("error"):
        return False
    return any(not u.get("unavailable") and
               (u.get("verified", 0) or u.get("gainers") or u.get("losers"))
               for u in (section.get("universes") or []) if isinstance(u, dict))


def section_metadata(
    section: dict, doc: dict | None = None, *, now: datetime | None = None,
) -> dict:
    """Copy with conservative identity inferred from section-local labels.

    Wrapper dates/timestamps are NOT evidence for a retained legacy section.
    Unknown/conflicting dates or capture times remain None. This is a pure
    read adapter, not an on-disk migration. Finality means usable stock data
    captured after that market's regular close (not merely state=CLOSED),
    with that close already reached on the validation clock. ``now`` may be
    supplied for deterministic/as-of validation; it must be timezone-aware.
    """
    out = dict(section)
    market = out.get("market")
    if market not in ("in", "us"):
        return {**out, "session_date": None, "captured_at": None,
                "mode": None, "finalized": False}
    tz = ZoneInfo("Asia/Kolkata" if market == "in" else "America/New_York")
    labels = [out.get("snapshot") or {}]
    for universe in out.get("universes") or []:
        if isinstance(universe, dict):
            labels.extend([universe.get("snapshot") or {}, universe])
    labels = [label for label in labels if isinstance(label, dict)]
    dates = {_iso_date(label.get("date")) for label in labels if label.get("date")}
    invalid_date = None in dates or bool(out.get("session_date") and
                                       not _iso_date(out["session_date"]))
    dates.discard(None)
    explicit = _iso_date(out.get("session_date"))
    if explicit:
        dates.add(explicit)
    day = next(iter(dates)) if len(dates) == 1 else None
    modes = {label.get("mode") for label in labels if label.get("mode")}
    if out.get("mode"):
        modes.add(out["mode"])
    if not modes:
        for label in labels:
            clock_label = str(label.get("time_local") or "")
            if clock_label == "Session close":
                modes.add("historical")
                continue
            try:
                datetime.strptime(clock_label, "%H:%M")
                modes.add("live")
            except ValueError:
                pass
    mode = next(iter(modes)) if len(modes) == 1 else None
    if mode not in ("live", "historical"):
        mode = None
    captured = _aware_datetime(out.get("captured_at"))
    conflict = (out.get("identity_conflict") or invalid_date or len(dates) > 1
                or len(modes) > 1 or bool(out.get("captured_at") and not captured))
    if conflict:
        out.update(identity_conflict=True, session_date=None, captured_at=None,
                   mode=None, finalized=False)
        return out
    if captured is None and day and mode == "live":
        # Every dated clock must agree; never borrow the latest writer's clock.
        clocks = set()
        for label in labels:
            label_day = _iso_date(label.get("date"))
            try:
                clock = datetime.strptime(str(label.get("time_local")), "%H:%M").time()
            except ValueError:
                continue
            if label_day:
                clocks.add(datetime.combine(Date.fromisoformat(label_day), clock, tz))
        if len(clocks) == 1:
            captured = clocks.pop()
    close = time(15, 30) if market == "in" else time(16)
    now = now if now is not None else datetime.now(timezone.utc)
    after_close = bool(day and captured and now.utcoffset() is not None and
                       captured <= now and
                       captured.astimezone(tz) >=
                       datetime.combine(Date.fromisoformat(day), close, tz))
    final = section_has_data(out) and after_close
    if "finalized" in out:
        final = final and out["finalized"] is True
    out.update(session_date=day, captured_at=captured.isoformat() if captured else None,
               mode=mode, finalized=bool(final))
    return out


def _doc_date(doc: dict) -> str | None:
    """Unambiguous section identity only; never the wrapper's IST write day."""
    days = {section_metadata(s).get("session_date")
            for s in (doc.get("report") or {}).get("sections") or []
            if isinstance(s, dict)}
    days.discard(None)
    return next(iter(days)) if len(days) == 1 else None


def _valid_date_name(value) -> bool:
    """Only canonical calendar dates may name files, never path fragments."""
    if not isinstance(value, str):
        return False
    try:
        return Date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _dated_path(date_str: str):
    from pathlib import Path

    if not _valid_date_name(date_str):
        raise ValueError("openclose date must be a valid YYYY-MM-DD calendar date")
    return Path(config.OPENREPORT_DIR) / f"{date_str}.json"


def list_openclose_dates() -> list[str]:
    """Sorted session dates with a recorded file (oldest first)."""
    try:
        directory = config.OPENREPORT_DIR
        if not directory.is_dir():
            return []
        return sorted(
            path.stem for path in directory.glob("*.json")
            if path.is_file() and path.suffix == ".json" and _valid_date_name(path.stem)
        )
    except Exception as error:
        log.debug("list_openclose_dates: %s", error)
        return []


def load_openclose(date: str | None = None) -> dict:
    """Return one recorded doc: the requested date, else the latest.

    An invalid/noncanonical date or a date never recorded returns {} (no
    path lookup or silent substitution). Only None means latest. With no
    date, the latest dated file wins, falling back to the legacy single
    data/openclose.json file (pre-move hosts) so nothing ever reads empty
    during the transition.
    """
    if date is not None and not _valid_date_name(date):
        return {}
    with _lock:
        if date is not None:
            path = _dated_path(date)
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


def merge_sections(
    base_report: dict, new_sections: list, *, now: datetime | None = None,
) -> dict:
    """Recorded report with sections replaced per-market (pure).

    One dated file accumulates every market built for that session: a fresh
    India scan replaces only the India section(s) while the US block stays
    until the US plan rebuilds it (and vice versa). Without this, each save
    wiped the other market and readers saw single-market files flip-flopping
    through the day. Totals recompute over all sections; an empty `new`
    never wipes existing content.
    """
    base = base_report if isinstance(base_report, dict) else {}
    by_market = {s.get("market"): section_metadata(s, now=now)
                 for s in (base.get("sections") or []) if isinstance(s, dict)}
    for section in new_sections or []:
        if not isinstance(section, dict):
            continue
        fresh = section_metadata(section, now=now)
        old = by_market.get(fresh.get("market"))
        same_session = old and old.get("session_date") == fresh.get("session_date")
        if old and section_has_data(old) and not section_has_data(fresh):
            continue
        if same_session and old.get("finalized") and not fresh.get("finalized"):
            continue
        old_time = _aware_datetime(old.get("captured_at")) if old else None
        new_time = _aware_datetime(fresh.get("captured_at"))
        if same_session and old_time and new_time and new_time < old_time:
            continue
        by_market[fresh.get("market")] = fresh
    order = {"in": 0, "us": 1}
    sections = sorted(by_market.values(), key=lambda s: order.get(s.get("market"), 99))
    merged = dict(base)
    merged["sections"] = sections
    modes = {s.get("mode") for s in sections}
    merged["mode"] = (next(iter(modes)) if len(modes) == 1 else
                      "mixed" if modes else base.get("mode"))
    days = {s.get("session_date") for s in sections}
    merged["session_date"] = next(iter(days)) if len(days) == 1 else None
    merged["target_date"] = (merged["session_date"]
                             if merged["mode"] == "historical" else None)
    merged["markets"] = [s["market"] for s in sections if s.get("market")]
    merged["finalized"] = bool(sections) and all(s.get("finalized") for s in sections)
    if merged["mode"] != "historical" and isinstance(merged.get("source"), str):
        merged["source"] = merged["source"].replace(
            " Historical mode: completed daily closes for the requested session.", "")
    merged["total_verified"] = sum(
        u.get("verified", 0) or 0 for s in sections
        for u in (s.get("universes") or []) if isinstance(u, dict))
    merged["total_target"] = sum(
        u.get("target", 0) or 0 for s in sections
        for u in (s.get("universes") or []) if isinstance(u, dict))
    merged["volume_computed"] = sum(s.get("volume_computed", 0) or 0 for s in sections)
    return merged


def save_openclose(doc: dict) -> None:
    """Persist one report doc under its session date, atomically.

    Merges (never overwrites): the incoming build's sections replace only
    their own markets in the dated file, so an India scan at 10:00 and a US
    scan at 19:10 accumulate into one both-markets file instead of wiping
    each other. Wrapper write timestamps are audit metadata only; identity, mode and
    markets are reconciled from the retained sections.
    """
    if not isinstance(doc, dict) or not doc:
        return
    incoming_report = doc.get("report") if isinstance(doc.get("report"), dict) else {}
    partitions: dict[str, list] = {}
    for section in incoming_report.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section = section_metadata(section, doc)
        day = section.get("session_date")
        if not day:
            log.warning("openclose: not filing ambiguous %s section", section.get("market"))
            continue
        partitions.setdefault(day, []).append(section)
    for day, sections in partitions.items():
        path = _dated_path(day)
        with _lock, _file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = read_json(path, {})
            existing = existing if isinstance(existing, dict) else {}
            base = existing.get("report") or {}
            # Retain legacy contents in place; selectors reject wrong/unknown
            # dates. Moving or deleting old sections requires a separate migration.
            report = merge_sections({**incoming_report, "sections": [], **base}, sections)
            merged = {**existing, **doc, "report": report}
            for key in ("mode", "markets", "target_date", "session_date", "finalized"):
                merged[key] = report.get(key)
            write_json(path, merged)
    _capture_versioned(doc)


def migrate_openclose_file() -> str | None:
    """One-time seeding from the legacy single data/openclose.json file.

    Copies only unambiguous single-session contents, never overwrites a dated
    file and never removes the original. Mixed/unknown identities require an
    explicit, separately reviewed migration. Returns the date seeded, if any.
    """
    try:
        legacy = config.OPENREPORT_FILE
        if not legacy.is_file():
            return None
        with _lock, _file_lock(legacy):
            data = read_json(legacy, {})
        if not isinstance(data, dict) or not data:
            return None
        day = _doc_date(data)
        sections = (data.get("report") or {}).get("sections") or []
        if not day or any(section_metadata(s).get("session_date") != day for s in sections):
            log.warning("legacy openclose migration deferred: ambiguous/mixed session identity")
            return None
        target = _dated_path(day)
        if not target.is_file():
            with _lock, _file_lock(target):
                target.parent.mkdir(parents=True, exist_ok=True)
                write_json(target, data)
        else:
            # An existing dated file does not prove the legacy contents survived.
            return None
        log.info("seeded legacy openclose file -> %s (original retained)", day)
        return day
    except Exception as error:
        log.warning("openclose migration skipped: %s", error)
        return None
