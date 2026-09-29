"""Session-snapshot command: /snap records the last session to disk.

Records the overnight gap-down scan, the session top gainers/losers and
the corporate-action list into data/snapshots.json - the same file the
web Sessions tab and the daily mail read, so they never re-fetch.
"""
from __future__ import annotations

import logging
from time import monotonic

from ..snapshots import record_session_snapshot
from .reply import reply

log = logging.getLogger(__name__)

SNAP_USAGE = (
    "<b>/snap</b> - record the last session's screens to disk\n"
    "<code>/snap</code>       \u2192 India: NIFTY 100 + 500 ex-100 + Microcap blocks (60 rows)\n"
    "<code>/snap us</code>    \u2192 US: Mega-cap + Large-cap blocks\n"
    "The web <b>Sessions</b> tab and the daily mail read this file, so "
    "recording once here saves every later view a full re-fetch."
)


def handle_snap(chat_id, parts) -> None:
    """Record the last session snapshot (skips when already recorded)."""
    text = " ".join(parts).lower()
    market = "us" if any(token in text for token in (" us", "usa", "nasdaq", "sp500", "s&p")) else "in"
    started_at = monotonic()
    reply(
        chat_id,
        f"\U0001F4F8 Recording the last session ({market.upper()}) - "
        "gap-downs, movers and actions. This takes a few minutes; "
        "I'll report back here when the file is written.",
    )
    try:
        doc = record_session_snapshot(market=market, force="force" in text,
                                      recorded_by="telegram")
    except Exception as error:
        log.warning("snap failed: %s", error)
        reply(chat_id, f"Snapshot failed: {error}. Try again in a minute.")
        return
    elapsed = monotonic() - started_at
    blocks = doc.get("universes") or []
    block_rows = sum(len(block.get("gainers", [])) + len(block.get("losers", [])) for block in blocks)
    reply(
        chat_id,
        "\U0001F4BE <b>Snapshot recorded</b> \u00b7 market "
        f"<b>{doc.get('market', market).upper()}</b> \u00b7 session "
        f"<b>{doc.get('session', '?')}</b> in {elapsed:.0f}s\n"
        f"Universe blocks: <b>{len(blocks)}</b> ({block_rows} rows) \u00b7 "
        f"Gap-downs: <b>{len(doc.get('gap_downs', []))}</b> \u00b7 "
        f"actions: <b>{len(doc.get('corporate_actions', []))}</b>\n"
        "See it on the web <b>Sessions</b> tab - no re-fetch needed.",
    )
