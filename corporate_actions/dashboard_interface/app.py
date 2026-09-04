"""Dashboard app assembly: page config, sidebar and the seven tabs.

The heavy lifting lives in helpers.py (pure data functions), widgets.py
(shared st widgets) and tabs/ (one renderer per tab). This module only
composes them - run `streamlit run dashboard.py` (a thin wrapper).
"""
from __future__ import annotations

try:
    import streamlit as st
except Exception:
    st = None

from .. import config, storage
from ..telegram.client import NotifierError, is_configured, send_message
from ..poller import poller
from .tabs import actions, market, news, screener, settings, status, stock, watchlist


def _page_config() -> None:
    st.set_page_config(
        page_title="Royal Stock — Dashboard",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        :root {
            --bg: #0b1120;
            --panel: #111827;
            --panel-2: #0f172a;
            --line: rgba(148,163,184,0.18);
            --text: #e5eefb;
            --muted: #94a3b8;
            --green: #22c55e;
            --red: #ef4444;
            --amber: #f59e0b;
            --blue: #60a5fa;
        }
        div[data-testid="stHeader"] {
            display: none !important;
        }
        div[data-testid="stToolbar"] {
            display: none !important;
        }
        div[data-testid="stDecoration"] {
            display: none !important;
        }
        button[kind="header"] {
            display: none !important;
        }
        .stApp {
            background: radial-gradient(circle at top left, rgba(59,130,246,0.18), transparent 28%),
                        linear-gradient(180deg, #020817 0%, #0b1120 100%);
            color: var(--text);
        }
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 2rem;
        }
        [data-testid="stSidebar"] {
            background: rgba(15, 23, 42, 0.96);
            border-right: 1px solid var(--line);
        }
        [data-testid="stMetric"] {
            background: rgba(15,23,42,0.75);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 0.8rem 0.9rem;
        }
        .dashboard-shell {
            background: linear-gradient(135deg, rgba(15,23,42,0.98), rgba(17,24,39,0.9));
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 1rem 1.1rem;
            margin-bottom: 1rem;
            box-shadow: 0 12px 40px rgba(15, 23, 42, 0.25);
        }
        .hero-card {
            background: linear-gradient(135deg, rgba(59,130,246,0.18), rgba(15,23,42,0.8));
            border: 1px solid rgba(96,165,250,0.25);
            border-radius: 16px;
            padding: 1rem 1.1rem;
            margin-bottom: 1rem;
        }
        .hero-tag {
            display: inline-block;
            background: rgba(96,165,250,0.1);
            border: 1px solid rgba(96,165,250,0.2);
            border-radius: 999px;
            padding: 0.28rem 0.7rem;
            color: #dbeafe;
            font-size: 0.76rem;
            margin-right: 0.35rem;
            margin-top: 0.35rem;
        }
        @media (max-width: 1024px) {
          [data-testid="stHorizontalBlock"] > div {
            flex: 1 1 50% !important;
            min-width: 45% !important;
          }
        }
        @media (max-width: 720px) {
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
    st.markdown(
        """
        <div class="hero-card">
            <div style="font-size:1.6rem; font-weight:700;">📈 Royal Stock</div>
            <div style="color:#cbd5e1; margin-top:0.3rem;">NSE & BSE market intelligence</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.subheader("🔌 Telegram Connection")
    if is_configured():
        st.success(f"Configured · chat_id `{config.TELEGRAM_CHAT_ID}`")
        if st.button("📨 Send test message", width="stretch"):
            try:
                send_message("<b>Royal Stock</b> test message OK.")
                st.success("Test message sent.")
            except NotifierError as error:
                st.error(config.redact(str(error)))
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
        except Exception as error:
            st.error(config.redact(str(error)))
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
    st.caption(f"Poll interval: {config.POLL_INTERVAL_SECONDS}s · Reminder: {config.REMINDER_DAYS}d")


def render_overview() -> None:
    status = poller.status
    watchlist = storage.load_watchlist()
    metric_cols = st.columns(4)
    with metric_cols[0]:
        st.metric("Watchlist", len(watchlist))
    with metric_cols[1]:
        st.metric("Poller", "Running" if status["running"] else "Idle")
    with metric_cols[2]:
        st.metric("Alerts sent", status["total_sent"])
    with metric_cols[3]:
        st.metric("Market gate", "Open" if status.get("running") else "Offline")

    overview_cols = st.columns([2, 1])
    with overview_cols[0]:
        st.markdown(
            """
            <div class="dashboard-shell">
                <div style="font-weight:700; font-size:1.2rem; margin-bottom:0.5rem;">Overview</div>
                <div style="color:#cbd5e1;">Track price action, new actions, watchlist quality, and scanner opportunities in one view.</div>
                <div style="margin-top:0.85rem;">
                    <span class="hero-tag">Market pulse</span>
                    <span class="hero-tag">Watchlist</span>
                    <span class="hero-tag">Fundamentals</span>
                    <span class="hero-tag">Trade ideas</span>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with overview_cols[1]:
        st.markdown(
            """
            <div class="dashboard-shell">
                <div style="font-weight:700; margin-bottom:0.3rem;">Quick actions</div>
                <div style="color:#cbd5e1;">• Screener filters</div>
                <div style="color:#cbd5e1;">• Corporate actions</div>
                <div style="color:#cbd5e1;">• Watchlist sync</div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def main() -> None:
    """Render the full dashboard (page config, sidebar and tabs)."""
    _page_config()
    with st.sidebar:
        render_sidebar()

    render_overview()

    tab_watch, tab_actions, tab_market, tab_stock, tab_screener, tab_news, tab_settings, tab_status = st.tabs(
        [
            "📌 Watchlist",
            "📋 Corporate Actions",
            "📊 Market Screens",
            "💹 Fundamental Analysis",
            "🔎 Screener",
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
    with tab_screener:
        screener.render()
    with tab_news:
        news.render()
    with tab_settings:
        settings.render()
    with tab_status:
        status.render()
