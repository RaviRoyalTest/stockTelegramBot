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
from ..formatting.stock_common import _consensus_label
from ..formatting.stock_india import _fund_report_lines
from .helpers import fetch_analysis, format_change, format_price, tg_to_markdown


def request_analysis(symbol: str, source: str) -> None:
    """Fetch and stash a linked analysis; only the requesting tab renders it."""
    if not symbol:
        return
    result = fetch_analysis(symbol)
    if result:
        st.session_state["linked_analysis"] = result
        st.session_state["linked_source"] = source
    else:
        st.warning(f"No data found for {symbol}. Check the symbol.")


def render_linked_analysis(source: str) -> None:
    """Render the deep report requested from another view (cross-link)."""
    if st.session_state.get("linked_source") != source:
        return
    result = st.session_state.get("linked_analysis")
    if not result:
        return
    symbol = result["sym"]
    # Rendered as plain widgets (no expander) so this works both at tab level
    # and inside the favourites' expanders - Streamlit forbids nested expanders.
    st.markdown(f"### 💹 {symbol} — Deep fundamentals")
    if result.get("us"):
        from ..formatting.stock_us import _us_stock_lines
        lines = _us_stock_lines(symbol, result["quote"], result["fund"], include_tip=False)
    else:
        lines = _fund_report_lines(symbol, result["quote"], result["fund"], include_tip=False)
    st.markdown(
        tg_to_markdown("\n".join(lines)),
        unsafe_allow_html=True,
    )


def symbol_fund_button(symbol: str, key: str, source: str, show_label: bool = True) -> None:
    """Single-click deep-fundamentals button for a symbol/name.

    show_label=True (grids) renders the ticker on the button so it is never
    a bare icon; False (inline next to a card heading) renders just the icon.
    Clicking fetches the deep report and renders it below the current view.
    key must be unique across the whole app; source scopes the render.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return
    label = f"{symbol} \U0001F4B9" if show_label else "\U0001F4B9"
    if st.button(label, key=key, help=f"Deep fundamentals for {symbol}",
                 type="primary", use_container_width=True):
        request_analysis(symbol, source)


def _pick_label(match: dict) -> str:
    """'TICKER — Full Name (EXCHANGE)' label for suggestion dropdowns."""
    name = match.get("name") or match.get("company") or ""
    exchange = match.get("exchange") or ""
    tag = f" ({exchange})" if exchange else ""
    return f"{match.get('symbol', '')} — {name}{tag}"


def symbol_picker(market: str, label: str, key: str, placeholder: str,
                  default_to_first: bool = True) -> str:
    """Type-to-search symbol input with a live suggestion dropdown.

    market is 'in' (NSE/BSE) or 'us' (NASDAQ/NYSE). As the user types, the
    closest tickers (symbol + full name + exchange) appear below the box and
    they can pick one. Returns the picked suggestion's symbol, or the raw
    typed text when there are no matches / nothing was picked (so free-text
    and keyword inputs keep working). default_to_first=False prepends a
    'use as typed' option so the top suggestion never silently replaces a
    partial query (used for company-keyword search).
    """
    query = st.text_input(label, key=f"{key}_q", placeholder=placeholder).strip().upper()
    if len(query) < 2:
        return query
    matches = (
        sources.search_us_tickers(query, limit=8) if market == "us"
        else sources.search_stocks(query, limit=8)
    )
    if not matches:
        return query
    if default_to_first:
        pick = st.selectbox("Suggestions", matches, format_func=_pick_label, key=f"{key}_pick")
        return pick["symbol"]
    options = [{"symbol": query, "name": f"Use '{query}' as typed", "exchange": ""}] + matches
    pick = st.selectbox("Suggestions", options, format_func=_pick_label, key=f"{key}_pick")
    return pick["symbol"]


def action_meta_caption(action: dict) -> str:
    """One-line caption for a corporate-action card: announcement date,
    face value, ISIN, plus Book Closure and the rights Offer Window when
    the feed carries them."""
    parts = []
    if action.get("announcement_date"):
        parts.append(f"Announced: {action['announcement_date']}")
    parts.append(f"Face value: {action.get('face_value') or '-'}")
    parts.append(f"ISIN: {action.get('isin') or '-'}")
    book_closure_dates = [date for date in (action.get("book_closure_start"), action.get("book_closure_end"))
          if date and str(date).strip() not in ("", "-")]
    if book_closure_dates:
        parts.append("Book Closure: " + " \u2013 ".join(book_closure_dates))
    rights_start, rights_end = action.get("rights_start"), action.get("rights_end")
    if rights_start and rights_end and str(rights_start).strip() not in ("", "-") and str(rights_end).strip() not in ("", "-"):
        parts.append(f"Offer Window: {rights_start} \u2192 {rights_end}")
    return " \u00b7 ".join(parts)


def render_ca_card(action: dict, key: str, source: str) -> None:
    """One corporate-action card with a single-click deep-fundamentals
    button right next to the symbol/name (mirrors the Telegram alert block)."""
    symbol = action.get("symbol") or "-"
    company = action.get("company") or "-"
    subject = action.get("subject") or "-"
    type_name = sources.action_type(subject)
    type_emoji = _TYPE_EMOJI.get(type_name, _TYPE_EMOJI["other"])
    dot, tag = status_tag(action)

    symbol_col, detail_col = st.columns([1, 5])
    with symbol_col:
        symbol_fund_button(symbol, key, source, show_label=False)
    with detail_col:
        st.markdown(f"### {type_emoji} {symbol} ({action.get('exchange')})")
        st.caption(company)
    st.markdown(f"**Subject:** {subject}")
    metric_col_1, metric_col_2, metric_col_3, metric_col_4 = st.columns(4)
    metric_col_1.metric("Type", sources.TYPE_LABELS.get(type_name, type_name))
    metric_col_2.metric("Ex-Date", action.get("ex_date") or "-")
    metric_col_3.metric("Record Date", action.get("record_date") or "-")
    quote = action.get("quote") or {}
    metric_col_4.metric("Price", format_price(quote.get("price")) if quote.get("price") is not None else "-",
              delta=format_change(quote.get("change_pct"))
              if quote.get("change_pct") is not None else None)
    st.caption(f"{dot} {tag}  ·  " + action_meta_caption(action))


def render_quick_card(quote: dict, fund: dict, symbol: str) -> None:
    """Render the quick analysis card widgets for one stock."""
    price = quote.get("price")
    change_pct = quote.get("change_pct")
    company_name = quote.get("name") or symbol

    st.subheader(f"{company_name} ({symbol})")
    if fund.get("sector"):
        st.caption(f"Sector: {fund['sector']}")

    # Price & today's movement
    price_col_1, price_col_2, price_col_3 = st.columns(3)
    if price is not None:
        price_col_1.metric("Current Price", format_price(price),
                    delta=format_change(change_pct) if change_pct is not None else None)
    else:
        price_col_1.metric("Current Price", "-")
    price_col_2.metric("52W High", format_price(fund.get("wk52_high")) if fund.get("wk52_high") else "-")
    price_col_3.metric("52W Low", format_price(fund.get("wk52_low")) if fund.get("wk52_low") else "-")

    # 52-week signal
    if price and fund.get("wk52_high") and fund.get("wk52_low"):
        try:
            low, high = float(fund["wk52_low"]), float(fund["wk52_high"])
            spread = high - low
            if spread > 0:
                percent_position = (float(price) - low) / spread
                if percent_position <= 0.15:
                    signal = "✅ Strong Buy — near 52-week LOW"
                elif percent_position <= 0.35:
                    signal = "📈 Buy Zone — low zone"
                elif percent_position >= 0.85:
                    signal = "🚫 Avoid — at/near 52-week HIGH"
                elif percent_position >= 0.65:
                    signal = "⚠️ High Zone — near 52-week HIGH"
                else:
                    signal = "🟡 Mid-Range — middle of 52-week range"
                st.info(signal)
        except (TypeError, ValueError):
            pass

    # RSI
    if fund.get("rsi") is not None:
        rsi_value = fund["rsi"]
        if rsi_value <= 30:
            rsi_text = f"🟢 RSI {rsi_value} (Oversold)"
        elif rsi_value <= 45:
            rsi_text = f"🟢 RSI {rsi_value} (Low)"
        elif rsi_value >= 70:
            rsi_text = f"🔴 RSI {rsi_value} (Overbought)"
        elif rsi_value >= 60:
            rsi_text = f"🔴 RSI {rsi_value} (High)"
        else:
            rsi_text = f"🟡 RSI {rsi_value}"
        st.metric("RSI (14)", rsi_text)

    # MACD (12, 26, 9)
    if (fund.get("macd_line") is not None or fund.get("macd_signal") is not None
            or fund.get("macd_hist") is not None):
        macd_col_1, macd_col_2, macd_col_3 = st.columns(3)
        macd_col_1.metric("MACD", f"{fund['macd_line']:.2f}" if fund.get("macd_line") is not None else "-")
        macd_col_2.metric("Signal", f"{fund['macd_signal']:.2f}" if fund.get("macd_signal") is not None else "-")
        macd_col_3.metric("Histogram", f"{fund['macd_hist']:.2f}" if fund.get("macd_hist") is not None else "-")
        if fund.get("macd_line") is not None and fund.get("macd_signal") is not None:
            if fund["macd_line"] >= fund["macd_signal"]:
                st.caption("🟢 MACD above signal line — bullish crossover")
            else:
                st.caption("🔴 MACD below signal line — bearish crossover")

    # Simple moving averages
    if fund.get("sma_50") is not None or fund.get("sma_200") is not None:
        sma_col_1, sma_col_2 = st.columns(2)
        sma_col_1.metric("SMA 50", format_price(fund["sma_50"]) if fund.get("sma_50") is not None else "-")
        sma_col_2.metric("SMA 200", format_price(fund["sma_200"]) if fund.get("sma_200") is not None else "-")

    # Analyst forecast, top executive & competitors (the forecast value)
    forecast_parts = []
    if fund.get("rec_mean") is not None:
        label = _consensus_label(fund)
        forecast_parts.append(f"Consensus {label} ({fund['rec_mean']:.1f}/5)")
    if fund.get("target_mean") is not None:
        target = float(fund["target_mean"])
        forecast_parts.append(f"Target \u20b9{target:,.0f}")
        if price is not None:
            try:
                upside = (target - float(price)) / float(price) * 100.0
                forecast_parts.append(f"{upside:+.0f}% vs price")
            except (TypeError, ValueError):
                pass
    if fund.get("officers"):
        first = fund["officers"][0]
        if first.get("name"):
            st.caption(f"\U0001F464 {first['name']} \u2014 {first.get('title') or 'Director'}")
    if forecast_parts:
        st.caption("\U0001F52D " + " \u00b7 ".join(forecast_parts))
    peers = fund.get("competitors") or []
    if peers:
        st.caption("\U0001F3E2 Top competitors: " + " \u00b7 ".join(
            peer["name"] for peer in peers[:4] if peer.get("name")
        ))

    # Fundamentals grid
    st.subheader("Valuation & Ratios")
    valuation_col_1, valuation_col_2, valuation_col_3, valuation_col_4, valuation_col_5 = st.columns(5)
    valuation_col_1.metric("P/E", f"{fund['pe']:.1f}" if fund.get("pe") else "N/A (Loss)")
    valuation_col_2.metric("Sector P/E", f"{fund['sector_pe']:.1f}" if fund.get("sector_pe") else "-")
    valuation_col_3.metric("Market Cap", f"₹{fund['market_cap']:,.0f}Cr" if fund.get("market_cap") else "-")
    valuation_col_4.metric("D/E", f"{fund['debt_to_equity']:.2f}" if fund.get("debt_to_equity") else "-")
    valuation_col_5.metric("Div Yield", f"{fund['div_yield']:.2f}%" if fund.get("div_yield") else "-")

    st.subheader("Profitability")
    profitability_col_1, profitability_col_2 = st.columns(2)
    profitability_col_1.metric("ROCE", f"{fund['roce']:.1f}%" if fund.get("roce") else "-")
    profitability_col_2.metric("ROE", f"{fund['roe']:.1f}%" if fund.get("roe") else "-")

    # Shareholding
    st.subheader("Shareholding Pattern (QoQ)")
    if any(fund.get(key) for key in ("promoter_pct", "fii_pct", "dii_pct", "public_pct")):
        shareholding_col_1, shareholding_col_2, shareholding_col_3, shareholding_col_4 = st.columns(4)
        shareholding_col_1.metric("Promoter", fund.get("promoter_pct") or "-")
        shareholding_col_2.metric("FII", fund.get("fii_pct") or "-")
        shareholding_col_3.metric("DII", fund.get("dii_pct") or "-")
        shareholding_col_4.metric("Public", fund.get("public_pct") or "-")
    else:
        st.info("No shareholding breakdown available.")

    # Distance from 52 week
    if price and fund.get("wk52_high") and fund.get("wk52_low"):
        try:
            low, high = float(fund["wk52_low"]), float(fund["wk52_high"])
            distance_from_low = ((float(price) - low) / low) * 100
            distance_from_high = ((high - float(price)) / high) * 100
            st.caption(f"📍 +{distance_from_low:.1f}% from 52w Low · -{distance_from_high:.1f}% from 52w High")
        except (TypeError, ValueError, ZeroDivisionError):
            pass
