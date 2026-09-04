"""💹 Fundamental Analysis tab - quick card or deep report, one stock or watchlist.

Supports both markets, mirroring the Telegram commands:
  * India  - /fundamentalanalyze & /fundamentalreport (NSE/BSE, INR)
  * US     - /usstock (NASDAQ/NYSE, USD)
When a symbol isn't found, a suggestion picker (ticker + full name) is shown
so the user can pick the right one - same behaviour as the bot's /stock and
/usstock "did you mean" replies.
"""
from __future__ import annotations

try:
    import streamlit as st
except Exception:
    st = None

from ... import sources, storage
from ...formatting.stock_india import _fund_report_lines
from ...formatting.stock_us import _us_stock_lines
from ..helpers import fetch_fund_lines, tg_to_markdown
from ..widgets import render_quick_card, symbol_picker

_INDIA = "🇮🇳 India (NSE/BSE)"
_US = "🇺🇸 US (NASDAQ/NYSE)"


def _fetch_symbol(symbol: str, market: str) -> tuple[dict, dict]:
    """Live quote + fundamentals for a symbol on the chosen market."""
    if market == _US:
        return (
            sources.get_quote("US", symbol) or {},
            sources.get_us_fundamentals(symbol) or {},
        )
    return (
        sources.get_quote("NSE", symbol) or sources.get_quote("BSE", symbol) or {},
        sources.get_fundamentals(symbol, with_screener=True) or {},
    )


def _suggestions_for(symbol: str, market: str) -> list[dict]:
    """Close matches for a symbol that didn't resolve (ticker + name + exchange)."""
    if market == _US:
        matches = sources.search_us_tickers(symbol, limit=6)
        if matches:
            return matches
        return sources.search_market_data(symbol, filters={"market": "us", "limit": 6})
    matches = sources.search_stocks(symbol, limit=6)
    if matches:
        return matches
    return sources.search_market_data(symbol, filters={"market": "in", "limit": 6})


def _pick_row(match: dict) -> str:
    exchange = match.get("exchange") or ""
    name = match.get("name") or match.get("company") or ""
    tag = f" ({exchange})" if exchange else ""
    return f"{match.get('symbol', '')} — {name}{tag}"


def render() -> None:
    if st is None:
        raise RuntimeError("Streamlit UI removed; use the FastAPI templates instead.")
    st.header("💹 Fundamental Analysis")
    st.caption("Quick analysis card (like /fundamentalanalyze) or the DEEP report "
               "(like /fundamentalreport) — plus US stocks (like /usstock). "
               "Unknown symbols show a pick-list of similar tickers.")

    market = st.radio(
        "Market", [_INDIA, _US], horizontal=True, key="stock_market",
    )
    is_us = market == _US
    scope_options = ["Single symbol"] + ([] if is_us else ["My whole watchlist"])
    stock_scope = st.radio(
        "Scope", scope_options, horizontal=True, key="stock_scope",
    )
    if is_us and stock_scope == "My whole watchlist":
        stock_scope = "Single symbol"
    stock_deep = st.radio(
        "Report type",
        [
            "Quick card (like /fundamentalanalyze)",
            "Deep report (like /fundamentalreport)",
        ],
        horizontal=True, key="stock_rtype",
    )
    deep = stock_deep.startswith("Deep")

    placeholder = "AAPL, MSFT, BRK-B..." if is_us else "TATATECH, RELIANCE, INFY"
    symbol = symbol_picker(
        "us" if is_us else "in",
        f"Symbol (e.g. {placeholder})",
        "stock_sym",
        placeholder,
    )
    if st.button(
        "🔍 Analyze", width="stretch",
        disabled=(stock_scope == "Single symbol" and not symbol),
    ):
        if stock_scope == "My whole watchlist":
            watch = storage.load_watchlist()
            if not watch:
                st.error("Your watchlist is empty. Add stocks in the 📌 Watchlist tab first.")
            else:
                with st.spinner("Fetching analysis for your whole watchlist..."):
                    st.session_state["stock_mylist"] = fetch_fund_lines(watch, deep=deep)
                st.session_state.pop("stock_result", None)
                st.session_state.pop("stock_suggestions", None)
        else:
            with st.spinner(f"Fetching analysis for {symbol}..."):
                quote, fund = _fetch_symbol(symbol, market)
                if quote.get("price") is None and not fund:
                    st.session_state.pop("stock_result", None)
                    st.session_state["stock_suggestions"] = _suggestions_for(symbol, market)
                    st.session_state["stock_query"] = symbol
                    st.session_state["stock_query_market"] = market
                else:
                    st.session_state["stock_result"] = {
                        "quote": quote, "fund": fund, "sym": symbol,
                        "deep": deep, "us": is_us,
                    }
                    st.session_state.pop("stock_suggestions", None)

    # --- Suggestion picker (unknown symbol -> similar tickers) ---
    suggestions = st.session_state.get("stock_suggestions")
    if suggestions is not None:
        query = st.session_state.get("stock_query", "")
        market_label = "US" if st.session_state.get("stock_query_market") == _US else "NSE/BSE"
        if not suggestions:
            tip = ("Check the spelling, e.g. AAPL, BRK-B, BF.B." if is_us
                   else "Check the spelling or try a company name, e.g. RELIANCE.")
            st.error(f"No {market_label} data found for {query}. {tip}")
        else:
            st.warning(f"No {market_label} data found for {query}. Did you mean one of these?")
            pick = st.selectbox(
                "Similar tickers", suggestions, format_func=_pick_row, key="stock_pick",
            )
            if st.button("🔍 Analyze suggestion", width="stretch"):
                with st.spinner(f"Fetching analysis for {pick['symbol']}..."):
                    quote, fund = _fetch_symbol(pick["symbol"], market)
                    st.session_state["stock_result"] = {
                        "quote": quote, "fund": fund, "sym": pick["symbol"],
                        "deep": deep, "us": is_us,
                    }
                    st.session_state.pop("stock_suggestions", None)

    if st.session_state.get("stock_mylist"):
        items = st.session_state["stock_mylist"]
        if isinstance(items, str):
            st.warning(items)
        else:
            st.subheader(f"Whole watchlist — {'Deep report' if deep else 'Quick card'}")
            for symbol, markdown_text in items:
                with st.expander(symbol, expanded=False):
                    st.markdown(markdown_text, unsafe_allow_html=True)

    if st.session_state.get("stock_result"):
        result = st.session_state["stock_result"]
        quote, fund, symbol = result["quote"], result["fund"], result["sym"]
        if result.get("us"):
            st.markdown(tg_to_markdown("\n".join(
                _us_stock_lines(symbol, quote, fund, include_tip=False)
            )), unsafe_allow_html=True)
        elif result.get("deep"):
            st.markdown(tg_to_markdown("\n".join(
                _fund_report_lines(symbol, quote, fund, include_tip=False)
            )), unsafe_allow_html=True)
        else:
            render_quick_card(quote, fund, symbol)
