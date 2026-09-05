"""Additive shared report pieces (bot + web parity).

Pure Telegram-HTML helpers that FILL the gaps in the existing renderers
without changing any existing line:

  * alias resolvers — company_name / rsi_value / mcap_cr_value, so every
    renderer reads the normalised keys (rsi14, mcap_cr, fund company) that
    the free-API layer now guarantees, with full fallback to the legacy keys.
  * financial_health_lines — current/quick ratio, D/E, interest coverage,
    operating + free cash flow (the Yahoo fields the deep report never showed).
  * peers_lines — TOP COMPETITORS from screener.in peers (the deep report
    never showed them; /forecast did).
  * sources_footer_lines — one honest "Data: ..." line so a blank field is
    never a mystery.

No fetching here — quote/fund dicts are passed in. Importing this module
never creates a cycle: it only depends on stock_common + core.
"""
from __future__ import annotations

from ..core.text import escape
from .stock_common import (
    _GREEN,
    _RED,
    _YELLOW,
    _cr_str,
    _inr_group,
    _num_or_na,
    _section,
)


def company_name(raw_symbol: str, quote: dict | None, fund: dict | None) -> str:
    """Best display name: live quote name -> fund company -> raw symbol."""
    quote = quote or {}
    fund = fund or {}
    return (
        quote.get("name")
        or quote.get("company")
        or fund.get("company")
        or fund.get("name")
        or raw_symbol
    )


def rsi_value(fund: dict | None):
    """RSI regardless of which key the fetch path stored it under."""
    fund = fund or {}
    value = fund.get("rsi")
    if value is None:
        value = fund.get("rsi14")
    return value


def mcap_cr_value(fund: dict | None):
    """Market cap in ₹ Cr regardless of which key the fetch path used."""
    fund = fund or {}
    value = fund.get("market_cap")
    if value is None:
        value = fund.get("mcap_cr")
    return value


def _health_signal(value, good: float, ok: float) -> str:
    """Traffic light: green when >= good, yellow when >= ok, else red."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number >= good:
        return f" {_GREEN}"
    if number >= ok:
        return f" {_YELLOW}"
    return f" {_RED}"


def financial_health_lines(fund: dict | None) -> list[str]:
    """Financial-health section (liquidity + leverage + cash generation).

    Covers the Yahoo fields the deep report never rendered: current ratio,
    quick ratio and operating/free cash flow, alongside D/E + interest
    coverage for one glanceable health block. Returns [] when every field
    is missing so callers simply skip the section.
    """
    fund = fund or {}
    rows: list[str] = []
    if fund.get("current_ratio") is not None:
        rows.append(
            f"Current Ratio: <b>{_num_or_na(fund['current_ratio'], 2)}x</b>"
            f"{_health_signal(fund['current_ratio'], 1.5, 1.0)}"
        )
    if fund.get("quick_ratio") is not None:
        rows.append(
            f"Quick Ratio: <b>{_num_or_na(fund['quick_ratio'], 2)}x</b>"
            f"{_health_signal(fund['quick_ratio'], 1.0, 0.7)}"
        )
    if fund.get("debt_to_equity") is not None:
        try:
            de_number = float(fund["debt_to_equity"])
        except (TypeError, ValueError):
            de_number = None
        if de_number is None:
            de_icon = ""
        elif de_number <= 0.5:
            de_icon = f" {_GREEN}"
        elif de_number <= 1.0:
            de_icon = f" {_YELLOW}"
        else:
            de_icon = f" {_RED}"
        rows.append(
            f"Debt/Equity: <b>{_num_or_na(fund['debt_to_equity'], 2)}x</b>{de_icon}"
        )
    if fund.get("interest_coverage_ratio") is not None:
        rows.append(
            f"Interest Coverage: <b>{_num_or_na(fund['interest_coverage_ratio'], 1)}x</b>"
            f"{_health_signal(fund['interest_coverage_ratio'], 3.0, 1.5)}"
        )
    if fund.get("operating_cashflow") is not None:
        rows.append(f"Operating Cash Flow: <b>{_cr_str(fund['operating_cashflow'])}</b>")
    if fund.get("free_cashflow") is not None:
        try:
            fcf_negative = float(fund["free_cashflow"]) < 0
        except (TypeError, ValueError):
            fcf_negative = False
        fcf_icon = f" {_RED}" if fcf_negative else ""
        rows.append(f"Free Cash Flow: <b>{_cr_str(fund['free_cashflow'])}</b>{fcf_icon}")
    if not rows:
        return []
    return _section("\U0001F9EA", "FINANCIAL HEALTH") + rows


def peers_lines(fund: dict | None, limit: int = 6) -> list[str]:
    """TOP COMPETITORS section from screener.in peers (price/P-E/mcap/ROCE)."""
    competitors = (fund or {}).get("competitors") or []
    if not competitors:
        return []
    out = _section("\U0001F3E2", "TOP COMPETITORS")
    out.append(f"Peers by market cap \u2014 {len(competitors)} compared:")
    for peer in competitors[:limit]:
        if isinstance(peer, str):
            out.append(f"  \u2022 <b>{escape(peer)}</b>")
            continue
        bits = []
        if peer.get("price") is not None:
            bits.append(f"CMP \u20b9{_inr_group(peer['price'], 1)}")
        if peer.get("market_cap") is not None:
            bits.append(f"MCap \u20b9{_inr_group(peer['market_cap'])}Cr")
        if peer.get("pe") is not None:
            bits.append(f"P/E {_num_or_na(peer['pe'], 1)}")
        if peer.get("roce") is not None:
            bits.append(f"ROCE {_num_or_na(peer['roce'], 1)}%")
        name = escape(peer.get("name") or peer.get("symbol") or "?")
        out.append(f"  \u2022 <b>{name}</b>" + (" \u2014 " + "  \u00b7  ".join(bits) if bits else ""))
    return out


def quote_source_tag(quote: dict | None) -> str:
    """' · via NSE' suffix for price lines, or '' when unknown/Yahoo."""
    source = ((quote or {}).get("source") or "").strip().lower()
    if source and source not in ("yahoo", "none"):
        return f" \u00b7 via {escape(source)}"
    return ""


def sources_footer_lines(quote: dict | None, fund: dict | None) -> list[str]:
    """One honest data-source line so a missing field is never a mystery."""
    fund = fund or {}
    sources: list[str] = []
    for key in ("data_sources",):
        for item in fund.get(key) or []:
            text = str(item).strip()
            if text and text not in sources:
                sources.append(text)
    quote_source = ((quote or {}).get("source") or "").strip()
    if quote_source and quote_source != "none" and quote_source not in sources:
        sources.append(f"price: {quote_source}")
    if fund.get("analyst_source") and str(fund["analyst_source"]) not in sources:
        sources.append(str(fund["analyst_source"]))
    if not sources:
        return []
    return [f"\U0001F4E1 <i>Data: {escape(' · '.join(sources))}</i>"]
