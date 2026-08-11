"""Streamlit-bound render widgets shared across dashboard tabs.

These are the only dashboard components that call st.* directly (besides the
tabs themselves): cross-link analysis buttons, the corporate-action card and
the quick-analysis card. Pure data logic lives in helpers.py.
"""
from __future__ import annotations

import streamlit as st

from .. import config, sources
from ..formatting import status_tag
from ..formatting.actions import _TYPE_EMOJI
from ..formatting.stock import _fund_report_lines
from .helpers import fetch_analysis, fmt_change, fmt_price, tg_to_markdown


def request_analysis(sym: str, source: str) -> None:
    """Fetch and stash a linked analysis; only the requesting tab renders it."""
    if not sym:
        return
    res = fetch_analysis(sym)
    if res:
        st.session_state["linked_analysis"] = res
        st.session_state["linked_source"] = source
    else:
        st.warning(f"No data found for {sym}. Check the symbol.")


def render_linked_analysis(source: str) -> None:
    """Render the deep report requested from another view (cross-link)."""
    if st.session_state.get("linked_source") != source:
        return
    res = st.session_state.get("linked_analysis")
    if not res:
        return
    sym = res["sym"]
    # Rendered as plain widgets (no expander) so this works both at tab level
    # and inside the favourites' expanders - Streamlit forbids nested expanders.
    st.markdown(f"### 💹 {sym} — Deep fundamentals")
    st.markdown(
        tg_to_markdown("\n".join(
            _fund_report_lines(sym, res["quote"], res["fund"], include_tip=False)
        )),
        unsafe_allow_html=True,
    )


def symbol_fund_button(sym: str, key: str, source: str, show_label: bool = True) -> None:
    """Single-click deep-fundamentals button for a symbol/name.

    show_label=True (grids) renders the ticker on the button so it is never
    a bare icon; False (inline next to a card heading) renders just the icon.
    Clicking fetches the deep report and renders it below the current view.
    key must be unique across the whole app; source scopes the render.
    """
    sym = (sym or "").strip()
    if not sym:
        return
    label = f"{sym} \U0001F4B9" if show_label else "\U0001F4B9"
    if st.button(label, key=key, help=f"Deep fundamentals for {sym}",
                 type="primary", use_container_width=True):
        request_analysis(sym, source)


def action_meta_caption(a: dict) -> str:
    """One-line caption for a corporate-action card: announcement date,
    face value, ISIN, plus Book Closure and the rights Offer Window when
    the feed carries them."""
    parts = []
    if a.get("announcement_date"):
        parts.append(f"Announced: {a['announcement_date']}")
    parts.append(f"Face value: {a.get('face_value') or '-'}")
    parts.append(f"ISIN: {a.get('isin') or '-'}")
    bc = [d for d in (a.get("bc_start"), a.get("bc_end"))
          if d and str(d).strip() not in ("", "-")]
    if bc:
        parts.append("Book Closure: " + " \u2013 ".join(bc))
    rs, re_ = a.get("rights_start"), a.get("rights_end")
    if rs and re_ and str(rs).strip() not in ("", "-") and str(re_).strip() not in ("", "-"):
        parts.append(f"Offer Window: {rs} \u2192 {re_}")
    return " \u00b7 ".join(parts)


def render_ca_card(a: dict, key: str, source: str) -> None:
    """One corporate-action card with a single-click deep-fundamentals
    button right next to the symbol/name (mirrors the Telegram alert block)."""
    sym = a.get("symbol") or "-"
    company = a.get("company") or "-"
    subject = a.get("subject") or "-"
    typ = sources.action_type(subject)
    type_emoji = _TYPE_EMOJI.get(typ, _TYPE_EMOJI["other"])
    dot, tag = status_tag(a)

    h1, h2 = st.columns([1, 5])
    with h1:
        symbol_fund_button(sym, key, source, show_label=False)
    with h2:
        st.markdown(f"### {type_emoji} {sym} ({a.get('exchange')})")
        st.caption(company)
    st.markdown(f"**Subject:** {subject}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Type", sources.TYPE_LABELS.get(typ, typ))
    m2.metric("Ex-Date", a.get("ex_date") or "-")
    m3.metric("Record Date", a.get("record_date") or "-")
    q = a.get("quote") or {}
    m4.metric("Price", fmt_price(q.get("price")) if q.get("price") is not None else "-",
              delta=fmt_change(q.get("change_pct"))
              if q.get("change_pct") is not None else None)
    st.caption(f"{dot} {tag}  ·  " + action_meta_caption(a))


def render_quick_card(quote: dict, fund: dict, sym: str) -> None:
    """Render the quick analysis card widgets for one stock."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    comp_name = quote.get("name") or sym

    st.subheader(f"{comp_name} ({sym})")
    if fund.get("sector"):
        st.caption(f"Sector: {fund['sector']}")

    # Price & today's movement
    col1, col2, col3 = st.columns(3)
    if price is not None:
        col1.metric("Current Price", fmt_price(price),
                    delta=fmt_change(change_pct) if change_pct is not None else None)
    else:
        col1.metric("Current Price", "-")
    col2.metric("52W High", fmt_price(fund.get("wk52_high")) if fund.get("wk52_high") else "-")
    col3.metric("52W Low", fmt_price(fund.get("wk52_low")) if fund.get("wk52_low") else "-")

    # 52-week signal
    if price and fund.get("wk52_high") and fund.get("wk52_low"):
        try:
            lo, hi = float(fund["wk52_low"]), float(fund["wk52_high"])
            spread = hi - lo
            if spread > 0:
                pct_pos = (float(price) - lo) / spread
                if pct_pos <= 0.15:
                    sig = "✅ Strong Buy — near 52-week LOW"
                elif pct_pos <= 0.35:
                    sig = "📈 Buy Zone — low zone"
                elif pct_pos >= 0.85:
                    sig = "🚫 Avoid — at/near 52-week HIGH"
                elif pct_pos >= 0.65:
                    sig = "⚠️ High Zone — near 52-week HIGH"
                else:
                    sig = "🟡 Mid-Range — middle of 52-week range"
                st.info(sig)
        except (TypeError, ValueError):
            pass

    # RSI
    if fund.get("rsi") is not None:
        rsi = fund["rsi"]
        if rsi <= 30:
            rsi_txt = f"🟢 RSI {rsi} (Oversold)"
        elif rsi <= 45:
            rsi_txt = f"🟢 RSI {rsi} (Low)"
        elif rsi >= 70:
            rsi_txt = f"🔴 RSI {rsi} (Overbought)"
        elif rsi >= 60:
            rsi_txt = f"🔴 RSI {rsi} (High)"
        else:
            rsi_txt = f"🟡 RSI {rsi}"
        st.metric("RSI (14)", rsi_txt)

    # Fundamentals grid
    st.subheader("Valuation & Ratios")
    f1, f2, f3, f4, f5 = st.columns(5)
    f1.metric("P/E", f"{fund['pe']:.1f}" if fund.get("pe") else "N/A (Loss)")
    f2.metric("Sector P/E", f"{fund['sector_pe']:.1f}" if fund.get("sector_pe") else "-")
    f3.metric("Market Cap", f"₹{fund['market_cap']:,.0f}Cr" if fund.get("market_cap") else "-")
    f4.metric("D/E", f"{fund['debt_to_equity']:.2f}" if fund.get("debt_to_equity") else "-")
    f5.metric("Div Yield", f"{fund['div_yield']:.2f}%" if fund.get("div_yield") else "-")

    st.subheader("Profitability")
    p1, p2 = st.columns(2)
    p1.metric("ROCE", f"{fund['roce']:.1f}%" if fund.get("roce") else "-")
    p2.metric("ROE", f"{fund['roe']:.1f}%" if fund.get("roe") else "-")

    # Shareholding
    st.subheader("Shareholding Pattern (QoQ)")
    if any(fund.get(k) for k in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Promoter", fund.get("promoter_pct") or "-")
        h2.metric("FII", fund.get("fii_pct") or "-")
        h3.metric("DII", fund.get("dii_pct") or "-")
        h4.metric("Public", fund.get("public_pct") or "-")
    else:
        st.info("No shareholding breakdown available.")

    # Distance from 52 week
    if price and fund.get("wk52_high") and fund.get("wk52_low"):
        try:
            lo, hi = float(fund["wk52_low"]), float(fund["wk52_high"])
            dist_lo = ((float(price) - lo) / lo) * 100
            dist_hi = ((hi - float(price)) / hi) * 100
            st.caption(f"📍 +{dist_lo:.1f}% from 52w Low · -{dist_hi:.1f}% from 52w High")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
