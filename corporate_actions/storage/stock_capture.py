"""Timestamped persistence of single-stock + batch stock detail.

The screen families (movers, gappers, screen, scan500, harmonic) have always
called ``capture_command_output`` themselves. The STOCK DETAIL paths - the
quick card (/stock), the deep report (/fundamentalreport), the watchlist
batches and the US report - rendered their data and threw it away: nothing on
disk recorded what a stock looked like at the moment it was answered.

This module is the single place that fixes that, so every stock path writes
the SAME envelope and no call site can drift:

    {
      "symbol": "RELIANCE",
      "market": "in" | "us",
      "deep": bool,               # deep report vs quick card
      "quote": {...},             # live quote as fetched
      "fundamentals": {...},      # normalised fundamentals as fetched
    }

Batches store one such record per watchlist position under a "batch" key.

The capture timestamp is NOT set here - ``save_json_snapshot`` stamps
``metadata.captured_at`` (IST, microsecond precision) and ``metadata.timezone``
into the envelope, and the filename carries the same IST timestamp. Keeping the
timestamp owned by a single writer is what guarantees the file body and the
file name can never disagree.

Note on the plumbing: ``capture_command_output(data_name, command, data, ...)``
builds its own metadata envelope from ``{"command": ...}`` plus any keyword
context, so the stock fields are passed as FLAT context - not as a nested
``metadata=`` dict, which would be swallowed one level too deep.

Never raises: a capture failure is logged by ``capture_command_output`` and the
command still answers the user.
"""
from __future__ import annotations

import logging
from typing import Any

from .realtime import capture_command_output

__all__ = ["capture_stock_detail", "capture_stock_batch"]

log = logging.getLogger(__name__)

# Where the files land: data/realtime/cmd_stock_detail/{Y}/{M}/{D}/... and
# data/realtime/cmd_stock_batch/{Y}/{M}/{D}/...
_STOCK_DATA_NAME = "stock_detail"
_BATCH_DATA_NAME = "stock_batch"


def _market_of(is_us: bool) -> str:
    return "us" if is_us else "in"


def _record(symbol: str, quote, fund, *, is_us: bool, deep: bool) -> dict:
    """One symbol's stored shape. Empty sections are dropped, not blanked."""
    record = {
        "symbol": (symbol or "").strip().upper(),
        "market": _market_of(is_us),
        "deep": bool(deep),
    }
    if quote:
        record["quote"] = quote
    if fund:
        record["fundamentals"] = fund
    return record


def capture_stock_detail(
    command: str,
    symbol: str,
    quote: dict | None,
    fund: dict | None,
    *,
    is_us: bool = False,
    deep: bool = False,
    source: str = "telegram",
    prefix: str = "cmd_",
    **context: Any,
):
    """Persist one symbol's quote + fundamentals, timestamped.

    ``command`` is the raw command text (e.g. "/stock RELIANCE") so the stored
    record says exactly which invocation produced it. Returns the written path,
    or None when the capture was skipped.
    """
    record = _record(symbol, quote, fund, is_us=is_us, deep=deep)
    # Flat context: capture_command_output merges this into its metadata
    # envelope alongside "command" and its own captured_at/timezone stamps.
    context.update({
        "symbol": record["symbol"],
        "market": record["market"],
        "deep": record["deep"],
    })
    return capture_command_output(
        _STOCK_DATA_NAME,
        command,
        record,
        source=source,
        prefix=prefix,
        **context,
    )


def capture_stock_batch(
    command: str,
    rows: list[dict],
    *,
    deep: bool = False,
    start: int | None = None,
    end: int | None = None,
    total: int | None = None,
    source: str = "telegram",
    prefix: str = "cmd_",
    **context: Any,
):
    """Persist a page of watchlist positions, timestamped.

    ``rows`` is one record per position, each already shaped by :func:`_record`
    (symbol / market / quote / fundamentals).
    """
    body = {
        "batch": list(rows or []),
        "count": len(rows or []),
        "deep": bool(deep),
    }
    if start is not None:
        context["start"] = start
    if end is not None:
        context["end"] = end
    if total is not None:
        context["total"] = total
    context["deep"] = bool(deep)
    return capture_command_output(
        _BATCH_DATA_NAME,
        command,
        body,
        source=source,
        prefix=prefix,
        **context,
    )
