"""💹 Fundamental Analysis tab - quick card or deep report, one stock or watchlist."""
from __future__ import annotations

import streamlit as st

from ... import sources, storage
from ...formatting.stock import _fund_report_lines
from ..helpers import fetch_fund_lines, tg_to_markdown
from ..widgets import render_quick_card


def render() -> None:
    st.header("💹 Fundamental Analysis")
    st.caption("Quick analysis card (like /fundamentalanalyze) or the DEEP report "
               "(like /fundamentalreport) — for one stock or your whole watchlist.")

    stock_scope = st.radio("Scope", ["Single symbol", "My whole watchlist"],
                           horizontal=True, key="stock_scope")
    stock_deep = st.radio(
        "Report type",
        ["Quick card (like /fundamentalanalyze)", "Deep report (like /fundamentalreport)"],
        horizontal=True, key="stock_rtype",
    )
    deep = stock_deep.startswith("Deep")

    sym = st.text_input("Symbol (e.g. TATATECH, RELIANCE, INFY)", key="stock_sym").strip().upper()
    if st.button("🔍 Analyze", width="stretch",
                 disabled=(stock_scope == "Single symbol" and not sym)):
        if stock_scope == "My whole watchlist":
            watch = storage.load_watchlist()
            if not watch:
                st.error("Your watchlist is empty. Add stocks in the 📌 Watchlist tab first.")
            else:
                with st.spinner("Fetching analysis for your whole watchlist..."):
                    st.session_state["stock_mylist"] = fetch_fund_lines(watch, deep=deep)
        else:
            with st.spinner(f"Fetching analysis for {sym}..."):
                quote = sources.get_quote("NSE", sym) or sources.get_quote("BSE", sym) or {}
                fund = sources.get_fundamentals(sym, with_screener=True) or {}
                if not quote and not fund:
                    st.error(f"No data found for {sym}. Check the symbol.")
                else:
                    st.session_state["stock_result"] = {"quote": quote, "fund": fund, "sym": sym, "deep": deep}

    if st.session_state.get("stock_mylist"):
        items = st.session_state["stock_mylist"]
        if isinstance(items, str):
            st.warning(items)
        else:
            st.subheader(f"Whole watchlist — {'Deep report' if deep else 'Quick card'}")
            for sym, md in items:
                with st.expander(sym, expanded=False):
                    st.markdown(md, unsafe_allow_html=True)

    if st.session_state.get("stock_result"):
        res = st.session_state["stock_result"]
        quote, fund, sym = res["quote"], res["fund"], res["sym"]
        if res.get("deep"):
            st.markdown(tg_to_markdown("\n".join(
                _fund_report_lines(sym, quote, fund, include_tip=False)
            )), unsafe_allow_html=True)
        else:
            render_quick_card(quote, fund, sym)
