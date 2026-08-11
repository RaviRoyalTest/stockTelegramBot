"""Harmonic report renderers: full single-stock report + compact scan rows."""
from __future__ import annotations

from ..core.numbers import fmt_money
from ..core.text import escape
from .patterns import PATTERNS

# Most actionable first. Statuses come from analyze()/_classify/_find_forming.
SCAN_PRIORITY = {
    "Reversal confirmed": 0,       # signal BUY/SELL - price has confirmed
    "PRZ reached": 1,              # signal WAIT - price is inside the zone
    "D point/PRZ approaching": 2,  # forming, D near its projection
    "Pattern forming": 3,          # forming, D still projected ahead
    "Pattern completed": 4,        # played out - levels for reference only
}

# Rows shown per scan. Keeps the "smaller version" readable even on NIFTY 500.
SCAN_MAX_ROWS = 25


def format_report(r) -> list[str]:
    """Render the harmonic analysis as HTML lines for Telegram."""
    sym = f"{r['symbol']}.NS" if r["exchange"] == "NSE" else f"{r['symbol']}.BO"
    lines = []
    lines.append(
        f"\U0001F3C6 <b>HARMONIC PATTERN SCAN</b>\n"
        f"\U0001F4CA {escape(r['name'].upper())} ({escape(sym)})"
    )
    lines.append(f"\U0001F5D3 Timeframe: <b>{r['timeframe'].upper()}</b>  \u00b7  {r['bars']} bars  \u00b7  RSI {r['rsi']:g}" if r["rsi"] is not None else
                 f"\U0001F5D3 Timeframe: <b>{r['timeframe'].upper()}</b>  \u00b7  {r['bars']} bars")
    lines.append(f"Last Price: <b>{fmt_money(r['price'])}</b>")
    lines.append("")

    # 4. Pattern
    lines.append("<b>\U0001F3AF HARMONIC PATTERN</b>")
    lines.append(f"<b>{escape(r['pattern'])}</b>" if r["pattern"] else
                 "<i>No complete harmonic pattern detected</i>")
    if r.get("matches"):
        lines.append("Detected: " + ", ".join(escape(m) for m in r["matches"]))
    lines.append("")

    # 5. Pattern points
    lines.append("<b>\U0001F4CB PATTERN POINTS</b>")
    pts = r.get("points")
    if pts:
        for label in ("X", "A", "B", "C"):
            v = pts.get(label)
            if v is not None:
                lines.append(f"  {label}: {fmt_money(v)}")
        d = pts.get("D")
        if d is not None:
            lines.append(f"  D: <b>{fmt_money(d)}</b> (completion)")
        else:
            lines.append(f"  D: <b>{fmt_money(r['d_comp'])}</b> (projected)")
    lines.append("")

    # 6. Fibonacci ratios
    lines.append("<b>\U0001F522 FIBONACCI RATIOS</b>")
    spec = PATTERNS.get(str(r["pattern"]).split(" ")[0]) if r["pattern"] else None
    ratios = r.get("ratios")
    if ratios and spec:
        lines.append(f"  B retr. of XA: <b>{ratios['b']:.2f}</b>  (ideal {spec['b_ideal']:.2f})")
        lines.append(f"  C retr. of AB: <b>{ratios['c']:.2f}</b>  (range {spec['c'][0]:.2f}\u2013{spec['c'][1]:.2f})")
        dbase = "of XA" if spec["d_base"] == "xa" else "of XC"
        if ratios.get("d") is not None:
            lines.append(f"  D {dbase}: <b>{ratios['d']:.2f}</b>  (ideal {spec['d_ideal']:.2f})")
        else:
            lines.append(f"  D {dbase}: <b>{spec['d_ideal']:.2f}</b> (projected until D completes)")
    elif r.get("points"):
        lines.append("  Projected D at ideal Gartley 0.786 retracement.")
    else:
        lines.append("  No ratios to report.")
    lines.append("")

    # 7. PRZ
    prz = r.get("prz")
    if prz:
        lines.append("<b>\U0001F6E1 PRZ (Potential Reversal Zone)</b>")
        lines.append(f"  Upper: <b>{fmt_money(prz['upper'])}</b>")
        lines.append(f"  Lower: <b>{fmt_money(prz['lower'])}</b>")
        lines.append(f"  Midpoint: <b>{fmt_money(prz['mid'])}</b>")
        dist = prz["dist"]
        tag = "inside PRZ" if prz["lower"] <= r["price"] <= prz["upper"] else \
            ("above PRZ" if dist > 0 else "below PRZ")
        lines.append(f"  Current price: {tag} ({dist:+,.2f} vs midpoint)")
    lines.append("")

    # 8. Status
    lines.append("<b>\U0001F504 PATTERN STATUS</b>")
    lines.append(f"<b>{escape(r['status'])}</b>")
    if "forming" in (r.get("status") or "").lower() or "approaching" in (r.get("status") or "").lower():
        lines.append("  <i>D has not completed \u2014 no entry is valid until price reaches the PRZ "
                     "and confirms a reversal.</i>")
    lines.append("")

    # 9-13. Trade plan (only when the setup is still actionable)
    plan = r.get("plan")
    direction = r.get("direction")
    status = r.get("status") or ""
    stale = status in ("Pattern completed", "Pattern invalidated") and plan
    near_prz = False
    prz = r.get("prz")
    if prz:
        near_prz = abs(r["price"] - prz["mid"]) <= 0.10 * r["price"]
    if plan and direction and not (stale and not near_prz):
        dirlbl = "Bullish reversal setup" if direction == "bullish" else "Bearish reversal setup"
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        if stale:
            lines.append("  <i>Setup already played out \u2014 levels shown for reference only.</i>")
        lines.append(f"  Direction: <b>{dirlbl}</b>")
        lines.append(f"  Entry (on confirmation): <b>{fmt_money(plan['entry'])}</b>")
        lines.append(f"  Stop Loss: <b>{fmt_money(plan['sl'])}</b>")
        lines.append("  Targets:")
        t = plan["targets"]
        rr = plan["rr"]
        for name, key in (("T1", "t1"), ("T2", "t2"), ("T3", "t3")):
            rr_str = f"  (R:R ≈ {rr[key]:.1f}:1)" if key in rr and rr[key] else ""
            lines.append(f"    {name}: <b>{fmt_money(t[key])}</b>{rr_str}")
        lines.append("  <i>Do NOT enter just because price touches the PRZ \u2014 wait for a "
                     "rejection/reversal candle, a break of the confirmation level, or volume.")
    elif plan and direction:
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        lines.append("  <i>Setup is no longer actionable \u2014 price has moved well past the PRZ. "
                     "Watch for a fresh XA swing before considering this pattern.</i>")
    lines.append("")

    # 14. Confirmation
    lines.append("<b>\U0001F50D CONFIRMATION</b>")
    if r["notes"]:
        lines.extend("  \u2022 " + n for n in r["notes"])
    else:
        lines.append("  No confirmation data available.")

    # 15. Final signal
    sig = r["signal"]
    emoji = {"BUY": "\U0001F7E2", "SELL": "\U0001F534",
             "WAIT": "\U0001F7E1", "NO TRADE": "\u26AA"}.get(sig, "\u26AA")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{emoji} <b>FINAL SIGNAL: {sig}</b>")
    lines.append("")
    lines.append(f"\U0001F4A1 <i>Tip: {escape(r['symbol'])} on the {r['timeframe']} chart. "
                 "A PRZ is a potential area, not a guaranteed reversal.</i>")
    return lines


def _fmt_chg(change_pct: float) -> str:
    """Format a price-move % with the green-up / red-down arrow icon."""
    arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
    color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
    sign = "+" if change_pct >= 0 else ""
    return f" {color_icon}{arrow} ({sign}{change_pct:.2f}%)"


def format_scan_row(r) -> str:
    """One compact entry (two lines) per stock for the bulk harmonic screener.

    Line 1: symbol, current price (+/-% move on this chart's last bar),
    pattern, direction, status and signal.
    Line 2 (indented): the PRZ range, the projected/completed D level and
    how far price currently is from the zone.
    """
    arrow = "\u25b2" if r.get("direction") == "bullish" else "\u25bc"
    chg = ""
    if r.get("change_pct") is not None:
        chg_pct = r["change_pct"]
        chg = _fmt_chg(chg_pct)
    elif r.get("prev_close"):
        chg_pct = (r["price"] / r["prev_close"] - 1) * 100
        chg = _fmt_chg(chg_pct)
    pattern = escape(r.get("pattern") or "?")
    status = escape(r.get("status") or "")
    line = (
        f"{arrow} <b>{escape(r['symbol'])}</b> "
        f"{fmt_money(r['price'])}{chg} \u2014 <b>{pattern}</b> "
        f"({r.get('direction') or '?'}) \u2014 {status} \u00b7 {r.get('signal') or 'NO TRADE'}"
    )

    extra = []
    prz = r.get("prz")
    if prz and prz.get("lower") is not None and prz.get("upper") is not None:
        extra.append(
            f"PRZ {fmt_money(prz['lower'])}\u2013"
            f"{fmt_money(prz['upper'])}"
        )
    if r.get("d_comp"):
        extra.append(f"D {fmt_money(r['d_comp'])}")
    if prz and prz.get("dist") is not None and r.get("price"):
        away = abs(prz["dist"]) / r["price"] * 100
        if prz["lower"] <= r["price"] <= prz["upper"]:
            tag = "inside PRZ"
        elif prz["dist"] > 0:
            tag = f"{away:.1f}% above PRZ"
        else:
            tag = f"{away:.1f}% below PRZ"
        extra.append(tag)
    if extra:
        line += "\n   " + " \u00b7 ".join(extra)
    return line
