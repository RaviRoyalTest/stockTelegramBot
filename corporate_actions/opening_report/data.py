"""Regular-session market data for the opening/closing screener.

Honours the strict data rules: regular-session Yahoo bars only
(includePrePost=false), no fabricated price/volume/market-cap, N/A for any
unreliable field, official NSE index CSVs for the Indian universes and
per-ticker Yahoo market caps for the US Mega/Large split.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import config
from ..sources.http import _quote_session, _throttle_chart_req

log = logging.getLogger(__name__)

_UNIVERSE_CACHE: dict = {}
_UNIVERSE_TTL = 86400  # constituents change rarely

_MICROCAP_CSV = "https://archives.nseindia.com/content/indices/ind_niftymicrocap250_list.csv"

_INDEX_TICKERS = {
    "in": [
        ("Nifty 50", "^NSEI"),
        ("Sensex", "^BSESN"),
        ("Nifty Bank", "^NSEBANK"),
        ("Nifty Midcap 150", "NIFTYMIDCAP150.NS"),
        ("Nifty Smallcap 100", "^CNXSC"),
        ("Nifty Microcap 250", "NIFTY_MICROCAP250.NS"),
    ],
    "us": [
        ("S&P 500", "^GSPC"),
        ("Nasdaq Composite", "^IXIC"),
        ("Dow Jones", "^DJI"),
        ("Russell 2000", "^RUT"),
    ],
}


def _yahoo_suffix(exchange: str) -> str:
    ex = (exchange or "").upper()
    return ".BO" if ex == "BSE" else ("" if ex == "US" else ".NS")


def _clean_symbols(symbols) -> list[str]:
    """Drop NSE 'Dummy...' placeholder rows and dedupe, preserving order.

    Some official NSE index CSVs (notably Nifty 500 and Nifty Microcap 250)
    carry corporate-action adjustment placeholder rows named like
    'DUMMYHEG'. They are not real constituents and must never enter a
    screener universe.
    """
    cleaned: list[str] = []
    for symbol in symbols or []:
        text = str(symbol or "").strip()
        if not text or text.upper().startswith("DUMMY"):
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _fetch_index_csv(url: str) -> list[str]:
    import csv as _csv
    import io as _io

    cached = _UNIVERSE_CACHE.get(url)
    now = time.time()
    if cached and now - cached["timestamp"] < _UNIVERSE_TTL:
        return cached["data"]
    symbols: list[str] = []
    try:
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        text = response.text
        if text.startswith("﻿"):
            text = text[1:]
        for row in _csv.DictReader(_io.StringIO(text)):
            symbol = (row.get("Symbol") or row.get("SYMBOL") or "").strip()
            # NSE places "Dummy ..." placeholder rows in some index CSVs (e.g.
            # the Microcap 250 list) for corporate-action adjustments - they
            # are not constituents, so they must never enter a universe.
            if not symbol or symbol.upper().startswith("DUMMY"):
                continue
            if symbol not in symbols:
                symbols.append(symbol)
    except Exception as error:
        log.warning("index CSV fetch failed (%s): %s", url, error)
    if symbols:
        _UNIVERSE_CACHE[url] = {"timestamp": now, "data": symbols}
    return symbols


def get_microcap250() -> list[str]:
    """Official Nifty Microcap 250 constituents (NSE archives CSV)."""
    return _clean_symbols(_fetch_index_csv(_MICROCAP_CSV))


def get_nifty100() -> list[str]:
    from ..sources.universe import get_index_universe
    return _clean_symbols(get_index_universe("nifty100"))


def get_nifty500() -> list[str]:
    from ..sources.universe import get_index_universe
    return _clean_symbols(get_index_universe("nifty500"))


def get_nifty500_ex_100() -> list[str]:
    """Nifty 500 minus the official Nifty 100 constituents (exact set diff)."""
    n100 = set(get_nifty100())
    return [s for s in get_nifty500() if s not in n100]


def get_us_universe() -> list[str]:
    """S&P 500 + NASDAQ 100 (deduped) - broad enough to cover Mega + Large cap."""
    from ..sources.universe import get_index_universe
    seen: list[str] = []
    for key in ("sp500", "nasdaq100"):
        for symbol in get_index_universe(key):
            if symbol not in seen:
                seen.append(symbol)
    return seen



def _session_rows(quote: dict) -> list[tuple]:
    """[(index, close, volume)] for every bar with a usable close, oldest first.

    Bar-index aligned: the last entry is the current (or most recent) session,
    the one before it is the previous completed session. Yahoo's meta
    'chartPreviousClose' is deliberately NOT used - for range=5d/10d it is the
    close BEFORE THE FIRST BAR of the window (5+ sessions back), not the
    previous trading day's close, and using it silently reports a multi-day
    move as a one-day move.
    """
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    rows: list[tuple] = []
    for index in range(len(closes)):
        close = closes[index]
        if close is None:
            continue
        volume = volumes[index] if index < len(volumes) else None
        rows.append((index, close, volume))
    return rows


def _utc_date(epoch: float):
    """Calendar date (UTC) of a Yahoo bar timestamp."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).date()


def _historical_snapshot(quote: dict, timestamps: list, end_epoch: float):
    """(close, volume, prev_close, prev_volume) for the session ON end_date.

    Works on timestamp-aligned triples BEFORE null-close dropping, because
    Yahoo's period-mode responses sometimes emit phantom bars (a timestamp
    with null OHLC, e.g. a holiday) which would misalign a rows-index guard.
    The target date must have a bar WITH a usable close; the reference bar is
    the nearest earlier one with a close (a phantom bar carries no data and
    can never be a real previous session). Returns None when the target date
    has no usable bar - the caller reports N/A rather than guessing.
    """
    end_date = _utc_date(end_epoch)
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    triples: list[tuple] = []
    for index, ts in enumerate(timestamps):
        if ts is None:
            continue
        triples.append((
            _utc_date(ts),
            closes[index] if index < len(closes) else None,
            volumes[index] if index < len(volumes) else None,
        ))
    position = next((p for p, (day, close, _v) in enumerate(triples) if day == end_date), None)
    if position is None or triples[position][1] is None:
        return None
    _day, close, volume = triples[position]
    prev_close = prev_volume = None
    for _pday, pclose, pvolume in reversed(triples[:position]):
        if pclose is not None:
            prev_close, prev_volume = pclose, pvolume
            break
    if prev_close is None:
        return None  # snapshot day is the symbol's first usable bar
    return close, volume, prev_close, prev_volume


def _fetch_regular_session(
    exchange: str, symbol: str, start: float | None = None, end: float | None = None,
) -> dict | None:
    """One Yahoo regular-session fetch for a symbol.

    range=10d&interval=1d&includePrePost=false returns ONLY regular-session
    daily bars. The last bar is the current session (in-progress while the
    market is open: its volume is today's cumulative regular volume); the bar
    before it is the previous completed session (its close = prev close, its
    volume = previous day's total). Returns None when the price cannot be
    reliably obtained. Volume Change % stays None when the previous-day
    volume is missing - never estimated.

    With start/end (epoch seconds) the fetch is a historical window ending at
    ``end``: the snapshot bar is the session on/before that date and the bar
    before it the reference day - both fully completed sessions, so a past
    date's report is at least as reliable as the live one. A symbol with no
    bar ON the date (holiday / not yet listed) returns None and is counted
    unavailable rather than ranked against a different day's close.
    """
    suffix = _yahoo_suffix(exchange)
    if start is not None and end is not None:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
            f"?period1={int(start)}&period2={int(end)}&interval=1d&includePrePost=false"
        )
    else:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}"
            "?range=10d&interval=1d&includePrePost=false"
        )
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        meta = result.get("meta") or {}
        name = (meta.get("longName") or meta.get("shortName") or "").strip()
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        quotes3 = quotes[0] or {}
        if start is not None and end is not None:
            snapshot = _historical_snapshot(quotes3, result.get("timestamp") or [], end)
            if snapshot is None:
                return None  # no usable bar ON the date - never rank another day
            last_close, today_volume, prev_close, prev_volume = snapshot
        else:
            rows = _session_rows(quotes3)
            if not rows:
                return None
            last_close, today_volume = rows[-1][1], rows[-1][2]
            prev_close = rows[-2][1] if len(rows) >= 2 else None
            prev_volume = rows[-2][2] if len(rows) >= 2 else None
        price = last_close  # historical: the completed close, never meta's live price
        change = price - prev_close
        change_pct = (change / prev_close) * 100.0
        volume_change_pct = None
        if today_volume is not None and prev_volume:
            volume_change_pct = ((today_volume - prev_volume) / prev_volume) * 100.0
        return {
            "symbol": symbol,
            "name": name or symbol,
            "price": price,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "volume": today_volume,
            "volume_change_pct": volume_change_pct,
        }
    except Exception as error:
        log.info("regular-session fetch failed for %s:%s - %s", exchange, symbol, error)
        return None


def fetch_universe_moves(
    exchange: str, symbols: list[str], max_workers: int = 16,
    start: float | None = None, end: float | None = None,
) -> list[dict]:
    """Fetch regular-session move dicts for a list of symbols in parallel.

    With start/end (epoch seconds) each row describes the completed session
    on/before ``end`` instead of the live one - see _fetch_regular_session.
    """
    rows: list[dict] = []
    if not symbols:
        return rows
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_regular_session, exchange, s, start, end): s
            for s in symbols
        }
        for future in as_completed(futures):
            row = future.result()
            if row is not None:
                rows.append(row)
    return rows


def top_gainers(rows: list[dict], count: int = 10) -> list[dict]:
    eligible = [r for r in rows if r.get("change_pct") is not None and r["change_pct"] > 0]
    return sorted(eligible, key=lambda r: r["change_pct"], reverse=True)[:count]


def top_losers(rows: list[dict], count: int = 10) -> list[dict]:
    eligible = [r for r in rows if r.get("change_pct") is not None and r["change_pct"] < 0]
    return sorted(eligible, key=lambda r: r["change_pct"])[:count]


def get_index_levels(
    market: str, start: float | None = None, end: float | None = None,
) -> list[dict]:
    """Current level, point change and % change for the market's benchmarks.

    Previous close comes from the last COMPLETED bar (see _session_rows) -
    never from meta.chartPreviousClose, which is the close before the whole
    fetch window and would misstate the day's move. With start/end the level
    is the benchmark's close on that historical session instead of live.
    """
    out: list[dict] = []
    for label, ticker in _INDEX_TICKERS.get(market, []):
        if start is not None and end is not None:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?period1={int(start)}&period2={int(end)}&interval=1d&includePrePost=false"
            )
        else:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                "?range=10d&interval=1d&includePrePost=false"
            )
        # One retry: a single throttled/timed-out request must not silently
        # drop a benchmark row (e.g. Russell 2000) from the overview.
        result = None
        for attempt in range(2):
            try:
                _throttle_chart_req()
                response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
                response.raise_for_status()
                result = response.json()["chart"]["result"][0]
                break
            except Exception as error:
                if attempt:
                    log.warning("index level failed for %s - %s", ticker, error)
        if result is None:
            out.append({"label": label, "level": None, "change": None, "change_pct": None})
            continue
        meta = result.get("meta") or {}
        quotes = (result.get("indicators") or {}).get("quote") or [{}]
        rows = _session_rows(quotes[0] or {})
        if start is not None and end is not None:
            snapshot = _historical_snapshot(
                quotes[0] or {}, result.get("timestamp") or [], end,
            )
            if snapshot is None:
                out.append({"label": label, "level": None, "change": None, "change_pct": None})
                continue
            level, _volume, prev, _pvolume = snapshot
        else:
            level = meta.get("regularMarketPrice") or (rows[-1][1] if rows else None)
            prev = rows[-2][1] if len(rows) >= 2 else None
        change = (level - prev) if (level is not None and prev) else None
        change_pct = (change / prev * 100.0) if (change is not None and prev) else None
        out.append({"label": label, "level": level, "change": change, "change_pct": change_pct})
    return out


def get_us_market_caps(symbols: list[str], max_workers: int = 12) -> dict:
    """Market cap in USD per ticker via Yahoo's batched v7/quote endpoint.

    ~100 tickers per request through the shared cookie/crumb session (the
    chart endpoint's meta does NOT carry marketCap - the previous per-ticker
    implementation read a field that never exists and returned nothing).
    Returns {symbol: market_cap_usd}. Tickers with no reliable market cap are
    simply absent - callers must treat them as unclassified, never guessed.
    """
    caps: dict = {}
    wanted = [s for s in symbols if s]
    if not wanted:
        return caps
    # Imported here (not at module top) so importing opening_report.data never
    # drags the whole fundamentals chain in for the Indian-only runs.
    from ..sources import fundamentals as _fund

    chunks = [wanted[i:i + 100] for i in range(0, len(wanted), 100)]
    for chunk in chunks:
        for attempt in range(2):  # second attempt after a 401 crumb refresh
            try:
                session, crumb = _fund._fund_session()
                if not crumb:
                    return caps
                response = session.get(
                    "https://query1.finance.yahoo.com/v7/finance/quote",
                    params={"symbols": ",".join(chunk), "crumb": crumb},
                    timeout=config.HTTP_TIMEOUT,
                )
                if response.status_code == 401 and not attempt:
                    _fund._invalidate_crumb()
                    continue
                response.raise_for_status()
                for item in response.json().get("quoteResponse", {}).get("result", []):
                    cap = item.get("marketCap")
                    if cap:
                        caps[item.get("symbol")] = float(cap)
                break
            except Exception as error:
                if attempt:
                    log.warning("us market-cap batch failed (%d tickers): %s", len(chunk), error)
    return caps


def split_us_by_cap(rows: list[dict], caps: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """Split US move rows into (mega $200B+, large $10B-$200B, unclassified).

    `caps` may be None: rows carry their own 'market_cap' captured from the
    same Yahoo fetch (the preferred path - no second round of calls). A row
    whose market cap is unknown, or below $10B, is NOT placed in either
    bucket - it is counted as unclassified so the caller reports honest
    Verified counts instead of guessing a bucket.
    """
    mega: list[dict] = []
    large: list[dict] = []
    unclassified = 0
    for row in rows:
        cap = (caps or {}).get(row["symbol"]) or row.get("market_cap")
        if cap is None:
            unclassified += 1
            continue
        if cap >= 200e9:
            mega.append(row)
        elif cap >= 10e9:
            large.append(row)
        else:
            unclassified += 1  # below $10B - belongs to neither table
    return mega, large, unclassified


def latest_session_date(market: str):
    """Date of the market's most recent regular session, from its benchmark index.

    Returns a datetime.date or None when unavailable. Used to distinguish a
    real session day from a weekend/exchange holiday: when the latest
    regular-session bar is not the market's local today, the exchange is closed
    and no opening/closing report must be produced.
    """
    ticker = "^NSEI" if market == "in" else "^GSPC"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?range=5d&interval=1d&includePrePost=false"
    )
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = [t for t in result.get("timestamp") or [] if t is not None]
        if not timestamps:
            return None
        return _utc_date(timestamps[-1])
    except Exception as error:
        log.info("latest session date failed for %s - %s", market, error)
        return None


def has_session_on(market: str, target_date) -> bool | None:
    """True/False when the market traded ON ``target_date`` (None: unknown).

    Reads ~1 year of the benchmark's daily bars ending the following day and
    looks for a bar stamped with that exact date. Used to refuse historical
    reports for holidays/weekends instead of silently reporting a different
    session's data.
    """
    import time as _time

    ticker = "^NSEI" if market == "in" else "^GSPC"
    end_epoch = _time.mktime(
        (target_date.year, target_date.month, target_date.day, 0, 0, 0, 0, 0, -1)
    ) + 2 * 86400  # the day after target, 00:00 local
    start_epoch = end_epoch - 370 * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={int(start_epoch)}&period2={int(end_epoch)}&interval=1d&includePrePost=false"
    )
    try:
        _throttle_chart_req()
        response = _quote_session().get(url, timeout=config.HTTP_TIMEOUT)
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = [t for t in result.get("timestamp") or [] if t is not None]
        return any(_utc_date(t) == target_date for t in timestamps)
    except Exception as error:
        log.info("session probe failed for %s %s - %s", market, target_date, error)
        return None

