"""Corporate-action taxonomy and small dict helpers."""
from __future__ import annotations

# Corporate actions are classified from the subject line into broad buckets so
# users can filter which alerts they receive (e.g. dividends only).
ACTION_TYPES = ("dividend", "bonus", "split", "rights", "buyback", "other")
TYPE_LABELS = {
    "dividend": "Dividend",
    "bonus": "Bonus",
    "split": "Split",
    "rights": "Rights",
    "buyback": "Buy-back",
    "other": "Other",
}

# Actions that INCREASE the number of shares a holder owns. Used by the
# /ca increase query (bonus + split + rights are all share-count increasing).
INCREASE_TYPES = ("bonus", "split", "rights")


def action_type(subject) -> str:
    """Classify a corporate-action subject into one of ACTION_TYPES."""
    text = (subject or "").lower()
    if "dividend" in text:
        return "dividend"
    if "bonus" in text:
        return "bonus"
    if "split" in text or "sub-division" in text or "sub division" in text:
        return "split"
    if "rights" in text or "right issue" in text:
        return "rights"
    if "buy back" in text or "buyback" in text:
        return "buyback"
    return "other"


def pick(record, *keys, default=""):
    """Return the first non-empty value found among candidate keys."""
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip() and str(value).strip() != "-":
            return str(value).strip()
    return default
