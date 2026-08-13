"""Telegram keyboard / markup builders (reply keyboards + inline buttons)."""


def quick_menu_markup() -> dict:
    """Persistent one-tap reply keyboard of the main commands.

    Each button label IS the command text (Telegram sends the label verbatim
    when tapped), so a tap runs the command through the normal dispatcher -
    no callback handling, no typing. Attach with send_message(reply_markup=...).
    """
    return {
        "keyboard": [
            ["/corpactionsformylist"],
            ["/myfavourites run", "/corpactions"],
            ["/corpactionssummary", "/topgainers 1h", "/toplosers 1h"],
            ["/watchlist", "/all", "/help"],
            ["/menu off"],
        ],
        "resize_keyboard": True,
    }


def hide_keyboard_markup() -> dict:
    """Remove the persistent quick-menu keyboard (ReplyKeyboardRemove)."""
    return {"remove_keyboard": True}


def example_markup(commands: list[str], per_row: int = 2) -> dict:
    """One-tap reply keyboard of example commands for a single command.

    Each button label IS a full example command (e.g. "/toplosers 2d"), so
    tapping it sends that text through the normal dispatcher - no typing.
    Works identically on mobile and desktop Telegram (ReplyKeyboardMarkup),
    and replaces the quick menu while it is shown.
    """
    rows = []
    for row_start in range(0, len(commands), per_row):
        rows.append(commands[row_start:row_start + per_row])
    return {"keyboard": rows, "resize_keyboard": True}


def symbol_buttons(symbols: list[str], prefix: str = "fund", per_row: int = 4) -> dict:
    """Inline keyboard of one button per symbol, e.g. tap PFC -> deep report.

    prefix is the callback prefix: "fund" opens /fundamentalreport,
    "ana" opens /fundamentalanalyze. Buttons make every symbol in a report
    tappable so users jump straight to fundamentals instead of typing.
    """
    rows = []
    for row_start in range(0, len(symbols), per_row):
        rows.append([
            {"text": symbol, "callback_data": f"{prefix}:{symbol}"}
            for symbol in symbols[row_start:row_start + per_row]
        ])
    return {"inline_keyboard": rows}


def fundamentals_button(label: str = "Get Fundamentals") -> dict:
    """One-tap button that enriches the current movers report with fundamentals.

    callback_data "mfund" tells the bot to fetch fundamentals for the last
    screen the user ran and send the full enriched report - so price-only
    movers reports stay fast and fundamentals are fetched only on demand.
    """
    return {"inline_keyboard": [[{"text": label, "callback_data": "mfund"}]]}
