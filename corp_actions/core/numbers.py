"""Number/price formatting helpers (pure, reusable)."""


def fmt_money(price, currency: str = "INR") -> str:
    """Format a price with a currency symbol and thousands separators."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return f"{price}"
    if currency == "INR":
        symbol = "\u20b9"
    elif currency == "USD":
        symbol = "$"
    else:
        symbol = f" {currency}"
    return f"{symbol}{value:,.2f}"


def format_num(value, nd: int = 1) -> str:
    """Format a number with `nd` decimals, trimming trailing zeros.

    '12.50' -> '12.5'; None or unparsable values render as 'N/A'.
    """
    if value is None:
        return "N/A"
    try:
        s = f"{float(value):.{nd}f}"
    except (TypeError, ValueError):
        return "N/A"
    return s.rstrip("0").rstrip(".") if "." in s else s
