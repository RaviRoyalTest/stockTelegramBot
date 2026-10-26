"""Pure, fail-closed three-way reconciliation of persisted domain state.

Never line-merge JSON. Missing values are distinct from JSON null, deletions
are edits, and unsupported concurrent changes require human recovery.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime

MISSING = object()


class StateConflict(ValueError):
    """A merge cannot be proven to preserve both writers' intent."""


def strict_json_loads(raw: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise StateConflict(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise StateConflict("nonfinite JSON number")
        return parsed

    def constant(value):
        raise StateConflict(f"nonfinite JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_float=number,
                      parse_constant=constant)


def _equal(a, b):
    # Python's True == 1 is not JSON type equality.
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _copy(value):
    return MISSING if value is MISSING else copy.deepcopy(value)


def _simple(base, local, remote):
    if _equal(local, remote):
        return True, _copy(local)
    if _equal(local, base):
        return True, _copy(remote)
    if _equal(remote, base):
        return True, _copy(local)
    return False, MISSING


def _mapping(base, local, remote, merge, label):
    if base is MISSING:
        base = {}
    if not all(isinstance(v, dict) for v in (base, local, remote)):
        raise StateConflict(f"{label}: concurrent deletion/type change")
    result = {}
    for key in sorted(base.keys() | local.keys() | remote.keys()):
        value = merge(base.get(key, MISSING), local.get(key, MISSING),
                      remote.get(key, MISSING), f"{label}/{key}")
        if value is not MISSING:
            result[key] = value
    return result


def _fields(base, local, remote, label):
    done, value = _simple(base, local, remote)
    if done:
        return value
    return _mapping(base, local, remote, _fields, label)


def _atomic(base, local, remote, label):
    done, value = _simple(base, local, remote)
    if not done:
        raise StateConflict(f"{label}: divergent edits")
    return value


def _stocks(base, local, remote, label):
    def indexed(items):
        if items is MISSING:
            return MISSING
        if not isinstance(items, list):
            raise StateConflict(f"{label}: expected stock list")
        result = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), str) \
                    or not isinstance(item.get("exchange", ""), str):
                raise StateConflict(f"{label}: invalid stock identity")
            key = (item.get("exchange", "").strip().upper(), item["symbol"].strip().upper())
            if not key[1] or key in result:
                raise StateConflict(f"{label}: missing/duplicate stock identity")
            result[key] = item
        return result

    b, l, r = (indexed(v) for v in (base, local, remote))
    done, value = _simple(b, l, r)
    if done:
        return MISSING if value is MISSING else list(value.values())
    return list(_mapping(b, l, r, _fields, label).values())


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is not None:
            return parsed
    except (AttributeError, TypeError, ValueError):
        pass
    raise StateConflict("missing/unverified capture timestamp")


def _section(section, day):
    from .storage.openclose import section_metadata

    if not isinstance(section, dict):
        raise StateConflict("unsupported report section")
    verified = section_metadata(section)
    if verified.get("identity_conflict") or verified.get("session_date") != day \
            or verified.get("market") not in ("in", "us") \
            or verified.get("mode") not in ("live", "historical"):
        raise StateConflict("unverified report session identity")
    captured = _timestamp(verified.get("captured_at"))
    if "finalized" in section and section["finalized"] is not verified["finalized"]:
        raise StateConflict("unverified report finality")
    return verified, captured


def _sections(base, local, remote, label, day):
    def indexed(items):
        if not isinstance(items, list):
            raise StateConflict("unsupported report sections")
        result = {}
        for item in items:
            verified, _ = _section(item, day)
            key = verified["market"]
            if key in result:
                raise StateConflict("duplicate report market")
            result[key] = item
        return result

    def choose(b, l, r, where):
        done, value = _simple(b, l, r)
        if done:
            return value
        if l is MISSING or r is MISSING:
            raise StateConflict(f"{where}: delete versus update")
        lv, lt = _section(l, day)
        rv, rt = _section(r, day)
        # Whole sections are observations, not independently editable fields.
        # Never synthesize a table by mixing captures; finality cannot regress.
        if lt > rt and lv["finalized"] >= rv["finalized"]:
            return _copy(l)
        if rt > lt and rv["finalized"] >= lv["finalized"]:
            return _copy(r)
        raise StateConflict(f"{where}: ambiguous capture order/finality")

    b = {} if base is MISSING else indexed(base)
    return list(_mapping(b, indexed(local), indexed(remote), choose, label).values())


def _report(base, local, remote, label, day):
    from .storage.openclose import merge_sections

    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except (ValueError, TypeError):
        raise StateConflict("report filename must identify a session") from None
    if base is MISSING:
        base = {}
    if not all(isinstance(v, dict) and isinstance(v.get("report", {}), dict)
               for v in (base, local, remote)):
        raise StateConflict("unsupported report wrapper")
    reports = [v.get("report", {}) for v in (base, local, remote)]
    sections = _sections(*(v.get("sections", MISSING) for v in reports), label, day)
    derived = {"mode", "markets", "target_date", "session_date", "finalized",
               "total_verified", "total_target", "volume_computed"}
    def clean(doc, omitted):
        return {k: v for k, v in doc.items() if k not in omitted}
    report = _fields(*(clean(v, derived | {"sections"}) for v in reports), label)
    report = merge_sections(report, sections)
    wrapper = _fields(*(clean(v, derived | {"report"}) for v in (base, local, remote)), label)
    wrapper["report"] = report
    for key in derived & {"mode", "markets", "target_date", "session_date", "finalized"}:
        wrapper[key] = report.get(key)
    return wrapper


def merge_state(kind, base, local, remote, *, label="state", session=None):
    """Merge validated JSON values; MISSING represents an absent file.

    Only explicitly supported domains get concurrent reconciliation. Unknown
    state (including schedules/caches/snapshots) is atomic and fails closed.
    """
    if kind == "seen":
        values = [v for v in (base, local, remote) if v is not MISSING]
        if not all(isinstance(v, list) and all(isinstance(k, str) for k in v) for v in values):
            raise StateConflict(f"{label}: invalid seen keys")
        # Dedup is monotonic, even if one writer pruned or deleted its cache.
        return sorted({key for value in values for key in value})
    if kind == "stocks":
        return _stocks(base, local, remote, label)
    done, value = _simple(base, local, remote)
    if done:
        return value
    if kind == "settings":
        return _fields(base, local, remote, label)
    if kind == "subscriptions":
        return _mapping(base, local, remote, _stocks, label)
    if kind == "report":
        return _report(base, local, remote, label, session)
    return _atomic(base, local, remote, label)
