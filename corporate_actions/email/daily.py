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
from .tables import actions_table, esc, index_table, kv_table, section, stock_table
from .tables import watchlist_table

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


def build_open_lines(report: dict, session_label: str) -> list[str]:
    """Colorful opening-session screener tables (pure - no network/disk)."""
    report = _as_report(report)
    lines = [
        section(f"Opening session screener · {session_label}", "green", "🌅"),
        f"<p>Top gainers &amp; losers across the official universes "
        f"(regular-session data only). Verified "
        f"<b>{esc(report.get('total_verified', '?'))}/{esc(report.get('total_target', '?'))}</b>.</p>",
    ]
    sections = report.get("sections") or []
    if not sections:
        lines.append('<p class="muted">No recorded session yet - run /openreport first.</p>')
        return lines
    for block in sections:
        if block.get("closed"):
            lines.append(section(f"{block.get('label', block.get('market', ''))} market closed", "slate", "🔴"))
            lines.append(f"<p class='muted'>{esc(block.get('reason') or '')}</p>")
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
                lines.append('<p class="muted">Universe unavailable - no rows fabricated.</p>')
                continue
            lines.append("<b>🟢 Top gainers</b>")
            lines.append(stock_table(universe.get("gainers") or [], "₹" if block.get("market") == "in" else "$"))
            lines.append("<b>🔴 Top losers</b>")
            lines.append(stock_table(universe.get("losers") or [], "₹" if block.get("market") == "in" else "$"))
        if block.get("indices"):
            lines.append(section("Market overview", "slate", "📈"))
            lines.append(index_table(block["indices"]))
    lines.append("<p class='muted'>Change % is vs the previous close. Open /openreport on the web for sortable tables.</p>")
    return lines


def build_close_lines(report: dict, session_label: str) -> list[str]:
    """Colorful closing-session screener tables (pure - no network/disk)."""
    lines = build_open_lines(report, session_label)
    # Re-title the banner from Opening -> Closing without rebuilding tables.
    if lines:
        lines[0] = section(f"Closing session screener · {session_label}", "red", "🌇")
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
        lines.append('<p class="muted">No scheduled reports - add one with /schedule add 3h /scan500.</p>')
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
    lines.append("<p class='muted'>Manage with /dailyemail off · full tables on the web Sessions tab.</p>")
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


def maybe_send_open_email(chat_id, report: dict | None = None, force: bool = False) -> bool:
    """Morning opening-screener mail (recorded file, never a live 60-90s scan here)."""
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
        live, label = (report, _session_label(_as_report(report), today)) if report else _recorded_openclose()
        live = _as_report(live)
        if not live.get("sections"):
            log.info("open mail: no recorded openclose yet - skipping chat %s", chat_id)
            return False
        ok, error = send_email(recipient, f"Royal Stock opening: {label}", build_open_lines(live, label))
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
    """Evening closing-screener + stored-details mail (colorful tables)."""
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
        live, label = (report, _session_label(_as_report(report), today)) if report else _recorded_openclose()
        live = _as_report(live)
        lines: list[str] = []
        if live.get("sections"):
            lines.extend(build_close_lines(live, label))
        else:
            # No recorded session yet - degrade to the legacy snapshot digest
            # so the EOD mail is never empty.
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
        ok, error = send_email(recipient, f"Royal Stock close + EOD: {label}", lines)
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
