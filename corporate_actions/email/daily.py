"""Daily mail digests: opening screener (morning) + closing/EOD stores (evening).

Opt-in per chat with /dailyemail (needs /setemail first). Called from the
poller's per-chat loop; each digest goes at most once per IST date and never
raises - every failure degrades to a skip so the poll cycle is unaffected.

- Opening mail (IST 07:30-12:00): recorded open-session screener tables.
- Closing/EOD mail (IST >= 15:45): recorded close-session screener + the
  chat's stored details (watchlist table, schedule, settings, file counts)
  in colorful readable tables.
- Legacy /dailyemail on (no scope) means both; /dailyemail open|close|both
  narrows it. Manual /emailopen + /emailclose (/eodmail) force-send now.
"""
from __future__ import annotations

import logging

from .. import storage
from ..core.dates import now_ist, today_ist
from ..core.text import escape
from .client import is_configured as email_configured
from .client import send_email
from .tables import actions_table, esc, index_table, kv_table, muted
from .tables import section, stat_chips, stock_table, watchlist_table

log = logging.getLogger(__name__)

_LAST_SENT_KEY = "last_daily_email"
_LAST_OPEN_KEY = "last_open_email"
_LAST_CLOSE_KEY = "last_eod_email"

OPEN_START_MIN = 7 * 60 + 30  # 07:30 IST - pre-open, screener warming up
OPEN_END_MIN = 12 * 60  # 12:00 IST - mid-session cutoff for the "opening" mail
CLOSE_START_MIN = 15 * 60 + 45  # 15:45 IST - just after the India close


def get_scope(settings: dict) -> str:
    """both | open | close - legacy daily_email=True (no scope) means both."""
    scope = str((settings or {}).get("email_scope") or "").strip().lower()
    if scope in ("both", "open", "close", "eod"):
        return "close" if scope == "eod" else scope
    return "both" if (settings or {}).get("daily_email") else "off"


def wants_open(settings: dict) -> bool:
    return get_scope(settings) in ("both", "open")


def wants_close(settings: dict) -> bool:
    return get_scope(settings) in ("both", "close")


def _ist_minutes() -> int:
    now = now_ist()
    return now.hour * 60 + now.minute


def in_open_window() -> bool:
    return OPEN_START_MIN <= _ist_minutes() < OPEN_END_MIN


def in_close_window() -> bool:
    return _ist_minutes() >= CLOSE_START_MIN


def _as_report(doc: dict) -> dict:
    """Accept a storage wrapper ({report: {...}}) or a raw report."""
    if not isinstance(doc, dict):
        return {}
    report = doc.get("report") if isinstance(doc.get("report"), dict) else doc
    return report if isinstance(report, dict) else {}


def _session_label(report: dict, fallback: str) -> str:
    for key in ("target_date", "session"):
        value = str(report.get(key) or "").strip()
        if value:
            return value
    return fallback


def _money(value) -> str:
    try:
        return f"\u20b9{float(value):,.1f}"
    except (TypeError, ValueError):
        return "-"


def build_daily_lines(snapshot: dict) -> list[str]:
    """Digest lines from a snapshot doc (pure - no network, no disk)."""
    session = snapshot.get("session") or "last session"
    lines = [
        f"\U0001F4C5 <b>Daily market digest \u00b7 {escape(str(session))}</b> "
        f"({escape(str(snapshot.get('universe') or 'nifty500').upper())})",
        "",
    ]
    gaps = (snapshot.get("gap_downs") or [])[:10]
    if gaps:
        lines.append("\U0001F53B <b>Overnight gap-downs</b>")
        for row in gaps:
            lines.append(
                f"  \u2022 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"{row.get('gap_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        lines.append("")
    gainers = (snapshot.get("top_gainers") or [])[:5]
    losers = (snapshot.get("top_losers") or [])[:5]
    if gainers or losers:
        lines.append("\U0001F4C8 <b>Session movers</b>")
        for row in gainers:
            lines.append(
                f"  \U0001F7E2 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"+{row.get('change_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        for row in losers:
            lines.append(
                f"  \U0001F534 <b>{escape(str(row.get('symbol') or '?'))}</b> "
                f"{row.get('change_pct', '?')}% \u00b7 {_money(row.get('price'))}"
            )
        lines.append("")
    actions = (snapshot.get("corporate_actions") or [])[:15]
    if actions:
        lines.append("\U0001F4CB <b>Corporate actions</b>")
        for row in actions:
            lines.append(
                f"  \u2022 <b>{escape(str(row.get('symbol') or '?'))}</b> \u2014 "
                f"{escape(str(row.get('action') or ''))} "
                f"<i>ex {escape(str(row.get('ex_date') or '?'))}</i>"
            )
        lines.append("")
    lines.append(
        "<i>From your recorded session file - open the web Sessions tab "
        "for the full tables. Manage with /dailyemail off.</i>"
    )
    return lines


def _build_session_lines(report: dict, session_label: str,
                         banner: str, tone: str, emoji: str) -> list[str]:
    """Shared screener-table builder (pure - no network/disk)."""
    report = _as_report(report)
    markets = [str(block.get("market") or "").upper()
               for block in (report.get("sections") or []) if not block.get("closed")]
    lines = [
        section(f"{banner} · {session_label}", tone, emoji),
        stat_chips([
            ("Verified", f"{report.get('total_verified', '?')}/{report.get('total_target', '?')}"),
            ("Markets", " · ".join(markets) or "-"),
        ]),
        muted("Top gainers &amp; losers across the official universes "
              "(regular-session data only)."),
    ]
    sections = report.get("sections") or []
    if not sections:
        lines.append(muted("No recorded session yet - run /openreport first."))
        return lines
    for block in sections:
        if block.get("closed"):
            lines.append(section(f"{block.get('label', block.get('market', ''))} market closed", "slate", "🔴"))
            lines.append(muted(esc(block.get("reason") or "")))
            continue
        flag = "🇮🇳" if block.get("market") == "in" else "🇺🇸"
        snap = block.get("snapshot") or {}
        lines.append(section(
            f"{flag} {'India' if block.get('market') == 'in' else 'U.S.'} · "
            f"{snap.get('date', '')} {snap.get('time_local', '')} {snap.get('state', '')}".strip(),
            "", "📊",
        ))
        for universe in block.get("universes") or []:
            title = str(universe.get("title") or "?")
            pill = f"{universe.get('verified', 0)}/{universe.get('target', 20)} verified"
            lines.append(section(f"{title} · {pill}", "slate", "📦"))
            if universe.get("unavailable"):
                lines.append(muted("Universe unavailable - no rows fabricated."))
                continue
            lines.append("<b>🟢 Top gainers</b>")
            lines.append(stock_table(universe.get("gainers") or [], "₹" if block.get("market") == "in" else "$"))
            lines.append("<b>🔴 Top losers</b>")
            lines.append(stock_table(universe.get("losers") or [], "₹" if block.get("market") == "in" else "$"))
        if block.get("indices"):
            lines.append(section("Market overview", "slate", "📈"))
            lines.append(index_table(block["indices"]))
    lines.append(muted(
        "Change % is vs the previous close. Open /openreport on the web "
        "for sortable tables."))
    return lines


def build_open_lines(report: dict, session_label: str) -> list[str]:
    """Colorful opening-session screener tables (pure - no network/disk)."""
    return _build_session_lines(report, session_label, "Opening session screener", "green", "🌅")


def build_close_lines(report: dict, session_label: str) -> list[str]:
    """Colorful closing-session screener tables (pure - no network/disk)."""
    return _build_session_lines(report, session_label, "Closing session screener", "red", "🌇")


def build_full_session_lines(report: dict, session_label: str) -> list[str]:
    """Combined open + close screener tables in one block (pure).

    The recorded file holds one session, so the tables are rendered once
    under a combined banner instead of duplicating them.
    """
    return _build_session_lines(report, session_label, "Open + Close session screener", "green", "🌅🌇")


def build_combined_lines(chat_id, report: dict, session_label: str,
                         quotes: list[dict] | None = None) -> list[str]:
    """One mail body: full session tables + EOD stored-details tables."""
    lines = build_full_session_lines(report, session_label)
    lines.extend(build_eod_store_lines(chat_id, quotes))
    return lines


def fetch_watchlist_quotes(watchlist: list[dict], limit: int = 30) -> list[dict]:
    """Attach live price/change to watchlist items (best-effort, capped)."""
    from .. import sources

    out: list[dict] = []
    for item in (watchlist or [])[:limit]:
        symbol = str(item.get("symbol") or "").strip().upper()
        exchange = str(item.get("exchange") or "NSE").upper()
        if not symbol:
            continue
        row = {"symbol": symbol, "company": item.get("company") or "",
               "currency": "$" if exchange == "US" else "₹"}
        try:
            quote = sources.get_best_quote(exchange, symbol) or {}
        except Exception:
            quote = {}
        row["price"] = quote.get("price")
        row["change_pct"] = quote.get("change_pct")
        out.append(row)
    return out


def build_eod_store_lines(chat_id, quotes: list[dict] | None = None) -> list[str]:
    """End-of-day stored-details tables for one chat (pure except storage reads)."""
    settings = storage.get_user_settings(chat_id) or {}
    watchlist = storage.load_watchlist() if str(chat_id) == str(_owner_chat()) else _chat_watchlist(chat_id)
    quotes = quotes if quotes is not None else []
    quote_by_sym = {str(q.get("symbol") or "").upper(): q for q in quotes}
    items = []
    for entry in watchlist or []:
        symbol = str(entry.get("symbol") or "").upper()
        hit = quote_by_sym.get(symbol, {})
        items.append({
            "symbol": symbol, "company": entry.get("company") or "",
            "currency": "$" if str(entry.get("exchange") or "").upper() == "US" else "₹",
            "price": hit.get("price"), "change_pct": hit.get("change_pct"),
        })
    lines = [section("Your stored details · end of day", "amber", "🗂️")]
    lines.append(f"<b>📌 Watchlist ({len(items)})</b>")
    lines.append(watchlist_table(items))
    try:
        schedule = storage.load_schedule_for(chat_id) or []
    except Exception:
        schedule = []
    if schedule:
        pairs = [(
            f"#{i} every {entry.get('interval_min')}m"
            + (f" at {entry.get('run_at')}" if entry.get("run_at") else ""),
            esc(", ".join(entry.get("commands") or [])),
        ) for i, entry in enumerate(schedule)]
        lines.append("<b>⏰ Your schedule</b>")
        lines.append(kv_table(pairs))
    else:
        lines.append(muted("No scheduled reports - add one with /schedule add 3h /scan500."))
    scope = get_scope(settings)
    lines.append("<b>⚙️ Mail settings</b>")
    lines.append(kv_table([
        ("Mail id", esc(settings.get("email") or "-")),
        ("Daily digest", esc("ON (" + scope + ")" if settings.get("daily_email") else "OFF")),
        ("Alerts", esc("OFF (quiet)" if settings.get("quiet") else "ON")),
    ]))
    try:
        snap_dates = storage.list_snapshot_dates() or []
    except Exception:
        snap_dates = []
    try:
        oc_dates = storage.list_openclose_dates() or []
    except Exception:
        oc_dates = []
    lines.append("<b>💾 Stored files</b>")
    lines.append(kv_table([
        ("Recorded sessions", esc(", ".join(snap_dates[-5:]) or "-")),
        ("Open/close reports", esc(", ".join(oc_dates[-5:]) or "-")),
    ]))
    snapshot = storage.load_snapshots() or {}
    if snapshot.get("corporate_actions"):
        lines.append("<b>📋 Corporate actions (recorded)</b>")
        lines.append(actions_table(snapshot["corporate_actions"]))
    lines.append(muted("Manage with /dailyemail off · full tables on the web Sessions tab."))
    return lines


def _owner_chat():
    try:
        from .. import config

        return str(config.TELEGRAM_CHAT_ID or "local")
    except Exception:
        return "local"


def _chat_watchlist(chat_id) -> list[dict]:
    """Owner watchlist vs per-chat subscriptions (never raises)."""
    try:
        subs = storage.load_subscriptions() or {}
        items = subs.get(str(chat_id))
        if isinstance(items, list) and items:
            return items
    except Exception as error:
        log.debug("_chat_watchlist: %s", error)
    try:
        return storage.load_watchlist() or []
    except Exception:
        return []


def _recorded_openclose() -> tuple[dict, str]:
    """Latest recorded open+close doc + session label ({} when never recorded)."""
    try:
        doc = storage.load_openclose() or {}
    except Exception as error:
        log.debug("_recorded_openclose: %s", error)
        return {}, today_ist().isoformat()
    report = _as_report(doc)
    label = _session_label(report, str(doc.get("recorded_at") or today_ist().isoformat())[:10])
    if report.get("sections"):
        return report, label
    return {}, label


def _mark_sent(chat_id, key: str, today: str) -> None:
    try:
        settings = storage.get_user_settings(chat_id) or {}
        settings[key] = today
        settings[_LAST_SENT_KEY] = today  # legacy readers see "already mailed today"
        storage.save_user_settings(chat_id, settings)
    except Exception as error:
        log.debug("_mark_sent: %s", error)


_TRADED_DAY_CACHE: dict = {}  # {"day": ist-iso, "in": date|None}


def _india_open_now() -> bool:
    """True while the India market is OPEN (pure wall-clock, no network).

    Unknown (helper failure) allows the send - a broken clock must not
    silently kill every morning mail.
    """
    try:
        from ..market.hours import is_market_open

        return bool(is_market_open("in"))
    except Exception as error:
        log.debug("_india_open_now: %s", error)
        return True


def _india_traded_today() -> bool:
    """True when India completed a session today (1 cached Yahoo probe/day)."""
    try:
        today = today_ist()
        if _TRADED_DAY_CACHE.get("day") == today.isoformat() and "in" in _TRADED_DAY_CACHE:
            return _TRADED_DAY_CACHE["in"] == today
        from ..opening_report.data import latest_session_date

        session = latest_session_date("in")
        _TRADED_DAY_CACHE.clear()
        _TRADED_DAY_CACHE["day"] = today.isoformat()
        _TRADED_DAY_CACHE["in"] = session
        return session == today
    except Exception as error:
        log.debug("_india_traded_today: %s", error)
        return True


def _recorded_doc_is_current(doc: dict, today_iso: str) -> bool:
    """True when the recorded doc describes today's session (not yesterday's)."""
    try:
        report = _as_report(doc)
        if str(report.get("target_date") or "").strip() == today_iso:
            return True
        recorded_at = str(doc.get("recorded_at") or "")
        if recorded_at:
            from datetime import datetime as _dt

            try:
                from zoneinfo import ZoneInfo as _ZoneInfo

                day = _dt.fromisoformat(recorded_at.replace("Z", "+00:00"))
                if day.tzinfo is None:
                    return recorded_at[:10] == today_iso
                return day.astimezone(_ZoneInfo("Asia/Kolkata")).date().isoformat() == today_iso
            except Exception:
                return recorded_at[:10] == today_iso
        return False
    except Exception:
        return False


def _ensure_fresh_india(today_iso: str) -> tuple[dict, str, bool]:
    """Recorded report + label + current-flag, building a live India scan first.

    When the recorded file is stale/missing AND India is OPEN right now, a
    live India-only scan runs (~30-45s) and is saved, so the morning mail
    carries opening-market details instead of yesterday's file. When the
    market is closed no scan runs (the caller skips the mail instead).
    Returns (report, label, is_current). Never raises.
    """
    try:
        doc = storage.load_openclose() or {}
    except Exception as error:
        log.debug("_ensure_fresh_india load: %s", error)
        return {}, today_iso, False
    report = _as_report(doc)
    label = _session_label(report, str(doc.get("recorded_at") or today_iso)[:10])
    if report.get("sections") and _recorded_doc_is_current(doc, today_iso):
        return report, label, True
    try:
        from ..market.hours import is_market_open
        from ..opening_report.report import collect, save_openclose_doc

        if not bool(is_market_open("in")):
            return report, label, False
        live = collect(("in",))
        if isinstance(live, dict) and live.get("sections"):
            try:
                save_openclose_doc(live, ("in",), None, recorded_by="auto-mail")
            except Exception as error:
                log.debug("_ensure_fresh_india save: %s", error)
            return live, _session_label(live, today_iso), True
        return report, label, False
    except Exception as error:
        log.info("_ensure_fresh_india live build skipped: %s", error)
        return report, label, False


def maybe_send_open_email(chat_id, report: dict | None = None, force: bool = False) -> bool:
    """Morning opening-screener mail with live opening-market details.

    Sends only while India is OPEN (never pre-open, weekends or holidays).
    When the recorded file is stale/missing, a live India scan runs first so
    the mail carries this morning's details instead of yesterday's file.
    """
    try:
        settings = storage.get_user_settings(chat_id) or {}
        recipient = (settings.get("email") or "").strip()
        if not recipient or not settings.get("daily_email") or not wants_open(settings):
            return False
        today = today_ist().isoformat()
        if not force and settings.get(_LAST_OPEN_KEY) == today:
            return False
        if not force and not in_open_window():
            return False
        if not email_configured():
            return False
        if not force and not _india_open_now():
            log.info("open mail: India market closed - skipping chat %s", chat_id)
            return False
        if report is not None:
            live, label = _as_report(report), _session_label(_as_report(report), today)
        else:
            live, label, _current = _ensure_fresh_india(today)
        live = _as_report(live)
        if not live.get("sections"):
            log.info("open mail: no recorded openclose yet - skipping chat %s", chat_id)
            return False
        ok, error = send_email(recipient, f"Royal Stock opening: {label}", build_open_lines(live, label),
                                 kind="auto-open", chat_id=chat_id)
        if not ok:
            log.info("open mail failed for chat %s: %s", chat_id, error)
            return False
        _mark_sent(chat_id, _LAST_OPEN_KEY, today)
        log.info("open mail sent to chat %s (%s)", chat_id, label)
        return True
    except Exception as error:
        log.info("open mail skipped for chat %s: %s", chat_id, error)
        return False


def maybe_send_eod_email(chat_id, report: dict | None = None, force: bool = False) -> bool:
    """Evening closing-screener + stored-details mail (colorful tables).

    Sends only when India actually traded today (never on weekends or
    holidays). A stale recorded file is never mailed as today's session -
    the snapshot digest fills in instead.
    """
    try:
        settings = storage.get_user_settings(chat_id) or {}
        recipient = (settings.get("email") or "").strip()
        if not recipient or not settings.get("daily_email") or not wants_close(settings):
            return False
        today = today_ist().isoformat()
        if not force and settings.get(_LAST_CLOSE_KEY) == today:
            return False
        if not force and not in_close_window():
            return False
        if not email_configured():
            return False
        if not force and not _india_traded_today():
            log.info("eod mail: no India session today - skipping chat %s", chat_id)
            return False
        if report is not None:
            live, label, current = _as_report(report), _session_label(_as_report(report), today), True
        else:
            live, label, current = _ensure_fresh_india(today)
        live = _as_report(live)
        lines: list[str] = []
        if live.get("sections") and current:
            lines.extend(build_close_lines(live, label))
        else:
            # No current session recorded - degrade to the legacy snapshot
            # digest so the EOD mail is never empty (and never stale).
            snapshot = storage.load_snapshots() or {}
            if not snapshot.get("gap_downs") and not snapshot.get("top_gainers") \
                    and not snapshot.get("corporate_actions"):
                log.info("eod mail: nothing recorded yet - skipping chat %s", chat_id)
                return False
            lines.extend(build_daily_lines(snapshot))
            label = str(snapshot.get("session") or today)
        try:
            watchlist = storage.load_watchlist() if str(chat_id) == str(_owner_chat()) else _chat_watchlist(chat_id)
            quotes = fetch_watchlist_quotes(watchlist)
        except Exception as error:
            log.debug("eod quotes skipped: %s", error)
            quotes = []
        lines.extend(build_eod_store_lines(chat_id, quotes))
        ok, error = send_email(recipient, f"Royal Stock close + EOD: {label}", lines,
                                 kind="auto-close", chat_id=chat_id)
        if not ok:
            log.info("eod mail failed for chat %s: %s", chat_id, error)
            return False
        _mark_sent(chat_id, _LAST_CLOSE_KEY, today)
        log.info("eod mail sent to chat %s (%s)", chat_id, label)
        return True
    except Exception as error:
        log.info("eod mail skipped for chat %s: %s", chat_id, error)
        return False


def maybe_send_daily_email(chat_id) -> bool:
    """Legacy entry: morning open mail, else evening close/EOD mail when due."""
    try:
        if maybe_send_open_email(chat_id):
            return True
        return bool(maybe_send_eod_email(chat_id))
    except Exception as error:
        log.info("daily mail skipped for chat %s: %s", chat_id, error)
        return False
