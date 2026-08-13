"""Scanner report rendering (Telegram HTML lines).

Designed to be skimmable: regime + breadth, the single best setup, a compact
approved table and a one-line rejection summary - no 30-field dump.
"""

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
