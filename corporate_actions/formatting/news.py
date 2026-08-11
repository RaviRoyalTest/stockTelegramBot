"""News headline renderers for Telegram."""
from __future__ import annotations

from ..core.dates import format_timestamp
from ..core.text import escape


def format_news_item(item: dict, index: int = 1) -> str:
    """Render one news headline with source and publish time."""
    title = escape(item.get("title") or "-")
    link = (item.get("link") or "").strip()
    published_at = format_timestamp(item.get("published_ts"))
    publisher = escape(item.get("publisher") or "")
    meta = " | ".join(part for part in (publisher, published_at) if part)
    head = f"<a href=\"{escape(link)}\">{title}</a>" if link else title
    return "\n".join([f"{index}. {head}"] + ([f"   {meta}"] if meta else []))


def format_news_list(symbol: str, exchange: str, items: list[dict]) -> str:
    """Render the latest news for one stock as a Telegram HTML message."""
    header = f"<b>Latest news - {escape(symbol)}</b> ({escape(exchange)})"
    if not items:
        return header + "\nNo recent news found."
    body = [format_news_item(item, index) for index, item in enumerate(items, 1)]
    return "\n".join([header] + body)
