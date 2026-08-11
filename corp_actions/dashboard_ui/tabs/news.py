"""📰 News tab - latest headlines for watchlist stocks or a single symbol."""
from __future__ import annotations

import streamlit as st

from ... import sources, storage
from ...core.dates import fmt_ts
from ..helpers import md_escape


def render() -> None:
    st.header("📰 News")
    st.caption("Latest headlines for watchlist stocks or a single symbol.")

    c1, c2 = st.columns([2, 1])
    with c1:
        news_target = st.text_input("Symbol (leave empty for whole watchlist)", key="news_target").strip().upper()
    with c2:
        news_count = st.slider("Headlines per stock", 1, 5, 3, key="news_count")

    if st.button("📰 Fetch news", width="stretch"):
        with st.spinner("Fetching news..."):
            if news_target:
                items = [{"symbol": news_target, "exchange": "NSE"}]
            else:
                items = storage.load_watchlist()
                if not items:
                    st.warning("Watchlist is empty — enter a symbol above.")
                    items = []
            results = []
            for item in items[:10]:
                news = sources.get_stock_news(item["exchange"], item["symbol"], news_count)
                results.append({"symbol": item["symbol"], "exchange": item["exchange"], "news": news})
            st.session_state["news_results"] = results

    if st.session_state.get("news_results"):
        for res in st.session_state["news_results"]:
            st.subheader(f"{res['symbol']} ({res['exchange']})")
            if not res["news"]:
                st.info("No recent news found.")
            for i, n in enumerate(res["news"], 1):
                title = n.get("title") or "-"
                link = n.get("link") or ""
                pub = n.get("published_ts")
                publisher = n.get("publisher") or ""
                meta = " | ".join(p for p in (publisher, fmt_ts(pub)) if p)
                if link:
                    st.markdown(f"{i}. [{md_escape(title)}](<{md_escape(link)}>)")
                else:
                    st.markdown(f"{i}. {md_escape(title)}")
                if meta:
                    st.caption(meta)
            st.divider()
