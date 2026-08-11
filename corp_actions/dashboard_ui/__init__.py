"""Web-dashboard package: pure helpers, shared widgets, per-tab renderers, app.

Layout (see AGENTS.md):
  helpers.py   pure data functions - no Streamlit (unit-testable)
  widgets.py   shared Streamlit render widgets (analysis buttons, cards)
  help_text.py the command guide in markdown
  tabs/        one module per dashboard tab, each exposing render()
  app.py       page config + sidebar + tab assembly (main)
"""
from .app import main

__all__ = ["main"]
