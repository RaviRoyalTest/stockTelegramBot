"""Per-indicator deep-dive command: /indicator SYMBOL [INDICATOR].

Works for Indian (NSE/BSE) and US (NASDAQ/NYSE) stocks - the market is
auto-detected from the Yahoo candles. With an indicator name it returns a
clear deep-dive for that one indicator (current value, signal, 5-session
trend, plain-language meaning and a reading-levels legend); without one it
returns the full all-indicators card (the /scan500 detail format). Unknown
symbols get the usual ticker + full-name suggestion pick-list.
"""
from __future__ import annotations

import logging

from ..core.text import escape, split_messages
from ..sources import get_ohlc
from .helpers import reply_suggestions
from .reply import reply, reply_messages

log = logging.getLogger(__name__)

_USAGE = (
    "Usage: <code>/indicator SYMBOL [INDICATOR]</code>\n"
    "  <code>/indicator RELIANCE RSI</code>  \u2192 deep-dive for one indicator\n"
    "  <code>/indicator AAPL macd</code>     \u2192 US tickers work too (auto-detected)\n"
    "  <code>/indicator RELIANCE</code>      \u2192 the FULL all-indicators card\n"
    "Indicators: rsi, macd, stochastic, bollinger, cci, adx, aroon, psar,\n"
    "supertrend, ema/sma, gmma, vwap, atr, donchian, squeeze, cmf, mfi, obv\n"
    "Aliases: <code>/ind</code>, <code>/tech</code>, <code>/technical</code>"
)


def handle_indicator(chat_id, parts) -> None:
    """One-indicator deep-dive or the full indicator card for a symbol."""
    if len(parts) < 2:
        reply(chat_id, _USAGE)
        return

    raw_symbol = parts[1].upper().strip()
    indicator_arg = " ".join(parts[2:]).strip() if len(parts) > 2 else ""

    key = None
    if indicator_arg:
        from ..scanner.indicator_report import available_indicator_names, match_indicator
        key = match_indicator(indicator_arg)
        if key is None:
            reply(
                chat_id,
                f"Unknown indicator <code>{escape(indicator_arg)}</code>.\n"
                f"Try one of: <code>{available_indicator_names()}</code>",
            )
            return

    # Fetch daily candles, auto-detecting the market: NSE \u2192 BSE \u2192 US.
    ohlc = None
    exchange = None
    for candidate in ("NSE", "BSE", "US"):
        try:
            data = get_ohlc(candidate, raw_symbol)
        except Exception as error:
            log.info("indicator: get_ohlc(%s, %s) failed: %s", candidate, raw_symbol, error)
            data = None
        if data and data.get("close") and len(data["close"]) >= 30:
            ohlc = data
            exchange = candidate
            break

    if not ohlc:
        log.info("indicator: no candles for %s - showing suggestions", raw_symbol)
        reply_suggestions(chat_id, raw_symbol, "indicator")
        return

    price = ohlc["close"][-1]
    open_price = (ohlc.get("open") or [None])[-1]
    change_pct = ((price / open_price) - 1.0) * 100.0 if open_price else None
    company = ohlc.get("name") or raw_symbol
    currency = "$" if exchange == "US" else "\u20b9"
    log.info("indicator: %s resolved on %s (%d candles, %s)", raw_symbol, exchange, len(ohlc["close"]), indicator_arg or "all")

    if key is None:
        _send_full_card(chat_id, raw_symbol, company, price, change_pct, currency, ohlc)
        return

    from ..scanner.indicator_report import build_indicator_report
    lines = build_indicator_report(raw_symbol, company, price, change_pct, ohlc, key, currency)
    reply_messages(chat_id, split_messages(lines))


def _send_full_card(chat_id, raw_symbol, company, price, change_pct, currency, ohlc) -> None:
    """The full all-indicators card (same format as the /scan500 TOP 10 cards)."""
    if len(ohlc["close"]) < 220:
        reply(
            chat_id,
            f"<code>{escape(raw_symbol)}</code> has only {len(ohlc['close'])} daily "
            "candles \u2014 the full card needs ~220 (1 year). Try a single indicator "
            f"instead: <code>/indicator {escape(raw_symbol)} rsi</code>",
        )
        return
    try:
        import corporate_actions.scanner as scanner
    except Exception as error:
        log.warning("indicator: scanner unavailable: %s", error)
        reply(chat_id, "Scanner engine unavailable (pandas missing?).")
        return
    finding = scanner.scan_stock(ohlc, index_close=None)
    if finding is None:
        reply(chat_id, f"Could not compute indicators for <code>{escape(raw_symbol)}</code>.")
        return
    finding = scanner.build_plan(finding)
    score, breakdown = scanner.score_stock(finding)
    from ..scanner.report import _detail_card_lines
    change_text = ""
    if change_pct is not None:
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
        change_text = f"  {color}{arrow} {change_pct:+.2f}%"
    lines = [f"\U0001F50D <b>FULL INDICATOR CARD</b> \u2014 <code>{escape(raw_symbol)}</code> "
             f"(all indicators, /scan500 format)"]
    lines.append(f"Price: <b>{currency}{price:,.2f}</b>{change_text}  \u00b7  Daily candles")
    lines.append("")
    lines.extend(_detail_card_lines(finding, score, breakdown))
    lines.append("")
    lines.append(f"\U0001F4A1 <i>Tip: <code>/indicator {escape(raw_symbol)} rsi</code> for a "
                 f"one-indicator deep-dive \u00b7 <code>/fundamentalreport {escape(raw_symbol)}</code> "
                 "for fundamentals.</i>")
    reply_messages(chat_id, split_messages(lines))
