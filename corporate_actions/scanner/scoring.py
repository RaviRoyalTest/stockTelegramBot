"""100-point scoring model for scanner survivors."""

SCORE_QUALIFY = 75.0         # minimum score to qualify


def score_stock(finding: dict) -> tuple[float, dict[str, float]]:
    """100-point score; returns (total, breakdown by category)."""
    breakdown = {}

    # Trend alignment (15)
    trend_score = 0.0
    for flag in ("above_ema20", "above_ema50", "above_ema_200"):
        if finding.get(flag) is True:
            trend_score += 5.0
    trend_score = min(trend_score, 15.0)
    breakdown["Trend"] = round(trend_score, 1)

    # Multi-timeframe agreement (15): daily EMAs + weekly supertrend + GMMA
    multi_tf_score = 0.0
    if finding.get("above_ema20") and finding.get("above_ema50"):
        multi_tf_score += 5.0
    if finding.get("weekly_supertrend_up") is True:
        multi_tf_score += 5.0
    if finding.get("gmma_bull") is True:
        multi_tf_score += 5.0
    breakdown["MultiTF"] = round(min(multi_tf_score, 15.0), 1)

    # Momentum RSI/MACD (10)
    momentum_score = 0.0
    rsi_value = finding.get("rsi14")
    if rsi_value is not None and 55 <= rsi_value <= 75:
        momentum_score += 5.0
    elif rsi_value is not None and (50 <= rsi_value < 55 or 75 < rsi_value <= 85):
        momentum_score += 3.0
    if finding.get("macd_bull") is True:
        momentum_score += 3.0
    if finding.get("macd_hist_rising") is True:
        momentum_score += 2.0
    breakdown["Momentum"] = round(min(momentum_score, 10.0), 1)

    # ADX trend strength (10)
    breakdown["ADX"] = round(min(finding.get("adx_strength", 0) * 3.33, 10.0), 1)

    # Delivery & money flow CMF/MFI (10)
    accumulation_score = 0.0
    if finding.get("cmf20") is not None and finding["cmf20"] > 0.10:
        accumulation_score += 4.0
    elif finding.get("cmf20") is not None and finding["cmf20"] > 0.0:
        accumulation_score += 2.0
    mfi_value = finding.get("mfi14")
    if mfi_value is not None and 55 <= mfi_value <= 80:
        accumulation_score += 4.0
    elif mfi_value is not None and mfi_value > 45:
        accumulation_score += 2.0
    if finding.get("delivery_estimate") is not None and finding["delivery_estimate"] >= 55:
        accumulation_score += 2.0
    breakdown["Accumulation"] = round(min(accumulation_score, 10.0), 1)

    # Breakout & price action (10)
    breakout_score = 0.0
    distance_from_high = finding.get("distance_from_52w_high")
    if distance_from_high is not None and -8.0 <= distance_from_high <= 2.0:
        breakout_score += 4.0
    elif distance_from_high is not None and -15.0 <= distance_from_high < -8.0:
        breakout_score += 2.0
    if finding.get("squeeze_on") is False:
        breakout_score += 3.0
    if finding.get("aroon_up") is not None and finding["aroon_up"] >= 70:
        breakout_score += 3.0
    breakdown["Breakout"] = round(min(breakout_score, 10.0), 1)

    # Mansfield relative strength (10)
    relative_strength = finding.get("mansfield_rs")
    if relative_strength is not None and relative_strength > 5:
        breakdown["RelStrength"] = 10.0
    elif relative_strength is not None and relative_strength > 0:
        breakdown["RelStrength"] = round(5.0 + relative_strength / 2.0, 1)
    else:
        breakdown["RelStrength"] = 0.0

    # Entry location & anchored VWAP (5)
    entry_vwap_score = 0.0
    if finding.get("above_avwap") is True:
        entry_vwap_score += 2.5
    distance_from_avwap = (finding["price"] / finding["avwap"] - 1.0) * 100.0 if finding.get("avwap") else None
    if distance_from_avwap is not None and -3.0 <= distance_from_avwap <= 8.0:
        entry_vwap_score += 2.5
    breakdown["EntryVWAP"] = round(entry_vwap_score, 1)

    # Risk/reward (10)
    reward_risk = finding.get("reward_risk_target_2")
    if reward_risk is not None:
        breakdown["RiskReward"] = round(min(reward_risk / 2.0 * 10.0, 10.0), 1)
    else:
        breakdown["RiskReward"] = 0.0

    # Volatility / ATR buffer (5)
    volatility_score = 0.0
    atr_pct = finding.get("atr_percent")
    if atr_pct is not None and atr_pct <= 4.0:
        volatility_score = 5.0
    elif atr_pct is not None and atr_pct <= 6.0:
        volatility_score = 3.0
    breakdown["Volatility"] = volatility_score

    total = round(sum(breakdown.values()), 1)
    return total, breakdown
