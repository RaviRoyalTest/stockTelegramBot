"""Scanner report rendering (Telegram HTML lines)."""

RULE_LINES = [
    "1. Weekly Supertrend RED or price below 200 SMA",
    "2. Delivery % < 40 (intraday churning)",
    "3. Chaikin Money Flow (CMF 20) < 0.00",
    "4. Mansfield Relative Strength (MRS) < 0.00 vs NIFTY 500",
    "5. R:R to Target 2 < 1:2.0 or Stop Loss > 8%",
    "6. Major unhedged binary event / governance risk",
    "7. Avg daily traded value < \u20b910 crore or wide spread",
]

_FIELD_LABELS = [
    ("price", "Last Price"),
    ("ema20", "EMA 20"),
    ("ema50", "EMA 50"),
    ("ema100", "EMA 100"),
    ("ema200", "EMA 200"),
    ("rsi14", "RSI (14)"),
    ("macd_line", "MACD line"),
    ("macd_signal", "MACD signal"),
    ("macd_hist", "MACD hist"),
    ("adx14", "ADX (14)"),
    ("pdi", "+DI"),
    ("mdi", "-DI"),
    ("atr14", "ATR (14)"),
    ("atr_percent", "ATR % of price"),
    ("cmf20", "CMF (20)"),
    ("mfi14", "MFI (14)"),
    ("obv_trend", "OBV trend"),
    ("delivery_estimate", "Delivery est."),
    ("aroon_up", "Aroon Up"),
    ("aroon_down", "Aroon Down"),
    ("donchian_high", "52w High"),
    ("donchian_low", "52w Low"),
    ("distance_from_52w_high", "Dist. to 52w High"),
    ("percent_52w_range", "52w range pos."),
    ("squeeze_on", "TTM Squeeze"),
    ("bollinger_position", "Bollinger pos."),
    ("weekly_supertrend", "Weekly Supertrend"),
    ("gmma_bull", "GMMA bullish"),
    ("avwap", "Anchored VWAP"),
    ("above_avwap", "Above Anch. VWAP"),
    ("mansfield_rs", "Mansfield RS"),
    ("average_daily_traded_value_crores", "ADTV (\u20b9cr)"),
]


def _format_field(finding: dict, key: str) -> str:
    value = finding.get(key)
    if value is None:
        return "-"
    if key in ("price", "ema20", "ema50", "ema100", "ema200", "atr14",
               "avwap", "donchian_high", "donchian_low"):
        return f"\u20b9{value:,.2f}"
    if key in ("distance_from_52w_high", "percent_52w_range"):
        return f"{value:+.1f}%"
    if key in ("atr_percent",):
        return f"{value:.1f}%"
    if key in ("rsi14", "adx14", "pdi", "mdi", "mfi14", "aroon_up", "aroon_down", "bollinger_position"):
        return f"{value:.1f}"
    if key in ("cmf20", "macd_line", "macd_signal", "macd_hist", "mansfield_rs"):
        return f"{value:+.2f}"
    if key in ("delivery_estimate",):
        return f"{value:.0f}%"
    if key in ("average_daily_traded_value_crores",):
        return f"{value:.1f}"
    if key == "obv_trend":
        return "rising" if value == "rising" else "falling"
    if key == "squeeze_on":
        return "ON" if value else "OFF"
    if key == "gmma_bull":
        return "bullish" if value else "bearish"
    if key == "above_avwap":
        return "yes" if value else "no"
    return str(value)


def _detail_lines(finding: dict) -> list[str]:
    lines = []
    for key, label in _FIELD_LABELS:
        lines.append(f"  {label}: <b>{_format_field(finding, key)}</b>")
    lines.append(f"  Entry: <b>\u20b9{finding['entry']:,.2f}</b>  \u00b7  SL: <b>\u20b9{finding['stop_loss']:,.2f}</b>")
    lines.append(f"  Targets: \u20b9{finding['target_1']:,.2f} / \u20b9{finding['target_2']:,.2f} / \u20b9{finding['target_3']:,.2f}")
    lines.append(f"  R:R: T1 {finding['reward_risk_target_1']:.1f}:1 \u00b7 T2 {finding['reward_risk_target_2']:.1f}:1 \u00b7 T3 {finding['reward_risk_target_3']:.1f}:1  \u00b7  "
                 f"SL {finding['stop_loss_percent']:.1f}%")
    return lines


def _hourly_roadmap(top: dict) -> list[str]:
    entry = top["entry"]
    target_1, target_2, target_3 = top["target_1"], top["target_2"], top["target_3"]
    return [
        "<b>\U0001F535 HOURLY EXECUTION ROADMAP (IST)</b>",
        f"\u2022 <b>09:15\u201310:15</b> Opening vol &amp; gap check \u2014 note gap vs "
        f"entry {entry:,.0f}",
        f"\u2022 <b>10:15\u201311:15</b> \U0001F7E2 Primary entry window (VWAP reclaim / "
        f"ORB above {entry:,.0f})",
        f"\u2022 <b>11:15\u201312:15</b> Trend confirmation &amp; pyramiding \u2014 T1 {target_1:,.0f}",
        f"\u2022 <b>12:15\u201313:15</b> Mid-day consolidation \u2014 trail SL to breakeven",
        f"\u2022 <b>13:15\u201314:15</b> European open \u2014 drive toward T2 {target_2:,.0f}",
        f"\u2022 <b>14:15\u201315:30</b> Closing power hour \u2014 T3 {target_3:,.0f} or square off",
    ]


def format_report(session: dict) -> list[str]:
    """Render the full scanner report as HTML lines for Telegram."""
    regime = session["regime"]
    lines = []
    lines.append("\U0001F4CA <b>NIFTY 500 \u2014 ADVANCED CNC/MIS SCANNER</b>")
    lines.append("")

    # 1. Market regime & breadth
    lines.append("<b>\U0001F300 MARKET REGIME &amp; BREADTH</b>")
    lines.append(f"MARKET REGIME: <b>{regime['label']}</b>")
    for detail in regime["details"]:
        lines.append(f"  \u2022 {detail}")
    lines.append("")

    # 2. Rejection rules
    lines.append("<b>\u26D4 STRICT \u201cDO NOT BUY / DO NOT SHOW\u201d RULES</b>")
    for rule in RULE_LINES:
        lines.append(f"  \u2022 {rule}")
    lines.append("")

    # 3. Rejected & excluded
    rejected = session.get("rejected", [])
    lines.append("<b>\u26D4 REJECTED &amp; EXCLUDED</b>")
    if rejected:
        for symbol, name, price, reasons in rejected[:12]:
            lines.append(f"  \u2022 <b>{symbol}</b> \u2014 {', '.join(reasons)}")
        if len(rejected) > 12:
            lines.append(f"  \u2026 and {len(rejected) - 12} more rejected (see rules)")
    else:
        lines.append("  None \u2014 every scanned stock passed the filters.")
    lines.append("")

    # 4. Top trade setup
    approved = session.get("approved", [])
    top = approved[0] if approved else None
    if top:
        top_fields = top["fields"]
        score, breakdown = top["score"], top["breakdown"]
        lines.append("<b>\U0001F3C6 #1 TOP TRADE SETUP</b>")
        lines.append(f"<b>{top_fields['name']}</b> ({top_fields['symbol']}.NS)  \u00b7  "
                     f"Score <b>{score:.0f}/100</b>")
        for key, value in breakdown.items():
            lines.append(f"  {key}: <b>{value:.0f}</b>")
        lines.append("<b>\U0001F4C8 CNC DELIVERY SETUP (Daily)</b>")
        lines.append(f"\U0001F7E2 ENTRY ZONE: <b>\u20b9{top_fields['entry']:,.2f}</b>  \u00b7  "
                     f"\u26A1 CONFIRM: VWAP reclaim / bullish candle close")
        lines.append(f"\U0001F534 STOP LOSS: <b>\u20b9{top_fields['stop_loss']:,.2f}</b>  "
                     f"(< {top_fields['stop_loss_percent']:.1f}% risk)")
        lines.append(f"\U0001F3AF TARGETS: <b>\u20b9{top_fields['target_1']:,.2f}</b> / "
                     f"<b>\u20b9{top_fields['target_2']:,.2f}</b> / <b>\u20b9{top_fields['target_3']:,.2f}</b>")
        lines.append(f"  R:R \u2248 1:{top_fields['reward_risk_target_2']:.1f} to T2")
        lines.append("")
        lines.extend(_hourly_roadmap(top_fields))
        lines.append("")
        lines.append("<b>\U0001F4CB FULL TECHNICAL MATRIX (TOP PICK)</b>")
        lines.extend(_detail_lines(top_fields))
        lines.append("")

    # 5. Approved matrix
    if approved:
        lines.append("<b>\U0001F4CA APPROVED STOCKS MATRIX</b>")
        lines.append("  Sym \u00b7 Score \u00b7 RSI \u00b7 ADX \u00b7 CMF \u00b7 MFI \u00b7 "
                     "52w% \u00b7 Entry \u00b7 R:R(T2)")
        for item in approved[:8]:
            finding = item["fields"]
            lines.append(
                f"  <b>{finding['symbol']}</b> \u00b7 {item['score']:.0f} \u00b7 "
                f"{finding['rsi14']:.0f} \u00b7 {finding['adx14']:.0f} \u00b7 {finding['cmf20']:+.2f} \u00b7 "
                f"{finding['mfi14']:.0f} \u00b7 {finding['percent_52w_range']:.0f}% \u00b7 "
                f"\u20b9{finding['entry']:,.0f} \u00b7 {finding['reward_risk_target_2']:.1f}")
        if len(approved) > 8:
            lines.append(f"  \u2026 +{len(approved) - 8} more")
        lines.append("")

    # 6. CNC vs MIS table
    if top:
        lines.append("<b>\u23F1 CNC vs MIS EXECUTION TABLE</b>")
        lines.append("  <b>CNC (Delivery):</b> Daily/weekly trend + CMF > 0 + delivery est. "
                     "> 50% \u2192 swing hold toward T2/T3")
        lines.append("  <b>MIS (Intraday):</b> 5m/15m VWAP reclaim + ORB + volume spurt "
                     "\u2192 exit by 3:30 PM (T1 or stop)")
        lines.append("")

    number_scanned = session.get("scanned", 0)
    lines.append(f"\U0001F4A1 <i>Scanned {number_scanned} NIFTY 500 stocks. Data: Yahoo Finance "
                 "daily candles. Delivery % is an estimate from money-flow (real NSE "
                 "delivery data is not public via this feed). Not investment advice.</i>")
    return lines
