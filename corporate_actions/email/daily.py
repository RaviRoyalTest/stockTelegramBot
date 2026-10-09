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

from .. import config, storage
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


def fetch_watchlist_actions(watchlist: list[dict], limit: int = 40) -> list[dict]:
    """Live corporate actions for EVERY watchlist stock (best-effort).

    Per-symbol NSE history + BSE/US feeds, sorted by ex-date (undated last),
    capped at `limit`. Never raises - failures degrade to [] so the mail
    still goes out with whatever else was gathered.
    """
    try:
        from ..poller import fetch_matching, parse_ex_date

        actions = fetch_matching(watchlist or []) or []
    except Exception as error:
        log.debug("watchlist actions fetch skipped: %s", error)
        return []

    def _sort_key(action: dict):
        # Dated first (chronological), undated at the end.
        day = None
        try:
            day = parse_ex_date(action.get("ex_date"))
        except Exception:
            day = None
        return (day is None, str(day or "9999-99-99"))

    actions.sort(key=_sort_key)
    return actions[: max(1, limit)]


def fetch_nifty_actions(limit: int = 80) -> list[dict]:
    """ALL Nifty-500 corporate actions currently in progress (best-effort).

    NSE + BSE feeds filtered to Nifty 500 constituents, NSE/BSE duplicates
    merged, sorted ex-date first. "In progress" = upcoming ex-date, recently
    passed but not completed, or announced awaiting dates. Never raises.
    """
    try:
        from ..poller import (
            action_is_completed,
            fetch_all_actions,
            parse_ex_date,
            recently_passed,
            within_reminder_window,
        )
        from ..sources.universe import get_index_universe

        universe = {str(s or "").strip().upper()
                    for s in (get_index_universe("nifty500") or [])}
    except Exception as error:
        log.debug("nifty actions setup skipped: %s", error)
        return []
    if not universe:
        return []
    try:
        all_actions, _errors, _warnings = fetch_all_actions()
    except Exception as error:
        log.debug("nifty actions fetch skipped: %s", error)
        return []

    def _is_inprogress(action: dict) -> bool:
        try:
            if within_reminder_window(action.get("ex_date")):
                return True
            if recently_passed(action.get("ex_date")) \
                    and not action_is_completed(action):
                return True
            return not parse_ex_date(action.get("ex_date"))
        except Exception:
            return False

    seen: set = set()
    rows: list[dict] = []
    for action in all_actions or []:
        symbol = str(action.get("symbol") or "").strip().upper()
        if not symbol or symbol not in universe:
            continue
        key = (symbol, str(action.get("subject") or "").strip()[:60],
               str(action.get("ex_date") or ""))
        if key in seen:
            continue
        seen.add(key)
        if _is_inprogress(action):
            rows.append(action)

    def _sort_key(action: dict):
        try:
            day = parse_ex_date(action.get("ex_date"))
        except Exception:
            day = None
        return (day is None, str(day or "9999-99-99"))

    rows.sort(key=_sort_key)
    return rows[: max(1, limit)]


def _build_grouped_action_tables(actions: list[dict], heading: str,
                                 cap_upcoming: int = 15, cap_rest: int = 10,
                                 more_hint: str = "", buy_by: bool = False) -> list[str]:
    """Shared grouped-tables renderer for watchlist + Nifty mails (pure)."""
    from ..poller import action_is_completed, parse_ex_date
    from ..poller import recently_passed, within_reminder_window

    upcoming, recent, pending = [], [], []
    for action in actions or []:
        try:
            if within_reminder_window(action.get("ex_date")):
                upcoming.append(action)
            elif recently_passed(action.get("ex_date")) \
                    and not action_is_completed(action):
                recent.append(action)
            elif not parse_ex_date(action.get("ex_date")):
                pending.append(action)
            else:
                recent.append(action)
        except Exception:
            pending.append(action)
    lines = [section(heading, "slate", "📋")]
    total = len(actions or [])
    if not total:
        lines.append('<p class="muted">No corporate actions in progress right now.</p>')
        return lines
    if upcoming:
        lines.append(f"<b>📅 Upcoming ex-dates ({len(upcoming)})</b>")
        lines.append(actions_table(upcoming, limit=cap_upcoming, buy_by=buy_by))
        if buy_by:
            lines.append(muted("Buy-by = own the shares a full trading day before ex-date "
                               "(T+1 settlement). A weekend/holiday just before ex-date "
                               "needs even earlier buying."))
    if recent:
        lines.append(f"<b>🔄 Recently passed / in progress ({len(recent)})</b>")
        lines.append(actions_table(recent, limit=cap_rest, buy_by=buy_by))
    if pending:
        lines.append(f"<b>📢 Announced · ex-date not fixed ({len(pending)})</b>")
        lines.append(actions_table(pending, limit=cap_rest, buy_by=buy_by))
    shown = min(total, cap_upcoming + 2 * cap_rest)
    if total > shown and more_hint:
        lines.append(f"<p class='muted'>…and {total - shown} more - {more_hint}.</p>")
    return lines


def build_nifty_actions_lines(actions: list[dict]) -> list[str]:
    """Colorful tables for Nifty-wide in-progress actions (pure - no I/O)."""
    return _build_grouped_action_tables(
        actions, "Nifty 500 · corporate actions in progress",
        cap_upcoming=25, cap_rest=15,
        more_hint="full list on the web /exdates page", buy_by=True)


def build_watchlist_actions_lines(actions: list[dict]) -> list[str]:
    """Colorful corporate-action tables grouped by state (pure - no I/O)."""
    if not actions:
        return [section("Corporate actions · your full watchlist", "slate", "📋"),
                '<p class="muted">No corporate actions found for your watchlist stocks.</p>']
    return _build_grouped_action_tables(
        actions, "Corporate actions · your full watchlist",
        more_hint="more in Telegram /corpactionsformylist")


def build_watchlist_block(chat_id) -> list[str]:
    """Full-watchlist corporate-action tables (+ snapshot fallback)."""
    lines: list[str] = []
    try:
        watchlist = storage.load_watchlist() if str(chat_id) == str(_owner_chat()) \
            else _chat_watchlist(chat_id)
    except Exception:
        watchlist = []
    actions = fetch_watchlist_actions(watchlist)
    if actions:
        lines.extend(build_watchlist_actions_lines(actions))
    else:
        try:
            snapshot = storage.load_snapshots() or {}
        except Exception:
            snapshot = {}
        if snapshot.get("corporate_actions"):
            lines.append("<b>📋 Corporate actions (recorded)</b>")
            lines.append(actions_table(snapshot["corporate_actions"]))
    return lines


def build_nifty_block() -> list[str]:
    """Nifty-500 in-progress tables (empty when the feed is down)."""
    try:
        nifty_actions = fetch_nifty_actions()
    except Exception as error:
        log.debug("nifty actions skipped: %s", error)
        nifty_actions = []
    if nifty_actions:
        return build_nifty_actions_lines(nifty_actions)
    return []


def build_all_actions_lines(chat_id) -> list[str]:
    """Corporate-action blocks shared by the morning + evening mails.

    Full-watchlist tables, then the Nifty-500 in-progress tables. A recorded
    snapshot fills in only when the live watchlist fetch comes back empty;
    a Nifty feed outage skips just that block. Never raises.
    """
    return build_watchlist_block(chat_id) + build_nifty_block()


def split_gaps(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(gap_downs, gap_ups) sorted most-extreme first (pure)."""
    downs = sorted((r for r in rows or [] if (r.get("gap_pct") or 0) < 0),
                   key=lambda r: r.get("gap_pct") or 0)[:8]
    ups = sorted((r for r in rows or [] if (r.get("gap_pct") or 0) > 0),
                 key=lambda r: r.get("gap_pct") or 0, reverse=True)[:8]
    return downs, ups


def fetch_morning_gaps(top_each: int = 8) -> tuple[list[dict], list[dict]]:
    """This morning's overnight gaps across NIFTY 100 (best-effort).

    One cheap chart call per ticker (cached 5 min server-side), threaded.
    Returns (gap_downs, gap_ups). Never raises - outages yield ([], []).
    """
    try:
        from concurrent.futures import ThreadPoolExecutor

        from .. import sources
        from ..sources.universe import get_index_universe

        symbols = [str(s or "").strip().upper()
                   for s in (get_index_universe("nifty100") or [])][:100]
    except Exception as error:
        log.debug("morning gaps setup skipped: %s", error)
        return [], []
    if not symbols:
        return [], []

    def _one(symbol: str) -> dict | None:
        try:
            move = sources.get_gap_change("NSE", symbol)
        except Exception:
            return None
        if not move or move.get("gap_pct") is None:
            return None
        return {"symbol": symbol, "name": move.get("name") or symbol,
                "open": move.get("open"), "gap_pct": move.get("gap_pct"),
                "move_from_open_pct": move.get("move_from_open_pct")}

    rows: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            for row in pool.map(_one, symbols):
                if row:
                    rows.append(row)
    except Exception as error:
        log.debug("morning gaps fetch skipped: %s", error)
    downs, ups = split_gaps(rows)
    return downs[: max(1, top_each)], ups[: max(1, top_each)]


def build_gaps_lines() -> list[str]:
    """Overnight gappers block for the morning mail (never raises)."""
    from .tables import gap_table

    try:
        downs, ups = fetch_morning_gaps()
    except Exception as error:
        log.debug("gaps block skipped: %s", error)
        downs, ups = [], []
    if not downs and not ups:
        # Pre-open fallback: yesterday's recorded gap-downs, honestly labeled.
        try:
            snapshot = storage.load_snapshots() or {}
        except Exception:
            snapshot = {}
        recorded = (snapshot.get("gap_downs") or [])[:8]
        lines = [section("Overnight gaps", "amber", "🌗")]
        if recorded:
            lines.append(muted("Live gaps not available yet - yesterday's recorded gap-downs:"))
            lines.append(gap_table(
                [{"symbol": r.get("symbol"), "name": r.get("symbol"),
                  "open": None, "gap_pct": r.get("gap_pct"),
                  "move_from_open_pct": None} for r in recorded], "₹"))
        else:
            lines.append(muted("Gaps appear after the 09:15 open."))
        return lines
    lines = [section("Overnight gaps · NIFTY 100", "amber", "🌗")]
    if downs:
        lines.append(f"<b>🔻 Gap-downs ({len(downs)})</b>")
        lines.append(gap_table(downs, "₹"))
    if ups:
        lines.append(f"<b>🟢 Gap-ups ({len(ups)})</b>")
        lines.append(gap_table(ups, "₹"))
    return lines


def _latest_recorded_before(today_iso: str) -> tuple[dict, str]:
    """Latest dated report strictly before today + label ({} when none)."""
    try:
        days = storage.list_openclose_dates() or []
    except Exception as error:
        log.debug("recorded-before list skipped: %s", error)
        return {}, today_iso
    for day in sorted(days, reverse=True):
        if day >= today_iso:
            continue
        try:
            doc = storage.load_openclose(day) or {}
        except Exception:
            continue
        report = _as_report(doc)
        if report.get("sections"):
            return report, _session_label(report, day)
    return {}, today_iso


def _latest_us_report() -> tuple[dict, str]:
    """Newest recorded US sections + label ({} when never recorded)."""
    try:
        days = storage.list_openclose_dates() or []
    except Exception as error:
        log.debug("us-report list skipped: %s", error)
        return {}, ""
    for day in sorted(days, reverse=True):
        try:
            doc = storage.load_openclose(day) or {}
        except Exception:
            continue
        report = _as_report(doc)
        us_sections = [s for s in report.get("sections") or []
                       if s.get("market") == "us" and s.get("universes")]
        if us_sections:
            sub = dict(report)
            sub["sections"] = us_sections
            sub["total_verified"] = sum(
                u.get("verified", 0) for s in us_sections for u in s.get("universes", []))
            sub["total_target"] = sum(
                u.get("target", 0) for s in us_sections for u in s.get("universes", []))
            return sub, _session_label(report, day)
    return {}, ""


def build_us_lines() -> list[str]:
    """Latest recorded U.S. tables for the morning mail (never raises)."""
    report, label = _latest_us_report()
    if not report.get("sections"):
        return []
    return _build_session_lines(report, label or "last recorded", "U.S. market", "", "🇺🇸")


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
    # Corporate actions for ALL watchlist stocks + ALL Nifty in-progress
    # (shared builder with the morning mail).
    lines.extend(build_all_actions_lines(chat_id))
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


def _merge_reports(base: dict, india_section: dict) -> dict:
    """Recorded report with its India section replaced by a live one (pure).

    Single shared implementation lives in storage.openclose.merge_sections
    (the save layer merges the same way) - this stays as a thin alias so
    existing callers and tests keep working.
    """
    return storage.merge_sections(_as_report(base), [india_section])


def _ensure_fresh_india(today_iso: str) -> tuple[dict, str, bool]:
    """Recorded report + label + current-flag, building a live India scan first.

    "Current" means recorded today AND carrying an India section - a US-only
    doc recorded today must never pass as the morning opening mail. When the
    record lacks a current India section AND India is OPEN right now, a live
    India-only scan runs (~30-45s) and is MERGED over the recorded doc (other
    markets' sections are kept), so the mail always lists India first with
    the US block alongside. When the market is closed no scan runs (the
    caller skips the mail instead). Returns (report, label, is_current).
    Never raises.
    """
    try:
        doc = storage.load_openclose() or {}
    except Exception as error:
        log.debug("_ensure_fresh_india load: %s", error)
        return {}, today_iso, False
    report = _as_report(doc)
    label = _session_label(report, str(doc.get("recorded_at") or today_iso)[:10])
    has_india = any(s.get("market") == "in" and s.get("universes")
                    for s in report.get("sections") or [])
    if has_india and _recorded_doc_is_current(doc, today_iso):
        return report, label, True
    try:
        from ..market.hours import is_market_open
        from ..opening_report.report import collect, save_openclose_doc

        if not bool(is_market_open("in")):
            return report, label, False
        live = collect(("in",))
        live_sections = (live.get("sections") or []) if isinstance(live, dict) else []
        india_sections = [s for s in live_sections
                          if s.get("market") == "in" and s.get("universes")]
        if not india_sections:
            return report, label, False
        merged = _merge_reports(report, india_sections[0])
        try:
            markets = tuple(sorted({*(doc.get("markets") or []), "in"}))
            save_openclose_doc(merged, markets, None, recorded_by="auto-mail")
        except Exception as error:
            log.debug("_ensure_fresh_india save: %s", error)
        return merged, _session_label(merged, today_iso), True
    except Exception as error:
        log.info("_ensure_fresh_india live build skipped: %s", error)
        return report, label, False


def maybe_send_open_email(chat_id, report: dict | None = None, force: bool = False) -> bool:
    """Five-section morning digest mail (India must be OPEN).

    (1) live opening screener + last completed close, (2) Nifty-500 actions
    with buy-by dates, (3) latest recorded U.S. tables, (4) overnight
    gappers, (5) full-watchlist actions. Skipped when closed or list-less.
    """
    try:
        # Single sender: the GitHub Actions cron (PROCESS_COMMANDS=false) runs
        # the same poll cycle - without this gate BOTH hosts would mail (and
        # both would run the live India build). Only the always-on server
        # sends automatic mails. Explicit force sends always go through.
        if not force and not config.PROCESS_COMMANDS:
            return False
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
        # An opening mail without the India list is a broken mail (e.g. a
        # US-only record). Skip without marking sent so the next cycle retries
        # instead of delivering a list-less "opening" mail.
        if not any(s.get("market") == "in" and s.get("universes")
                   for s in live.get("sections") or []):
            log.info("open mail: no India section - skipping chat %s (retry next cycle)", chat_id)
            return False
        # Five-section morning digest: (1) live open + last close,
        # (2) Nifty actions with buy-by dates, (3) U.S. tables,
        # (4) overnight gappers, (5) watchlist actions.
        lines = build_open_lines(live, label)
        prev, prev_label = _latest_recorded_before(today)
        if prev.get("sections"):
            lines.extend(build_close_lines(prev, f"{prev_label} (last close)"))
        lines.extend(build_nifty_block())
        lines.extend(build_us_lines())
        lines.extend(build_gaps_lines())
        lines.extend(build_watchlist_block(chat_id))
        ok, error = send_email(recipient, f"Royal Stock opening: {label}", lines,
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
        # Single sender (see maybe_send_open_email): cron hosts never mail.
        if not force and not config.PROCESS_COMMANDS:
            return False
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
