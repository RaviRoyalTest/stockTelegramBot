"""Market-regime summary from index candles + universe breadth."""
from __future__ import annotations

import pandas as pd

from .indicators import ema, safe_last


def market_regime(benchmark: dict | None, breadth: dict, benchmark_label: str = "NIFTY 50",
                  vix_label: str = "India VIX") -> dict:
    """Summarise regime from the index candles + breadth of the universe."""
    regime = {"label": "MIXED", "details": [], "breadth": breadth}
    details = []
    if benchmark and len(benchmark["close"]) > 60:
        closes = benchmark["close"]
        price = closes[-1]
        ema50 = safe_last(ema(pd.Series(closes), 50))
        ema200 = safe_last(ema(pd.Series(closes), 200))
        count = len(closes)
        return_5d = (price / closes[max(0, count - 6)] - 1.0) * 100.0 if count > 6 else None
        return_20d = (price / closes[max(0, count - 21)] - 1.0) * 100.0 if count > 21 else None
        return_200d = (price / closes[max(0, count - 201)] - 1.0) * 100.0 if count > 201 else None
        details.append(f"{benchmark_label} {price:,.0f}  (5d {return_5d:+.1f}% / 20d {return_20d:+.1f}%)")
        if ema50 is not None:
            details.append(f"{benchmark_label} vs 50 EMA: {'above' if price > ema50 else 'below'} "
                           f"({((price / ema50 - 1) * 100):+.1f}%)")
        if ema200 is not None:
            details.append(f"{benchmark_label} vs 200 EMA: {'above' if price > ema200 else 'below'}")
    vix = breadth.get("vix")
    if vix is not None:
        details.append(f"{vix_label} {vix:.1f} ({'low/stable' if vix < 15 else 'elevated' if vix < 22 else 'high stress'})")
    breadth_above_ema50 = breadth.get("above_ema50")
    breadth_above_ema200 = breadth.get("above_ema_200")
    advancing_count = breadth.get("advance")
    declining_count = breadth.get("decline")
    if breadth_above_ema50 is not None:
        details.append(f"Breadth: {breadth_above_ema50:.0f}% above 50 EMA · {breadth_above_ema200:.0f}% above 200 EMA")
    if advancing_count is not None:
        details.append(f"Advance/Decline: {advancing_count:.0f}/{declining_count:.0f} ({advancing_count / max(declining_count, 1):.2f})")
    if breadth_above_ema50 is not None and breadth_above_ema200 is not None and vix is not None:
        if breadth_above_ema50 >= 55 and breadth_above_ema200 >= 45 and vix < 22:
            regime["label"] = "BULLISH"
        elif breadth_above_ema50 >= 45 and breadth_above_ema200 >= 35 and vix < 25:
            regime["label"] = "SIDEWAYS-BULLISH"
        elif breadth_above_ema50 <= 30 and breadth_above_ema200 <= 20:
            regime["label"] = "BEARISH"
        elif breadth_above_ema50 <= 45 or vix >= 25:
            regime["label"] = "HIGH VOLATILITY / RISK-OFF"
        else:
            regime["label"] = "SIDEWAYS"
    regime["details"] = details
    return regime
