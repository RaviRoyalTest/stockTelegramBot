"""Custom web app entry for the Stock Alert Bot.

This is a minimal non-Streamlit launcher; the actual UI is served by FastAPI.
"""
from __future__ import annotations

import os

import uvicorn


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("dashboard:app", host="0.0.0.0", port=port, reload=False)
