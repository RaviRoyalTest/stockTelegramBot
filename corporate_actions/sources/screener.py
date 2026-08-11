"""screener.in enrichment: sector P/E + shareholding + ratios.

screener.in rate-limits aggressively, so requests are serialised and paced,
with a simple circuit breaker that pauses enrichment for 10 minutes after a
few consecutive failures so a blocked/rate-limited screener.in never slows
the movement screens down repeatedly.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from urllib.parse import quote

from .. import config
from .http import _session

log = logging.getLogger(__name__)

_screener_lock = threading.Lock()
_last_screener_req = 0.0
_screener_fail_count = 0
_screener_blocked_until = 0.0
_SCREENER_INTERVAL = 0.05  # seconds between screener.in requests
_SCREENER_MAX_FAILS = 5  # consecutive failures before pausing
_SCREENER_BLOCK_SECONDS = 600  # pause enrichment for 10 minutes when blocked


def _screener_get(url: str) -> str | None:
    """Paced, rate-limit-safe GET of a screener.in page."""
    global _last_screener_req, _screener_fail_count, _screener_blocked_until
    now = time.time()
    with _screener_lock:
        if now < _screener_blocked_until:
            return None
        wait = _last_screener_req + _SCREENER_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _last_screener_req = time.time()
    try:
        response = _session().get(url, timeout=3.0)
        response.raise_for_status()
        text = response.text
    except Exception as error:
        log.info("screener.in fetch failed for %s - %s", url, error)
        with _screener_lock:
            _screener_fail_count += 1
            if _screener_fail_count >= _SCREENER_MAX_FAILS:
                _screener_blocked_until = time.time() + _SCREENER_BLOCK_SECONDS
                _screener_fail_count = 0
                log.warning(
                    "screener.in appears blocked - pausing enrichment for %ss",
                    _SCREENER_BLOCK_SECONDS,
                )
        return None
    with _screener_lock:
        _screener_fail_count = 0
    return text


_sector_pe_cache: dict = {}
_SECTOR_PE_CACHE_SECONDS = 86400  # 24 hours - sectors change rarely
_SECTOR_PE_RETRY_CACHE_SECONDS = 600  # 10 min when the fetch failed, so we retry soon


def get_sector_pe(slug: str) -> float | None:
    """Average P/E of a screener.in sector, from its constituent list."""
    slug = (slug or "").strip()
    if not slug:
        return None
    now = time.time()
    cached = _sector_pe_cache.get(slug)
    if cached and now - cached["timestamp"] < cached.get("time_to_live", _SECTOR_PE_CACHE_SECONDS):
        return cached["data"]
    sector_pe = None
    page = _screener_get(f"https://www.screener.in{slug}")
    if page:
        table = re.search(r"<table[^>]*>(.*?)</table>", page, re.S)
        if table:
            values = []
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S)[1:]:
                cells = [
                    re.sub(r"<[^>]+>|\s+", " ", cell).strip()
                    for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
                ]
                if len(cells) >= 4 and cells[3]:
                    try:
                        value = float(cells[3].replace(",", ""))
                        if value > 0:
                            values.append(value)
                    except ValueError:
                        continue
            if values:
                sector_pe = round(sum(values) / len(values), 1)
    # A failed/empty fetch is cached only briefly so a transient screener.in
    # outage (or the 10-min circuit breaker) doesn't suppress the sector P/E
    # for a full day.
    _sector_pe_cache[slug] = {
        "ts": now,
        "data": sector_pe,
        "ttl": _SECTOR_PE_CACHE_SECONDS if sector_pe else _SECTOR_PE_RETRY_CACHE_SECONDS,
    }
    return sector_pe


def _parse_chg(curr_str, prev_str):
    try:
        current_price = float(re.sub(r"[^\d\.-]", "", curr_str))
        previous_price = float(re.sub(r"[^\d\.-]", "", prev_str))
        diff = round(current_price - previous_price, 2)
        if diff > 0:
            return f"{curr_str} (\u25b2+{diff:.2f}%)"
        elif diff < 0:
            return f"{curr_str} (\u25bc{diff:.2f}%)"
        else:
            return curr_str
    except Exception:
        return curr_str


def parse_screener_fundamentals(symbol: str) -> dict | None:
    """Best-effort ratios (P/E, Div, D/E, 52W range, ROCE, ROE, Market Cap) + holding from screener.in."""
    page = _screener_get(f"https://www.screener.in/company/{quote(symbol)}/")
    if not page:
        return None
    out = {}
    match = re.search(r'<p class="sub">(.*?)</p>', page, re.S)
    if match:
        for link in re.finditer(
            r'<a href="(/market/[^"]+)"[^>]*title="Sector">(.*?)</a>',
            match.group(1),
            re.S,
        ):
            out["sector"] = html.unescape(
                re.sub(r"<[^>]+>|\s+", " ", link.group(2)).strip()
            )
            out["sector_pe"] = get_sector_pe(link.group(1))
            break

    # Parse top ratios block (Stock P/E, Market Cap, Dividend Yield, Debt to equity, High/Low, ROCE, ROE)
    index = page.find('id="top-ratios"')
    if index > 0:
        chunk = page[index:index + 3000]
        for list_item in re.findall(r'<li[^>]*>(.*?)</li>', chunk, re.S):
            name_m = re.search(r'<span class="name"[^>]*>(.*?)</span>', list_item, re.S)
            num_m = re.findall(r'<span class="number"[^>]*>(.*?)</span>', list_item, re.S)
            if name_m and num_m:
                name = re.sub(r'<[^>]+>|\s+', ' ', name_m.group(1)).strip().lower()
                vals = [
                    re.sub(r'<[^>]+>|\s+|,|₹', '', number_match.group(1) if hasattr(number_match, 'group') else str(number_match)).strip()
                    for number_match in num_m
                ]
                if vals and vals[0]:
                    try:
                        if 'stock p/e' in name or name == 'p/e':
                            out['pe'] = float(vals[0])
                        elif 'dividend yield' in name:
                            out['div_yield'] = float(vals[0])
                        elif 'debt to equity' in name:
                            out['debt_to_equity'] = float(vals[0])
                        elif 'roce' in name:
                            out['roce'] = float(vals[0])
                        elif 'roe' in name:
                            out['roe'] = float(vals[0])
                        elif 'market cap' in name:
                            out['market_cap'] = float(vals[0])
                        elif 'high / low' in name or 'high/low' in name:
                            if len(vals) >= 2 and vals[1]:
                                out['wk52_high'] = float(vals[0])
                                out['wk52_low'] = float(vals[1])
                    except (ValueError, IndexError):
                        pass

    index = page.find('<div id="quarterly-shp"')
    end_index = page.find('<div id="yearly-shp"')
    segment = page[index:end_index] if index > 0 and end_index > index else ""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", segment, re.S):
        first = re.search(r'<td class="text">(.*?)</td>', row, re.S)
        if not first:
            continue
        label = re.sub(r"<[^>]+>|\s+", " ", first.group(1)).strip().lower()
        cells = [
            re.sub(r"<[^>]+>|\s+", " ", cell).strip()
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        ]
        if cells:
            curr = cells[-1]
            prev = cells[-2] if len(cells) >= 2 else None
            val_str = _parse_chg(curr, prev) if prev else curr
            if label.startswith("promoter"):
                out["promoter_pct"] = val_str
            elif label.startswith("fii"):
                out["fii_pct"] = val_str
            elif label.startswith("dii"):
                out["dii_pct"] = val_str
            elif label.startswith("public"):
                out["public_pct"] = val_str
    return out or None
