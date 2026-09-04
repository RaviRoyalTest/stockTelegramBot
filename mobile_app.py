"""Mobile-first PWA launcher for the stock bot dashboard.

This file is intentionally tiny: it points to the existing Streamlit dashboard
and adds lightweight metadata for easier installability on mobile browsers.

Run locally:
    streamlit run mobile_app.py

This is a practical mobile-friendly wrapper for the existing app without
building a separate native app.
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Stock Alert Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#0f172a" />
    <meta name="apple-mobile-web-app-capable" content="yes" />
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
    <link rel="manifest" href="/static/manifest.json" />
    """,
    unsafe_allow_html=True,
)

st.title("📈 Stock Alert Bot")
st.caption("Mobile-friendly dashboard for watchlists, movers, fundamentals and alerts.")

st.markdown(
    """
    <style>
    @media (max-width: 720px) {
        .stApp {
            padding-left: 0.5rem !important;
            padding-right: 0.5rem !important;
        }
        [data-testid="stMetricValue"] { font-size: 1.1rem !important; }
        [data-testid="stHorizontalBlock"] > div { min-width: 100% !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.info("Open the full dashboard in the browser, or run the same project via the standard app entry points.")

if st.button("Launch dashboard", type="primary", use_container_width=True):
    st.switch_page("dashboard.py")
