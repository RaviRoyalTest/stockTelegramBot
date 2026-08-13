"""NIFTY 500 advanced multi-indicator CNC/MIS scanner (/scan500)."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic

from ..core.text import split_messages
from ..sources import get_index_ohlc, get_ohlc
from .reply import reply, reply_messages

log = logging.getLogger(__name__)


def _above_ema(ohlc, span: int) -> bool:
    """Quick price-vs-EMA check for breadth (no pandas dependency here)."""
    if not ohlc or len(ohlc["close"]) < span + 5:
        return False
    closes = ohlc["close"]
    price = closes[-1]
    smoothing_factor = 2.0 / (span + 1.0)
    ema = closes[-span]
    for close in closes[-span:]:
        ema = close * smoothing_factor + ema * (1 - smoothing_factor)
    return price > ema


def handle_scan500(chat_id, parts) -> None:
    """NIFTY 500 advanced multi-indicator CNC/MIS scanner (/scan500).

    Runs the full indicator suite (EMAs, SMA golden cross, RSI, MACD,
    Stochastic, Bollinger, CCI, ADX, Aroon, Parabolic SAR, CMF, MFI, OBV,
    TTM squeeze, Donchian, weekly Supertrend, GMMA, anchored VWAP,
    Mansfield RS) over the NIFTY 500 universe, applies the strict rejection
    rules, scores survivors out of 100 and reports regime + top picks,
    ending with a full indicator card for each of the TOP 10.
    """
    started_at = monotonic()
    log.info("scan500: starting (chat %s)", chat_id)
    reply(
        chat_id,
        "Scanning NIFTY 500 (daily candles, ~500 stocks)... "
        "this can take a minute or two.",
    )
    try:
        import corporate_actions.scanner as scanner
    except Exception as error:
        log.warning("scan500: scanner module unavailable: %s", error)
        reply(chat_id, "Scanner engine unavailable (pandas missing?).")
        return

    from ..sources import get_index_universe

    symbols = get_index_universe("nifty500")
    if not symbols:
        log.warning("scan500: no symbols loaded")
        reply(chat_id, "Could not load the NIFTY 500 universe right now. Try again in a minute.")
        return

    # Market regime inputs
    index_50 = get_index_ohlc("^NSEI", "2y", "1d")
    vix = get_index_ohlc("^INDIAVIX", "6mo", "1d")
    index_500 = get_index_ohlc("^CRSLDX", "2y", "1d") or index_50
    benchmark_close = (index_500 or {}).get("close")

    def _fetch(symbol):
        try:
            return symbol, get_ohlc("NSE", symbol, "1d")
        except Exception:
            return symbol, None

    ohlc_by_symbol = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(_fetch, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                ohlc_by_symbol[symbol] = future.result()[1]
            except Exception:
                ohlc_by_symbol[symbol] = None
    log.info("scan500: fetched %d/%d OHLC sets in %.1fs",
             sum(1 for value in ohlc_by_symbol.values() if value), len(symbols),
             monotonic() - started_at)

    rows = []
    rejected = []
    for symbol, ohlc in ohlc_by_symbol.items():
        try:
            finding = scanner.scan_stock(ohlc, index_close=benchmark_close)
            if finding is None:
                continue
            finding = scanner.build_plan(finding)
            score, breakdown = scanner.score_stock(finding)
            reasons = scanner.rejection_reasons(finding)
            finding["score"] = score
            if reasons:
                rejected.append((symbol, finding.get("name") or symbol, finding["price"], reasons))
            else:
                rows.append({"fields": finding, "score": score, "breakdown": breakdown})
        except Exception as error:
            log.info("scan500: skip %s (%s)", symbol, error)
            continue

    # Breadth across the scanned universe
    above_50 = sum(1 for _, ohlc_data in ohlc_by_symbol.items() if _above_ema(ohlc_data, 50))
    above_200 = sum(1 for _, ohlc_data in ohlc_by_symbol.items() if _above_ema(ohlc_data, 200))
    advance_count = sum(1 for symbol, ohlc_data in ohlc_by_symbol.items() if ohlc_data and ohlc_data["close"] and ohlc_data["close"][-1] > ohlc_data["open"][-1])
    decline_count = sum(1 for symbol, ohlc_data in ohlc_by_symbol.items() if ohlc_data and ohlc_data["close"] and ohlc_data["close"][-1] < ohlc_data["open"][-1])
    total = sum(1 for ohlc_data in ohlc_by_symbol.values() if ohlc_data)
    vix_value = (vix or {}).get("close")
    vix_last_close = vix_value[-1] if vix_value else None
    breadth = {
        "above_ema50": (above_50 / total * 100.0) if total else None,
        "above_ema_200": (above_200 / total * 100.0) if total else None,
        "advance": float(advance_count), "decline": float(decline_count),
        "vix": vix_last_close,
    }
    regime = scanner.market_regime(index_50, breadth)

    # Approve by score threshold
    rows.sort(key=lambda row: row["score"], reverse=True)
    approved = [row for row in rows if row["score"] >= scanner.SCORE_QUALIFY]

    lines = scanner.format_report({
        "regime": regime,
        "rejected": rejected,
        "approved": approved,
        "scanned": total or len(symbols),
    })
    reply_messages(chat_id, split_messages(lines))
    log.info("scan500: done in %.1fs (%d approved, %d rejected, %d scanned)",
             monotonic() - started_at, len(approved), len(rejected), total or len(symbols))
