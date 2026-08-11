"""Harmonic report renderers: full single-stock report + compact scan rows."""
from __future__ import annotations

from ..core.numbers import format_money
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


def format_report(result) -> list[str]:
    """Render the harmonic analysis as HTML lines for Telegram."""
    symbol = f"{result['symbol']}.NS" if result["exchange"] == "NSE" else f"{result['symbol']}.BO"
    lines = []
    lines.append(
        f"\U0001F3C6 <b>HARMONIC PATTERN SCAN</b>\n"
        f"\U0001F4CA {escape(result['name'].upper())} ({escape(symbol)})"
    )
    lines.append(f"\U0001F5D3 Timeframe: <b>{result['timeframe'].upper()}</b>  \u00b7  {result['bars']} bars  \u00b7  RSI {result['rsi']:g}" if result["rsi"] is not None else
                 f"\U0001F5D3 Timeframe: <b>{result['timeframe'].upper()}</b>  \u00b7  {result['bars']} bars")
    lines.append(f"Last Price: <b>{format_money(result['price'])}</b>")
    lines.append("")

    # 4. Pattern
    lines.append("<b>\U0001F3AF HARMONIC PATTERN</b>")
    lines.append(f"<b>{escape(result['pattern'])}</b>" if result["pattern"] else
                 "<i>No complete harmonic pattern detected</i>")
    if result.get("matches"):
        lines.append("Detected: " + ", ".join(escape(match) for match in result["matches"]))
    lines.append("")

    # 5. Pattern points
    lines.append("<b>\U0001F4CB PATTERN POINTS</b>")
    points = result.get("points")
    if points:
        for label in ("X", "A", "B", "C"):
            value = points.get(label)
            if value is not None:
                lines.append(f"  {label}: {format_money(value)}")
        d_price = points.get("D")
        if d_price is not None:
            lines.append(f"  D: <b>{format_money(d_price)}</b> (completion)")
        else:
            lines.append(f"  D: <b>{format_money(result['d_completion_price'])}</b> (projected)")
    lines.append("")

    # 6. Fibonacci ratios
    lines.append("<b>\U0001F522 FIBONACCI RATIOS</b>")
    spec = PATTERNS.get(str(result["pattern"]).split(" ")[0]) if result["pattern"] else None
    ratios = result.get("ratios")
    if ratios and spec:
        lines.append(f"  B retr. of XA: <b>{ratios['b_ratio']:.2f}</b>  (ideal {spec['b_ideal']:.2f})")
        lines.append(f"  C retr. of AB: <b>{ratios['c_ratio']:.2f}</b>  (range {spec['c_ratio'][0]:.2f}\u2013{spec['c_ratio'][1]:.2f})")
        d_base_leg_label = "of XA" if spec["d_base_leg"] == "x_a_leg" else "of XC"
        if ratios.get("d_ratio") is not None:
            lines.append(f"  D {d_base_leg_label}: <b>{ratios['d_ratio']:.2f}</b>  (ideal {spec['d_ideal']:.2f})")
        else:
            lines.append(f"  D {d_base_leg_label}: <b>{spec['d_ideal']:.2f}</b> (projected until D completes)")
    elif result.get("points"):
        lines.append("  Projected D at ideal Gartley 0.786 retracement.")
    else:
        lines.append("  No ratios to report.")
    lines.append("")

    # 7. PRZ
    potential_reversal_zone = result.get("potential_reversal_zone")
    if potential_reversal_zone:
        lines.append("<b>\U0001F6E1 PRZ (Potential Reversal Zone)</b>")
        lines.append(f"  Upper: <b>{format_money(potential_reversal_zone['upper'])}</b>")
        lines.append(f"  Lower: <b>{format_money(potential_reversal_zone['lower'])}</b>")
        lines.append(f"  Midpoint: <b>{format_money(potential_reversal_zone['mid'])}</b>")
        distance = potential_reversal_zone["distance"]
        tag = "inside PRZ" if potential_reversal_zone["lower"] <= result["price"] <= potential_reversal_zone["upper"] else \
            ("above PRZ" if distance > 0 else "below PRZ")
        lines.append(f"  Current price: {tag} ({distance:+,.2f} vs midpoint)")
    lines.append("")

    # 8. Status
    lines.append("<b>\U0001F504 PATTERN STATUS</b>")
    lines.append(f"<b>{escape(result['status'])}</b>")
    if "forming" in (result.get("status") or "").lower() or "approaching" in (result.get("status") or "").lower():
        lines.append("  <i>D has not completed \u2014 no entry is valid until price reaches the PRZ "
                     "and confirms a reversal.</i>")
    lines.append("")

    # 9-13. Trade plan (only when the setup is still actionable)
    plan = result.get("plan")
    direction = result.get("direction")
    status = result.get("status") or ""
    stale = status in ("Pattern completed", "Pattern invalidated") and plan
    near_reversal_zone = False
    potential_reversal_zone = result.get("potential_reversal_zone")
    if potential_reversal_zone:
        near_reversal_zone = abs(result["price"] - potential_reversal_zone["mid"]) <= 0.10 * result["price"]
    if plan and direction and not (stale and not near_reversal_zone):
        direction_label = "Bullish reversal setup" if direction == "bullish" else "Bearish reversal setup"
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        if stale:
            lines.append("  <i>Setup already played out \u2014 levels shown for reference only.</i>")
        lines.append(f"  Direction: <b>{direction_label}</b>")
        lines.append(f"  Entry (on confirmation): <b>{format_money(plan['entry'])}</b>")
        lines.append(f"  Stop Loss: <b>{format_money(plan['stop_loss'])}</b>")
        lines.append("  Targets:")
        targets = plan["targets"]
        reward_risk = plan["reward_risk"]
        for name, key in (("T1", "target_1"), ("T2", "target_2"), ("T3", "target_3")):
            reward_risk_string = f"  (R:R ≈ {reward_risk[key]:.1f}:1)" if key in reward_risk and reward_risk[key] else ""
            lines.append(f"    {name}: <b>{format_money(targets[key])}</b>{reward_risk_string}")
        lines.append("  <i>Do NOT enter just because price touches the PRZ \u2014 wait for a "
                     "rejection/reversal candle, a break of the confirmation level, or volume.")
    elif plan and direction:
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        lines.append("  <i>Setup is no longer actionable \u2014 price has moved well past the PRZ. "
                     "Watch for a fresh XA swing before considering this pattern.</i>")
    lines.append("")

    # 14. Confirmation
    lines.append("<b>\U0001F50D CONFIRMATION</b>")
    if result["notes"]:
        lines.extend("  \u2022 " + note for note in result["notes"])
    else:
        lines.append("  No confirmation data available.")

    # 15. Final signal
    signal = result["signal"]
    emoji = {"BUY": "\U0001F7E2", "SELL": "\U0001F534",
             "WAIT": "\U0001F7E1", "NO TRADE": "\u26AA"}.get(signal, "\u26AA")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{emoji} <b>FINAL SIGNAL: {signal}</b>")
    lines.append("")
    lines.append(f"\U0001F4A1 <i>Tip: {escape(result['symbol'])} on the {result['timeframe']} chart. "
                 "A PRZ is a potential area, not a guaranteed reversal.</i>")
    return lines


def _format_change(change_pct: float) -> str:
    """Format a price-move % with the green-up / red-down arrow icon."""
    arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
    color_icon = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
    sign = "+" if change_pct >= 0 else ""
    return f" {color_icon}{arrow} ({sign}{change_pct:.2f}%)"


def format_scan_row(result) -> str:
    """One compact entry (two lines) per stock for the bulk harmonic screener.

    Line 1: symbol, current price (+/-% move on this chart's last bar),
    pattern, direction, status and signal.
    Line 2 (indented): the PRZ range, the projected/completed D level and
    how far price currently is from the zone.
    """
    arrow = "\u25b2" if result.get("direction") == "bullish" else "\u25bc"
    change = ""
    if result.get("change_pct") is not None:
        change_pct = result["change_pct"]
        change = _format_change(change_pct)
    elif result.get("prev_close"):
        change_pct = (result["price"] / result["prev_close"] - 1) * 100
        change = _format_change(change_pct)
    pattern = escape(result.get("pattern") or "?")
    status = escape(result.get("status") or "")
    line = (
        f"{arrow} <b>{escape(result['symbol'])}</b> "
        f"{format_money(result['price'])}{change} \u2014 <b>{pattern}</b> "
        f"({result.get('direction') or '?'}) \u2014 {status} \u00b7 {result.get('signal') or 'NO TRADE'}"
    )

    extra = []
    potential_reversal_zone = result.get("potential_reversal_zone")
    if potential_reversal_zone and potential_reversal_zone.get("lower") is not None and potential_reversal_zone.get("upper") is not None:
        extra.append(
            f"PRZ {format_money(potential_reversal_zone['lower'])}\u2013"
            f"{format_money(potential_reversal_zone['upper'])}"
        )
    if result.get("d_completion_price"):
        extra.append(f"D {format_money(result['d_completion_price'])}")
    if potential_reversal_zone and potential_reversal_zone.get("distance") is not None and result.get("price"):
        away = abs(potential_reversal_zone["distance"]) / result["price"] * 100
        if potential_reversal_zone["lower"] <= result["price"] <= potential_reversal_zone["upper"]:
            tag = "inside PRZ"
        elif potential_reversal_zone["distance"] > 0:
            tag = f"{away:.1f}% above PRZ"
        else:
            tag = f"{away:.1f}% below PRZ"
        extra.append(tag)
    if extra:
        line += "\n   " + " \u00b7 ".join(extra)
    return line
