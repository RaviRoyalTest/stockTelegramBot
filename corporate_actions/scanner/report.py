"""Scanner report rendering (Telegram HTML lines).

Designed to be skimmable: regime + breadth, the single best setup, a compact
approved table, then full per-stock indicator cards for the TOP 10 (every
computed indicator - trend, momentum, volume/flow, trade plan - in clear
sections), and a one-line rejection summary.
"""
from __future__ import annotations

from ..core.text import escape

RULE_LINES = [
    "Weekly Supertrend RED or price below 200 SMA",
    "Delivery % < 40 (intraday churning)",
    "Chaikin Money Flow (CMF 20) < 0.00",
    "Mansfield Relative Strength (MRS) < 0.00 vs NIFTY 500",
    "R:R to Target 2 < 1:2.0 or Stop Loss > 8%",
    "Major unhedged binary event / governance risk",
    "Avg daily traded value < \u20b910 crore or wide spread",
]


def _score_badges(finding: dict) -> str:
    """Compact one-line technical snapshot for a stock row."""
    parts = []
    if finding.get("rsi14") is not None:
        parts.append(f"RSI {finding['rsi14']:.0f}")
    if finding.get("adx14") is not None:
        parts.append(f"ADX {finding['adx14']:.0f}")
    if finding.get("cmf20") is not None:
        parts.append(f"CMF {finding['cmf20']:+.2f}")
    if finding.get("percent_52w_range") is not None:
        parts.append(f"52w {finding['percent_52w_range']:.0f}%")
    return "  \u00b7  ".join(parts)


def _entry_sl_targets(finding: dict) -> list[str]:
    entry = finding["entry"]
    return [
        f"ENTRY <b>\u20b9{entry:,.2f}</b>  \u00b7  "
        f"SL <b>\u20b9{finding['stop_loss']:,.2f}</b>  "
        f"(<b>{finding['stop_loss_percent']:.1f}%</b> risk)",
        f"TARGETS <b>\u20b9{finding['target_1']:,.2f}</b> / "
        f"<b>\u20b9{finding['target_2']:,.2f}</b> / "
        f"<b>\u20b9{finding['target_3']:,.2f}</b>  \u00b7  "
        f"R:R \u2248 <b>1:{finding['reward_risk_target_2']:.1f}</b> to T2",
    ]


def _top_pick_lines(top_fields: dict, score: float) -> list[str]:
    """Render the #1 setup as a clean entry/SL/targets card."""
    lines = []
    name = top_fields.get("name") or top_fields.get("symbol")
    lines.append(f"\U0001F3C6 <b>{name}</b> ({top_fields['symbol']}.NS)  \u00b7  "
                 f"Score <b>{score:.0f}/100</b>  \u00b7  "
                 f"Price <b>\u20b9{top_fields['price']:,.2f}</b>")
    lines.append(_score_badges(top_fields))
    lines.extend(_entry_sl_targets(top_fields))
    return lines


def _up_down(flag) -> str:
    """\U0001F7E2 / \U0001F534 / \U0001F7E1 for a true / false / None condition."""
    if flag is True:
        return "\U0001F7E2"
    if flag is False:
        return "\U0001F534"
    return "\U0001F7E1"


def _detail_card_lines(finding: dict, score: float, breakdown: dict | None = None) -> list[str]:
    """Full per-stock indicator card - every computed indicator, in sections.

    The card reads like the corporate-action alerts: company name + symbol on
    line one, score/price/move on line two, then clear \u2022-bulleted sections
    for TREND & STRUCTURE, MOMENTUM, VOLUME & FLOW and TRADE PLAN.
    """
    symbol = finding["symbol"]
    name = escape(finding.get("name") or symbol)
    price = finding["price"]
    lines = []

    # Header: name, symbol, score, price + today's move
    lines.append(f"\U0001F4B0 <b>{name}</b> (<code>{symbol}</code>)")
    header_bits = [f"Score <b>{score:.0f}/100</b>", f"Price <b>\u20b9{price:,.2f}</b>"]
    if finding.get("change_pct") is not None:
        change = finding["change_pct"]
        arrow = "\u25b2" if change >= 0 else "\u25bc"
        color = "\U0001F7E2" if change >= 0 else "\U0001F534"
        header_bits.append(f"{color}{arrow} {change:+.2f}%")
    lines.append("  \u00b7  ".join(header_bits))
    if breakdown:
        lines.append("Breakdown: " + " ".join(
            f"{escape(label)} {value:.0f}" for label, value in breakdown.items()
        ))
    lines.append("")

    # --- TREND & STRUCTURE ---
    lines.append("<b>\U0001F4C8 TREND & STRUCTURE</b>")
    ema_bits = []
    for span, key in ((20, "ema20"), (50, "ema50"), (100, "ema100"), (200, "ema200")):
        value = finding.get(key)
        if value is not None:
            ema_bits.append(f"EMA{span} \u20b9{value:,.2f}")
    if ema_bits:
        lines.append("  \u2022 " + "  \u00b7  ".join(ema_bits))
    position_bits = []
    for span, key in ((20, "above_ema20"), (50, "above_ema50"), (200, "above_ema_200")):
        if finding.get(key) is not None:
            position_bits.append(f"{_up_down(finding[key])} vs EMA{span}")
    if position_bits:
        lines.append("  \u2022 Price " + "  ".join(position_bits))
    if finding.get("sma50") is not None and finding.get("sma200") is not None:
        golden = finding.get("sma_golden")
        cross = "Golden cross (bullish)" if golden else "Death cross (bearish)"
        lines.append(
            f"  \u2022 SMA50 \u20b9{finding['sma50']:,.2f}  \u00b7  SMA200 \u20b9{finding['sma200']:,.2f}  "
            f"\u2014  {_up_down(golden)} {cross}"
        )
    if finding.get("weekly_supertrend"):
        up = finding["weekly_supertrend"] == "green"
        label = "Green (uptrend)" if up else "RED (downtrend)"
        lines.append(f"  \u2022 Weekly Supertrend: {_up_down(up)} {label}")
    if finding.get("gmma_bull") is not None:
        label = "Bullish (short EMAs above long EMAs)" if finding["gmma_bull"] else "Bearish (short EMAs below long EMAs)"
        lines.append(f"  \u2022 Guppy GMMA: {_up_down(finding['gmma_bull'])} {label}")
    if finding.get("donchian_high") is not None and finding.get("donchian_low") is not None:
        lines.append(
            f"  \u2022 Donchian 52W: High \u20b9{finding['donchian_high']:,.2f}  \u00b7  "
            f"Low \u20b9{finding['donchian_low']:,.2f}"
        )
        if finding.get("percent_52w_range") is not None:
            lines.append(f"  \u2022 52W range position: {finding['percent_52w_range']:.0f}%")
    if finding.get("avwap") is not None:
        label = "Price above" if finding.get("above_avwap") else "Price below"
        lines.append(f"  \u2022 Anchored VWAP \u20b9{finding['avwap']:,.2f} \u2014 {_up_down(finding.get('above_avwap'))} {label}")
    if finding.get("psar_dir"):
        bull = finding["psar_dir"] == "bull"
        label = "Bullish (SAR below price)" if bull else "Bearish (SAR above price)"
        lines.append(f"  \u2022 Parabolic SAR: {_up_down(bull)} {label}")

    # --- MOMENTUM ---
    lines.append("<b>\u26a1 MOMENTUM</b>")
    rsi = finding.get("rsi14")
    if rsi is not None:
        zone = "Oversold" if rsi <= 30 else ("Overbought" if rsi >= 70 else ("High" if rsi >= 60 else ("Low" if rsi <= 45 else "Neutral")))
        color = "\U0001F7E2" if rsi <= 45 else ("\U0001F534" if rsi >= 60 else "\U0001F7E1")
        lines.append(f"  \u2022 RSI(14): <b>{rsi:.1f}</b> {color} ({zone})")
    if finding.get("macd_line") is not None and finding.get("macd_signal") is not None:
        macd_bull = finding.get("macd_bull")
        macd_text = f"  \u2022 MACD(12,26,9): {finding['macd_line']:.2f} / {finding['macd_signal']:.2f}"
        if finding.get("macd_hist") is not None:
            macd_text += f" / {finding['macd_hist']:+.2f}"
        macd_text += f" \u2014 {_up_down(macd_bull)} {'Bullish' if macd_bull else 'Bearish'}"
        if finding.get("macd_hist_rising") is not None:
            macd_text += f" \u00b7 Histogram {'rising' if finding['macd_hist_rising'] else 'falling'}"
        lines.append(macd_text)
    if finding.get("adx14") is not None:
        adx_value = finding["adx14"]
        strength = ("Strong trend" if adx_value >= 40 else ("Trending" if adx_value >= 25 else ("Developing" if adx_value >= 20 else "Range-bound")))
        adx_text = f"  \u2022 ADX(14): <b>{adx_value:.1f}</b> ({strength})"
        if finding.get("pdi") is not None and finding.get("mdi") is not None:
            adx_text += f" \u00b7 +DI {finding['pdi']:.1f} / -DI {finding['mdi']:.1f}"
        lines.append(adx_text)
    if finding.get("aroon_up") is not None and finding.get("aroon_down") is not None:
        lines.append(f"  \u2022 Aroon: Up {finding['aroon_up']:.0f} \u00b7 Down {finding['aroon_down']:.0f}")
    if finding.get("stoch_k") is not None:
        stoch_text = f"  \u2022 Stochastic: %K {finding['stoch_k']:.0f}"
        if finding.get("stoch_d") is not None:
            stoch_text += f" / %D {finding['stoch_d']:.0f}"
        stoch_k = finding["stoch_k"]
        stoch_text += " (" + ("Overbought" if stoch_k >= 80 else ("Oversold" if stoch_k <= 20 else "Neutral")) + ")"
        lines.append(stoch_text)
    if finding.get("bb_upper") is not None and finding.get("bb_lower") is not None:
        bb_text = (
            f"  \u2022 Bollinger: U \u20b9{finding['bb_upper']:,.2f} \u00b7 "
            f"M \u20b9{finding['bb_mid']:,.2f} \u00b7 L \u20b9{finding['bb_lower']:,.2f}"
        )
        if finding.get("bb_percent_b") is not None:
            bb_text += f" \u00b7 %B {finding['bb_percent_b']:.0f}"
        lines.append(bb_text)
    if finding.get("cci20") is not None:
        cci_value = finding["cci20"]
        zone = "Overbought" if cci_value >= 100 else ("Oversold" if cci_value <= -100 else ("Bullish" if cci_value > 0 else "Bearish"))
        lines.append(f"  \u2022 CCI(20): <b>{cci_value:.0f}</b> ({zone})")

    # --- VOLUME & FLOW ---
    lines.append("<b>\U0001F4B5 VOLUME & FLOW</b>")
    cmf = finding.get("cmf20")
    if cmf is not None:
        zone = "Buying pressure" if cmf > 0.05 else ("Selling pressure" if cmf < -0.05 else "Neutral")
        lines.append(f"  \u2022 CMF(20): {cmf:+.2f} ({zone})")
    mfi = finding.get("mfi14")
    if mfi is not None:
        zone = "Overbought" if mfi >= 80 else ("Oversold" if mfi <= 20 else ("Accumulation" if mfi > 55 else "Neutral"))
        lines.append(f"  \u2022 MFI(14): {mfi:.1f} ({zone})")
    if finding.get("obv_trend"):
        lines.append(f"  \u2022 OBV: {'Rising' if finding['obv_trend'] == 'rising' else 'Falling'}")
    if finding.get("volume_ratio") is not None:
        ratio = finding["volume_ratio"]
        note = "above average" if ratio >= 1.2 else ("below average" if ratio <= 0.8 else "average")
        lines.append(f"  \u2022 Volume ratio: {ratio:.2f}x vs 20d avg ({note})")
    if finding.get("average_daily_traded_value_crores") is not None:
        lines.append(f"  \u2022 ADTV: \u20b9{finding['average_daily_traded_value_crores']:.0f} Cr")
    if finding.get("delivery_estimate") is not None:
        lines.append(f"  \u2022 Delivery (est.): {finding['delivery_estimate']:.0f}%")

    # --- TRADE PLAN ---
    lines.append("<b>\U0001F4C9 TRADE PLAN</b>")
    lines.extend(_entry_sl_targets(finding))
    if finding.get("atr14") is not None:
        atr_text = f"  \u2022 ATR(14): \u20b9{finding['atr14']:,.2f}"
        if finding.get("atr_percent") is not None:
            atr_text += f" ({finding['atr_percent']:.1f}% of price)"
        lines.append(atr_text)

    return lines


def format_report(session: dict) -> list[str]:
    """Render the full scanner report as HTML lines for Telegram."""
    regime = session["regime"]
    lines = []
    lines.append("\U0001F4CA <b>NIFTY 500 \u2014 ADVANCED SCANNER</b>")
    lines.append("")

    # 1. Market regime & breadth (concise)
    lines.append(f"\U0001F300 <b>Regime: {regime['label']}</b>")
    for detail in regime["details"]:
        lines.append(f"  \u2022 {detail}")
    lines.append("")

    # 2. Top pick(s): the best qualifying setup first
    approved = session.get("approved", [])
    if approved:
        top = approved[0]
        lines.append("<b>\U0001F3C6 #1 TOP TRADE SETUP</b>")
        lines.extend(_top_pick_lines(top["fields"], top["score"]))
        lines.append("")
        if len(approved) > 1:
            lines.append(f"\u26a1 <b>+{len(approved) - 1} more qualifying</b> - see the table below.")
            lines.append("")
    else:
        lines.append("\u26D4 <b>No stock passed the strict rules this run.</b> "
                     "Nothing qualifies - avoid the rejected names below.")
        lines.append("")

    # 3. Approved table (compact, fixed columns)
    if approved:
        lines.append("<b>\U0001F4CA QUALIFIED SETUPS</b>")
        lines.append("  <code>Sym    Score  Price   Entry   RSI  R:R</code>")
        for item in approved[:10]:
            finding = item["fields"]
            rsi = finding.get("rsi14")
            rr = finding.get("reward_risk_target_2")
            lines.append(
                f"  <code>{finding['symbol']:<7} {item['score']:5.0f} "
                f"{finding['price']:7,.0f} {finding['entry']:7,.0f} "
                f"{(f'{rsi:3.0f}' if rsi is not None else ' n/a'):>4}  "
                f"{('1:' + format(rr, '.1f')) if rr is not None else 'n/a':>6}</code>"
            )
        if len(approved) > 10:
            lines.append(f"  \u2026 +{len(approved) - 10} more")
        lines.append("")
        lines.append("\U0001F4A1 <i>Use /fundamentalreport SYMBOL for any ticker above, "
                     "then plan with the entry/SL/targets shown.</i>")
        lines.append("")

        # 3b. Top-10 full indicator detail cards (the requested deep dive)
        top_ten = approved[:10]
        lines.append("<b>\U0001F3C6 TOP 10 \u2014 FULL INDICATOR DETAIL</b>")
        lines.append("Every computed indicator for the highest-scored qualifying setups:")
        lines.append("")
        for index, item in enumerate(top_ten, 1):
            card = _detail_card_lines(item["fields"], item["score"], item.get("breakdown"))
            lines.append(f"<b>{index}.</b> {card[0]}")
            lines.extend(card[1:])
            lines.append("")

    # 4. Rejected summary - one line, not a wall of rules
    rejected = session.get("rejected", [])
    if rejected:
        top_reasons = ", ".join(dict.fromkeys(
            reason for _, _, _, reasons in rejected[:6] for reason in reasons
        )) or ""
        lines.append(
            f"\u26D4 <b>{len(rejected)} rejected</b> by the strict rules "
            f"(Supertrend/SMA, delivery, CMF, MRS, R:R, liquidity)."
        )
        if top_reasons:
            lines.append(f"  Most common: {top_reasons}")
        lines.append("")

    number_scanned = session.get("scanned", 0)
    lines.append(f"\U0001F4A1 <i>Scanned {number_scanned} NIFTY 500 stocks. Data: Yahoo Finance "
                 "daily candles. Delivery % is estimated from money-flow. "
                 "Not investment advice.</i>")
    return lines
