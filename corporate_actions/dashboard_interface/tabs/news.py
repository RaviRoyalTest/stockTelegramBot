"""📰 News tab - latest headlines for watchlist stocks or a single symbol."""
from __future__ import annotations

try:
    import streamlit as st
except Exception:
    st = None

from ... import sources, storage
from ...core.dates import format_timestamp
from ..helpers import md_escape
from ..widgets import symbol_picker


def render() -> None:
    if st is None:
        raise RuntimeError("Streamlit UI removed; use the FastAPI templates instead.")
    st.header("📰 News")
    st.caption("Latest headlines for watchlist stocks or a single symbol.")

    column_1, column_2 = st.columns([2, 1])
    with column_1:
        news_target = symbol_picker(
            "in",
            "Symbol (leave empty for whole watchlist)",
            "news_target",
            "Type to search NSE/BSE...",
        )
    with column_2:
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
        for result in st.session_state["news_results"]:
            st.subheader(f"{result['symbol']} ({result['exchange']})")
            if not result["news"]:
                st.info("No recent news found.")
            for index, news_item in enumerate(result["news"], 1):
                title = news_item.get("title") or "-"
                link = news_item.get("link") or ""
                published_at = news_item.get("published_ts")
                publisher = news_item.get("publisher") or ""
                meta = " | ".join(part for part in (publisher, format_timestamp(published_at)) if part)
                if link:
                    st.markdown(f"{index}. [{md_escape(title)}](<{md_escape(link)}>)")
                else:
                    st.markdown(f"{index}. {md_escape(title)}")
                if meta:
                    st.caption(meta)
            st.divider()
