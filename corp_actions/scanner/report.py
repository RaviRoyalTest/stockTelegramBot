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
    ("atr_pct", "ATR % of price"),
    ("cmf20", "CMF (20)"),
    ("mfi14", "MFI (14)"),
    ("obv_trend", "OBV trend"),
    ("delivery_est", "Delivery est."),
    ("aroon_up", "Aroon Up"),
    ("aroon_dn", "Aroon Down"),
    ("donchian_hi", "52w High"),
    ("donchian_lo", "52w Low"),
    ("dist_52w_hi", "Dist. to 52w High"),
    ("pct_52w", "52w range pos."),
    ("squeeze_on", "TTM Squeeze"),
    ("bb_pos", "Bollinger pos."),
    ("wk_supertrend", "Weekly Supertrend"),
    ("gmma_bull", "GMMA bullish"),
    ("avwap", "Anchored VWAP"),
    ("above_avwap", "Above Anch. VWAP"),
    ("mrs", "Mansfield RS"),
    ("adtv_cr", "ADTV (\u20b9cr)"),
]


def _fmt_field(f: dict, key: str) -> str:
    v = f.get(key)
    if v is None:
        return "-"
    if key in ("price", "ema20", "ema50", "ema100", "ema200", "atr14",
               "avwap", "donchian_hi", "donchian_lo"):
        return f"\u20b9{v:,.2f}"
    if key in ("dist_52w_hi", "pct_52w"):
        return f"{v:+.1f}%"
    if key in ("atr_pct",):
        return f"{v:.1f}%"
    if key in ("rsi14", "adx14", "pdi", "mdi", "mfi14", "aroon_up", "aroon_dn", "bb_pos"):
        return f"{v:.1f}"
    if key in ("cmf20", "macd_line", "macd_signal", "macd_hist", "mrs"):
        return f"{v:+.2f}"
    if key in ("delivery_est",):
        return f"{v:.0f}%"
    if key in ("adtv_cr",):
        return f"{v:.1f}"
    if key == "obv_trend":
        return "rising" if v == "rising" else "falling"
    if key == "squeeze_on":
        return "ON" if v else "OFF"
    if key == "gmma_bull":
        return "bullish" if v else "bearish"
    if key == "above_avwap":
        return "yes" if v else "no"
    return str(v)


def _detail_lines(f: dict) -> list[str]:
    lines = []
    for key, label in _FIELD_LABELS:
        lines.append(f"  {label}: <b>{_fmt_field(f, key)}</b>")
    lines.append(f"  Entry: <b>\u20b9{f['entry']:,.2f}</b>  \u00b7  SL: <b>\u20b9{f['sl']:,.2f}</b>")
    lines.append(f"  Targets: \u20b9{f['t1']:,.2f} / \u20b9{f['t2']:,.2f} / \u20b9{f['t3']:,.2f}")
    lines.append(f"  R:R: T1 {f['rr_t1']:.1f}:1 \u00b7 T2 {f['rr_t2']:.1f}:1 \u00b7 T3 {f['rr_t3']:.1f}:1  \u00b7  "
                 f"SL {f['sl_pct']:.1f}%")
    return lines


def _hourly_roadmap(top: dict) -> list[str]:
    e = top["entry"]
    t1, t2, t3 = top["t1"], top["t2"], top["t3"]
    return [
        "<b>\U0001F535 HOURLY EXECUTION ROADMAP (IST)</b>",
        f"\u2022 <b>09:15\u201310:15</b> Opening vol &amp; gap check \u2014 note gap vs "
        f"entry {e:,.0f}",
        f"\u2022 <b>10:15\u201311:15</b> \U0001F7E2 Primary entry window (VWAP reclaim / "
        f"ORB above {e:,.0f})",
        f"\u2022 <b>11:15\u201312:15</b> Trend confirmation &amp; pyramiding \u2014 T1 {t1:,.0f}",
        f"\u2022 <b>12:15\u201313:15</b> Mid-day consolidation \u2014 trail SL to breakeven",
        f"\u2022 <b>13:15\u201314:15</b> European open \u2014 drive toward T2 {t2:,.0f}",
        f"\u2022 <b>14:15\u201315:30</b> Closing power hour \u2014 T3 {t3:,.0f} or square off",
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
    for d in regime["details"]:
        lines.append(f"  \u2022 {d}")
    lines.append("")

    # 2. Rejection rules
    lines.append("<b>\u26D4 STRICT \u201cDO NOT BUY / DO NOT SHOW\u201d RULES</b>")
    for r in RULE_LINES:
        lines.append(f"  \u2022 {r}")
    lines.append("")

    # 3. Rejected & excluded
    rejected = session.get("rejected", [])
    lines.append("<b>\u26D4 REJECTED &amp; EXCLUDED</b>")
    if rejected:
        for sym, name, price, reasons in rejected[:12]:
            lines.append(f"  \u2022 <b>{sym}</b> \u2014 {', '.join(reasons)}")
        if len(rejected) > 12:
            lines.append(f"  \u2026 and {len(rejected) - 12} more rejected (see rules)")
    else:
        lines.append("  None \u2014 every scanned stock passed the filters.")
    lines.append("")

    # 4. Top trade setup
    approved = session.get("approved", [])
    top = approved[0] if approved else None
    if top:
        tf = top["fields"]
        score, brk = top["score"], top["breakdown"]
        lines.append("<b>\U0001F3C6 #1 TOP TRADE SETUP</b>")
        lines.append(f"<b>{tf['name']}</b> ({tf['symbol']}.NS)  \u00b7  "
                     f"Score <b>{score:.0f}/100</b>")
        for key, val in brk.items():
            lines.append(f"  {key}: <b>{val:.0f}</b>")
        lines.append("<b>\U0001F4C8 CNC DELIVERY SETUP (Daily)</b>")
        lines.append(f"\U0001F7E2 ENTRY ZONE: <b>\u20b9{tf['entry']:,.2f}</b>  \u00b7  "
                     f"\u26A1 CONFIRM: VWAP reclaim / bullish candle close")
        lines.append(f"\U0001F534 STOP LOSS: <b>\u20b9{tf['sl']:,.2f}</b>  "
                     f"(< {tf['sl_pct']:.1f}% risk)")
        lines.append(f"\U0001F3AF TARGETS: <b>\u20b9{tf['t1']:,.2f}</b> / "
                     f"<b>\u20b9{tf['t2']:,.2f}</b> / <b>\u20b9{tf['t3']:,.2f}</b>")
        lines.append(f"  R:R \u2248 1:{tf['rr_t2']:.1f} to T2")
        lines.append("")
        lines.extend(_hourly_roadmap(tf))
        lines.append("")
        lines.append("<b>\U0001F4CB FULL TECHNICAL MATRIX (TOP PICK)</b>")
        lines.extend(_detail_lines(tf))
        lines.append("")

    # 5. Approved matrix
    if approved:
        lines.append("<b>\U0001F4CA APPROVED STOCKS MATRIX</b>")
        lines.append("  Sym \u00b7 Score \u00b7 RSI \u00b7 ADX \u00b7 CMF \u00b7 MFI \u00b7 "
                     "52w% \u00b7 Entry \u00b7 R:R(T2)")
        for item in approved[:8]:
            f = item["fields"]
            lines.append(
                f"  <b>{f['symbol']}</b> \u00b7 {item['score']:.0f} \u00b7 "
                f"{f['rsi14']:.0f} \u00b7 {f['adx14']:.0f} \u00b7 {f['cmf20']:+.2f} \u00b7 "
                f"{f['mfi14']:.0f} \u00b7 {f['pct_52w']:.0f}% \u00b7 "
                f"\u20b9{f['entry']:,.0f} \u00b7 {f['rr_t2']:.1f}")
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

    n_scanned = session.get("scanned", 0)
    lines.append(f"\U0001F4A1 <i>Scanned {n_scanned} NIFTY 500 stocks. Data: Yahoo Finance "
                 "daily candles. Delivery % is an estimate from money-flow (real NSE "
                 "delivery data is not public via this feed). Not investment advice.</i>")
    return lines
