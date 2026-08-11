"""100-point scoring model for scanner survivors."""

SCORE_QUALIFY = 75.0         # minimum score to qualify


def score_stock(f: dict) -> tuple[float, dict[str, float]]:
    """100-point score; returns (total, breakdown by category)."""
    s = {}

    # Trend alignment (15)
    t = 0.0
    for flag in ("above_ema20", "above_ema50", "above_ema200"):
        if f.get(flag) is True:
            t += 5.0
    t = min(t, 15.0)
    s["Trend"] = round(t, 1)

    # Multi-timeframe agreement (15): daily EMAs + weekly supertrend + GMMA
    m = 0.0
    if f.get("above_ema20") and f.get("above_ema50"):
        m += 5.0
    if f.get("wk_supertrend_up") is True:
        m += 5.0
    if f.get("gmma_bull") is True:
        m += 5.0
    s["MultiTF"] = round(min(m, 15.0), 1)

    # Momentum RSI/MACD (10)
    mo = 0.0
    rsi = f.get("rsi14")
    if rsi is not None and 55 <= rsi <= 75:
        mo += 5.0
    elif rsi is not None and (50 <= rsi < 55 or 75 < rsi <= 85):
        mo += 3.0
    if f.get("macd_bull") is True:
        mo += 3.0
    if f.get("macd_hist_rising") is True:
        mo += 2.0
    s["Momentum"] = round(min(mo, 10.0), 1)

    # ADX trend strength (10)
    s["ADX"] = round(min(f.get("adx_strength", 0) * 3.33, 10.0), 1)

    # Delivery & money flow CMF/MFI (10)
    dm = 0.0
    if f.get("cmf20") is not None and f["cmf20"] > 0.10:
        dm += 4.0
    elif f.get("cmf20") is not None and f["cmf20"] > 0.0:
        dm += 2.0
    mfi = f.get("mfi14")
    if mfi is not None and 55 <= mfi <= 80:
        dm += 4.0
    elif mfi is not None and mfi > 45:
        dm += 2.0
    if f.get("delivery_est") is not None and f["delivery_est"] >= 55:
        dm += 2.0
    s["Accumulation"] = round(min(dm, 10.0), 1)

    # Breakout & price action (10)
    bp = 0.0
    dist = f.get("dist_52w_hi")
    if dist is not None and -8.0 <= dist <= 2.0:
        bp += 4.0
    elif dist is not None and -15.0 <= dist < -8.0:
        bp += 2.0
    if f.get("squeeze_on") is False:
        bp += 3.0
    if f.get("aroon_up") is not None and f["aroon_up"] >= 70:
        bp += 3.0
    s["Breakout"] = round(min(bp, 10.0), 1)

    # Mansfield relative strength (10)
    mrs = f.get("mrs")
    if mrs is not None and mrs > 5:
        s["RelStrength"] = 10.0
    elif mrs is not None and mrs > 0:
        s["RelStrength"] = round(5.0 + mrs / 2.0, 1)
    else:
        s["RelStrength"] = 0.0

    # Entry location & anchored VWAP (5)
    ev = 0.0
    if f.get("above_avwap") is True:
        ev += 2.5
    dist_avwap = (f["price"] / f["avwap"] - 1.0) * 100.0 if f.get("avwap") else None
    if dist_avwap is not None and -3.0 <= dist_avwap <= 8.0:
        ev += 2.5
    s["EntryVWAP"] = round(ev, 1)

    # Risk/reward (10)
    rr = f.get("rr_t2")
    if rr is not None:
        s["RiskReward"] = round(min(rr / 2.0 * 10.0, 10.0), 1)
    else:
        s["RiskReward"] = 0.0

    # Volatility / ATR buffer (5)
    vb = 0.0
    atr_pct = f.get("atr_pct")
    if atr_pct is not None and atr_pct <= 4.0:
        vb = 5.0
    elif atr_pct is not None and atr_pct <= 6.0:
        vb = 3.0
    s["Volatility"] = vb

    total = round(sum(s.values()), 1)
    return total, s
