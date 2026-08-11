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
    k = 2.0 / (span + 1.0)
    ema = closes[-span]
    for c in closes[-span:]:
        ema = c * k + ema * (1 - k)
    return price > ema


def handle_scan500(chat_id, parts) -> None:
    """NIFTY 500 advanced multi-indicator CNC/MIS scanner (/scan500).

    Runs the full indicator suite (EMAs, RSI, MACD, ADX, CMF, MFI, OBV,
    Aroon, TTM squeeze, Donchian, weekly Supertrend, GMMA, anchored VWAP,
    Mansfield RS) over the NIFTY 500 universe, applies the strict rejection
    rules, scores survivors out of 100 and reports regime + top picks.
    """
    t0 = monotonic()
    log.info("scan500: starting (chat %s)", chat_id)
    reply(
        chat_id,
        "Scanning NIFTY 500 (daily candles, ~500 stocks)... "
        "this can take a minute or two.",
    )
    try:
        import corp_actions.scanner as sc
    except Exception as exc:
        log.warning("scan500: scanner module unavailable: %s", exc)
        reply(chat_id, "Scanner engine unavailable (pandas missing?).")
        return

    from ..sources import get_index_universe

    symbols = get_index_universe("nifty500")
    if not symbols:
        log.warning("scan500: no symbols loaded")
        reply(chat_id, "Could not load the NIFTY 500 universe right now. Try again in a minute.")
        return

    # Market regime inputs
    idx50 = get_index_ohlc("^NSEI", "2y", "1d")
    vix = get_index_ohlc("^INDIAVIX", "6mo", "1d")
    idx500 = get_index_ohlc("^CRSLDX", "2y", "1d") or idx50
    bench_close = (idx500 or {}).get("close")

    def _fetch(sym):
        try:
            return sym, get_ohlc("NSE", sym, "1d")
        except Exception:
            return sym, None

    ohlc_by_sym = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch, sym): sym for sym in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                ohlc_by_sym[sym] = fut.result()[1]
            except Exception:
                ohlc_by_sym[sym] = None
    log.info("scan500: fetched %d/%d OHLC sets in %.1fs",
             sum(1 for v in ohlc_by_sym.values() if v), len(symbols),
             monotonic() - t0)

    rows = []
    rejected = []
    for sym, ohlc in ohlc_by_sym.items():
        try:
            f = sc.scan_stock(ohlc, index_close=bench_close)
            if f is None:
                continue
            f = sc.build_plan(f)
            score, breakdown = sc.score_stock(f)
            reasons = sc.rejection_reasons(f)
            f["score"] = score
            if reasons:
                rejected.append((sym, f.get("name") or sym, f["price"], reasons))
            else:
                rows.append({"fields": f, "score": score, "breakdown": breakdown})
        except Exception as exc:
            log.info("scan500: skip %s (%s)", sym, exc)
            continue

    # Breadth across the scanned universe
    above_50 = sum(1 for _, o in ohlc_by_sym.items() if _above_ema(o, 50))
    above_200 = sum(1 for _, o in ohlc_by_sym.items() if _above_ema(o, 200))
    adv = sum(1 for s, o in ohlc_by_sym.items() if o and o["close"] and o["close"][-1] > o["open"][-1])
    dec = sum(1 for s, o in ohlc_by_sym.items() if o and o["close"] and o["close"][-1] < o["open"][-1])
    total = sum(1 for o in ohlc_by_sym.values() if o)
    vix_val = (vix or {}).get("close")
    vix_last = vix_val[-1] if vix_val else None
    breadth = {
        "above_ema50": (above_50 / total * 100.0) if total else None,
        "above_ema200": (above_200 / total * 100.0) if total else None,
        "advance": float(adv), "decline": float(dec),
        "vix": vix_last,
    }
    regime = sc.market_regime(idx50, breadth)

    # Approve by score threshold
    rows.sort(key=lambda r: r["score"], reverse=True)
    approved = [r for r in rows if r["score"] >= sc.SCORE_QUALIFY]

    lines = sc.format_report({
        "regime": regime,
        "rejected": rejected,
        "approved": approved,
        "scanned": total or len(symbols),
    })
    reply_messages(chat_id, split_messages(lines))
    log.info("scan500: done in %.1fs (%d approved, %d rejected, %d scanned)",
             monotonic() - t0, len(approved), len(rejected), total or len(symbols))
