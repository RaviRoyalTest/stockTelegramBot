"""Comprehensive web dashboard for the Stock Alert Bot.

Surfaces ALL bot functionality in a well-organized, customisable UI:
  * Watchlist management (owner + subscriber lists)
  * Live prices, market movers / gainers / losers screens
  * Corporate action queries (by type, ex-date, symbol, keyword)
  * Single-stock deep analysis
  * News
  * Alert settings (action-type filters + price threshold)
  * System status & persistence

All rendering logic lives in corp_actions.dashboard_ui (helpers, widgets,
per-tab renderers); this file is a thin wrapper that Streamlit runs.

Run locally:       streamlit run dashboard.py
Run on Render:     streamlit run dashboard.py --server.port $PORT
"""
import logging
import sys

from corp_actions.dashboard_ui.app import main

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

main()
