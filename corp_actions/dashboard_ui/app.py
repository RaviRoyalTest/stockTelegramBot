"""Dashboard app assembly: page config, sidebar and the seven tabs.

The heavy lifting lives in helpers.py (pure data functions), widgets.py
(shared st widgets) and tabs/ (one renderer per tab). This module only
composes them - run `streamlit run dashboard.py` (a thin wrapper).
"""
from __future__ import annotations

import streamlit as st

from .. import config
from ..telegram.client import NotifierError, is_configured, send_message
from ..poller import poller
from .tabs import actions, market, news, settings, status, stock, watchlist


def _page_config() -> None:
    st.set_page_config(
        page_title="Stock Alert Bot — Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Responsive layout: on phones/tablets stack every column block vertically
    # so metric grids and multi-column rows stay readable instead of squeezing
    # into unreadable slivers. Tablets get a slightly larger breakpoint.
    st.markdown(
        """
        <style>
        @media (max-width: 1024px) {
          /* tablets: two-up instead of cramming 4-5 metrics in a row */
          [data-testid="stHorizontalBlock"] > div {
            flex: 1 1 50% !important;
            min-width: 45% !important;
          }
        }
        @media (max-width: 720px) {
          /* phones: everything stacks to full width */
          [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
          [data-testid="stHorizontalBlock"] > div {
            flex: 1 1 100% !important;
            min-width: 100% !important;
          }
          [data-testid="stMetric"] { padding: 0.25rem 0.5rem !important; }
          .stMarkdown { font-size: 0.95rem !important; }
          [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        }
        @media (max-width: 420px) {
          [data-testid="stMetricValue"] { font-size: 0.95rem !important; }
          .stMarkdown { font-size: 0.9rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar() -> None:
    st.title("📈 Stock Alert Bot")
    st.caption("NSE & BSE corporate actions, movers, news → Telegram")

    st.divider()
    st.subheader("🔌 Telegram Connection")
    if is_configured():
        st.success(f"Configured · chat_id `{config.TELEGRAM_CHAT_ID}`")
        if st.button("📨 Send test message", width="stretch"):
            try:
                send_message("<b>Stock Alert Bot</b> test message OK.")
                st.success("Test message sent.")
            except NotifierError as exc:
                st.error(config.redact(str(exc)))
    else:
        st.warning("Telegram not configured.")

    st.divider()
    st.subheader("⚙️ Poller")
    status = poller.status
    col1, col2 = st.columns(2)
    with col1:
        if st.button("▶️ Start", disabled=status["running"], width="stretch"):
            poller.start()
            st.rerun()
    with col2:
        if st.button("⏹ Stop", disabled=not status["running"], width="stretch"):
            poller.stop()
            st.rerun()
    if st.button("🔍 Check now", width="stretch"):
        try:
            poller.run_once()
            st.success("Checked.")
        except Exception as exc:
            st.error(config.redact(str(exc)))
        st.rerun()

    st.divider()
    st.subheader("🌐 Sources")
    enable_bse = st.toggle(
        "Use BSE (usually WAF-blocked on datacenter IPs)",
        value=config.ENABLE_BSE,
    )
    if enable_bse != config.ENABLE_BSE:
        config.ENABLE_BSE = enable_bse
        st.rerun()

    st.divider()
    st.caption(f"Poll interval: {config.POLL_INTERVAL_SECONDS}s · "
               f"Reminder: {config.REMINDER_DAYS}d")


def main() -> None:
    """Render the full dashboard (page config, sidebar and tabs)."""
    _page_config()
    with st.sidebar:
        render_sidebar()

    tab_watch, tab_actions, tab_market, tab_stock, tab_news, tab_settings, tab_status = st.tabs(
        [
            "📌 Watchlist",
            "📋 Corporate Actions",
            "📊 Market Screens",
            "💹 Fundamental Analysis",
            "📰 News",
            "🎛️ Alert Settings",
            "🖥️ System",
        ]
    )

    with tab_watch:
        watchlist.render()
    with tab_actions:
        actions.render()
    with tab_market:
        market.render()
    with tab_stock:
        stock.render()
    with tab_news:
        news.render()
    with tab_settings:
        settings.render()
    with tab_status:
        status.render()
