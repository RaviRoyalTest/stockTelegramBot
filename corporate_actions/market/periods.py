"""Movement-screen period vocabulary (shared by the bot and the dashboard)."""

MOVERS_PERIODS = {
    # intraday: ("intraday", minutes)
    "5m": ("intraday", 5), "10m": ("intraday", 10), "15m": ("intraday", 15),
    "30m": ("intraday", 30), "45m": ("intraday", 45),
    "1h": ("intraday", 60), "2h": ("intraday", 120), "4h": ("intraday", 240),
    # multi-day: ("days", N)
    "today": ("days", 1), "day": ("days", 1), "1d": ("days", 1),
    "2d": ("days", 2), "3d": ("days", 3), "5d": ("days", 5), "7d": ("days", 7),
    "1w": ("days", 7), "week": ("days", 7), "2w": ("days", 14),
    "1mo": ("days", 30), "month": ("days", 30), "3mo": ("days", 90),
    "6mo": ("days", 180), "1y": ("days", 365), "year": ("days", 365),
}


def period_label(kind: str, value: int) -> str:
    """Human label for a period, e.g. ('intraday', 60) -> 'last 1h'."""
    if kind == "intraday":
        if value % 60 == 0:
            return f"last {value // 60}h"
        return f"last {value}m"
    if value == 1:
        return "today"
    if value == 7:
        return "last 1 week"
    if value == 14:
        return "last 2 weeks"
    if value == 30:
        return "last 1 month"
    if value == 90:
        return "last 3 months"
    if value == 180:
        return "last 6 months"
    if value == 365:
        return "last 1 year"
    return f"last {value} days"
