"""Text helpers: Telegram HTML escaping, message chunking, HTML stripping."""
from __future__ import annotations

import html as _html
import re


def escape(text) -> str:
    """Escape text for Telegram HTML parse mode.

    quote=True also escapes " and ' so escaped values are safe inside HTML
    attributes too (e.g. the href of a news link).
    """
    return _html.escape(str(text or ""), quote=True)


def strip_html(text: str) -> str:
    """Remove HTML tags from a message (plain-text fallback for Telegram)."""
    return re.sub(r"<[^>]+>", "", text)


def split_messages(lines: list[str], max_len: int = 3800) -> list[str]:
    """Split text lines into messages under Telegram's 4096-char limit."""
    messages, current, size = [], [], 0
    for line in lines:
        line_len = len(line) + 1
        if current and size + line_len > max_len:
            messages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_len
    if current:
        messages.append("\n".join(current))
    return messages or [""]
