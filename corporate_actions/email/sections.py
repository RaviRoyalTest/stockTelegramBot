"""Mail section catalogue + pure selection helpers.

One job: know which sections a mail can contain, and turn whatever the user
typed ("nifty, gaps", "all", "add us") into a validated, ordered list of
section keys. No I/O, no storage, no Telegram - so it is trivially testable
and safe to call from anywhere.

This is the single source of truth for sections. ``daily.py`` imports the
catalogue from here and re-exports the historical names, so existing call
sites keep working unchanged.
"""
from __future__ import annotations

from typing import Iterable

__all__ = [
    "MORNING_SECTIONS",
    "CLOSE_SECTIONS",
    "MORNING_SECTION_TITLES",
    "CLOSE_SECTION_TITLES",
    "SECTION_ALIASES",
    "default_sections",
    "normalize_section_name",
    "normalize_section_list",
    "validate_sections",
    "add_section",
    "remove_section",
    "describe_sections",
    "sections_for",
]

# ---- catalogue -----------------------------------------------------------
# Order matters: it is the order sections appear in a mail.
MORNING_SECTIONS = ("screener", "wk52", "nifty", "us", "gappers", "watchlist")

MORNING_SECTION_TITLES = {
    "screener": "Screener",
    "wk52": "52-week range",
    "nifty": "Nifty actions",
    "us": "U.S. markets",
    "gappers": "Gappers",
    "watchlist": "Watchlist actions",
}

# The evening mail has its own set: it leads with the CLOSING screener and
# ends with the end-of-day stored details.
CLOSE_SECTIONS = ("screener", "wk52", "nifty", "us", "eod")

CLOSE_SECTION_TITLES = {
    "screener": "Closing screener",
    "wk52": "52-week range",
    "nifty": "Nifty actions",
    "us": "U.S. markets",
    "eod": "Stored details (end of day)",
}

# Selector word -> section key. Shared by both mails: the same word means the
# same thing everywhere, so a user's muscle memory never has to change.
SECTION_ALIASES = {
    "screen": "screener", "screener": "screener", "open": "screener",
    "close": "screener", "closing": "screener",
    "wk52": "wk52", "52w": "wk52", "52week": "wk52", "52-week": "wk52",
    "52weeks": "wk52", "highlow": "wk52", "hilo": "wk52", "range": "wk52",
    "actions": "nifty", "nifty": "nifty", "buy": "nifty", "nifty500": "nifty",
    "us": "us", "usa": "us", "ustocks": "us", "us-actions": "us",
    "gap": "gappers", "gappers": "gappers", "gaps": "gappers",
    "watchlist": "watchlist", "mylist": "watchlist", "mine": "watchlist",
    "eod": "eod", "stored": "eod", "store": "eod", "settings": "eod",
}

# Words meaning "every section".
_ALL_WORDS = ("all", "everything", "digest", "full", "*")


def _catalogue(kind: str) -> tuple:
    return MORNING_SECTIONS if kind == "morning" else CLOSE_SECTIONS


def _titles(kind: str) -> dict:
    return MORNING_SECTION_TITLES if kind == "morning" else CLOSE_SECTION_TITLES


def default_sections(kind: str = "morning") -> list[str]:
    """Every section of a mail, in display order."""
    return list(_catalogue(kind))


def normalize_section_name(raw: str, kind: str = "morning") -> str | None:
    """One selector word -> section key, or None when unknown.

    'all'-style words resolve to None here; use :func:`normalize_section_list`
    for user input that may name the whole digest.
    """
    word = str(raw or "").strip().lower()
    if not word:
        return None
    key = SECTION_ALIASES.get(word)
    if key is None:
        return None
    # A word may belong to the other mail's catalogue - reject it rather than
    # silently mailing a section this mail cannot render.
    return key if key in _catalogue(kind) else None


def normalize_section_list(raw, kind: str = "morning") -> tuple[list[str], list[str]]:
    """Parse user input into (section keys, unknown words).

    Accepts a comma/space separated string or an iterable of words. 'all'
    (or None/empty) yields every section in display order. Duplicates are
    collapsed and the result is always ordered by the mail's own section
    order, so "gappers,nifty" renders identically to "nifty,gappers".

    Returns ([], []) only for empty input meaning "nothing"; unknown words
    are reported instead of being dropped silently.
    """
    if raw is None:
        return default_sections(kind), []
    if isinstance(raw, str):
        words = [w for w in raw.replace(",", " ").split() if w]
    else:
        # An iterable of tokens (e.g. an already-space-split command tail).
        # Commas are still honoured: Telegram hands us "nifty,gappers" as a
        # single token, and a user should not have to know that.
        words = []
        for item in raw:
            words.extend(w for w in str(item).replace(",", " ").split() if w)
    if not words:
        return default_sections(kind), []
    if len(words) == 1 and words[0].lower() in _ALL_WORDS:
        return default_sections(kind), []

    keys: list[str] = []
    unknown: list[str] = []
    for word in words:
        if word.lower() in _ALL_WORDS:
            continue  # a stray "all" inside a list adds nothing
        key = normalize_section_name(word, kind)
        if key is None:
            unknown.append(word)
        elif key not in keys:
            keys.append(key)
    ordered = [key for key in _catalogue(kind) if key in keys]
    return ordered, unknown


def validate_sections(keys: Iterable[str], kind: str = "morning") -> list[str]:
    """Keep only sections this mail can render, in display order.

    Used when READING a stored preference: a section removed from the
    catalogue in a later release must not crash the mail.
    """
    wanted = {str(k).strip().lower() for k in keys or ()}
    return [key for key in _catalogue(kind) if key in wanted]


def add_section(current: Iterable[str], raw: str, kind: str = "morning") -> tuple[list[str], str | None]:
    """Add one section to a stored list. Returns (new list, error)."""
    key = normalize_section_name(raw, kind)
    if key is None:
        return list(current or []), f"unknown section {raw!r}"
    keys = [k for k in (current or [])]
    if key in keys:
        return keys, None
    keys.append(key)
    return validate_sections(keys, kind), None


def remove_section(current: Iterable[str], raw: str, kind: str = "morning") -> tuple[list[str], str | None]:
    """Remove one section from a stored list. Returns (new list, error)."""
    key = normalize_section_name(raw, kind)
    if key is None:
        return list(current or []), f"unknown section {raw!r}"
    keys = [k for k in (current or []) if k != key]
    return validate_sections(keys, kind), None


def describe_sections(keys: Iterable[str], kind: str = "morning") -> str:
    """Human-readable 'Screener, Gappers' - falls back to the key if untitled."""
    titles = _titles(kind)
    return ", ".join(titles.get(k, k) for k in keys or ())


def sections_for(settings: dict | None, kind: str = "morning") -> list[str]:
    """A chat's chosen sections, falling back to every section.

    Reads the per-user ``email_sections`` (morning) / ``email_close_sections``
    (evening) preference. An absent, empty or invalid preference means "all"
    - never "nothing", so a user can never accidentally silence their own
    mail by leaving the setting blank.
    """
    key = "email_sections" if kind == "morning" else "email_close_sections"
    stored = (settings or {}).get(key)
    if not stored:
        return default_sections(kind)
    chosen = validate_sections(stored, kind)
    return chosen or default_sections(kind)
