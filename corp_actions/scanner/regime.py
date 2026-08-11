"""Market-regime summary from index candles + universe breadth."""
from __future__ import annotations

import pandas as pd

from .indicators import ema, safe_last


def market_regime(benchmark: dict | None, breadth: dict) -> dict:
    """Summarise regime from the index candles + breadth of the universe."""
    regime = {"label": "MIXED", "details": [], "breadth": breadth}
    details = []
    if benchmark and len(benchmark["close"]) > 60:
        closes = benchmark["close"]
        price = closes[-1]
        ema50 = safe_last(ema(pd.Series(closes), 50))
        ema200 = safe_last(ema(pd.Series(closes), 200))
        m = len(closes)
        ret_5d = (price / closes[max(0, m - 6)] - 1.0) * 100.0 if m > 6 else None
        ret_20d = (price / closes[max(0, m - 21)] - 1.0) * 100.0 if m > 21 else None
        ret_200d = (price / closes[max(0, m - 201)] - 1.0) * 100.0 if m > 201 else None
        details.append(f"NIFTY 50 {price:,.0f}  (5d {ret_5d:+.1f}% / 20d {ret_20d:+.1f}%)")
        if ema50 is not None:
            details.append(f"NIFTY 50 vs 50 EMA: {'above' if price > ema50 else 'below'} "
                           f"({((price / ema50 - 1) * 100):+.1f}%)")
        if ema200 is not None:
            details.append(f"NIFTY 50 vs 200 EMA: {'above' if price > ema200 else 'below'}")
    vix = breadth.get("vix")
    if vix is not None:
        details.append(f"India VIX {vix:.1f} ({'low/stable' if vix < 15 else 'elevated' if vix < 22 else 'high stress'})")
    b50 = breadth.get("above_ema50")
    b200 = breadth.get("above_ema200")
    adv = breadth.get("advance")
    dec = breadth.get("decline")
    if b50 is not None:
        details.append(f"Breadth: {b50:.0f}% above 50 EMA · {b200:.0f}% above 200 EMA")
    if adv is not None:
        details.append(f"Advance/Decline: {adv:.0f}/{dec:.0f} ({adv / max(dec, 1):.2f})")
    if b50 is not None and b200 is not None and vix is not None:
        if b50 >= 55 and b200 >= 45 and vix < 22:
            regime["label"] = "BULLISH"
        elif b50 >= 45 and b200 >= 35 and vix < 25:
            regime["label"] = "SIDEWAYS-BULLISH"
        elif b50 <= 30 and b200 <= 20:
            regime["label"] = "BEARISH"
        elif b50 <= 45 or vix >= 25:
            regime["label"] = "HIGH VOLATILITY / RISK-OFF"
        else:
            regime["label"] = "SIDEWAYS"
    regime["details"] = details
    return regime
