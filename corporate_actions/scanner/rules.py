"""Strict 'do not buy / do not show' rejection rules for the scanner."""

REJECT_MIN_DELIVERY_ESTIMATE = 40.0      # Rule 2 (proxy: CMF/MFI accumulation)
REJECT_MIN_CMF = 0.00           # Rule 3
REJECT_MIN_MANSFIELD_RS = 0.00           # Rule 4
REJECT_MIN_REWARD_RISK_TARGET_2 = 2.0          # Rule 5 risk:reward to Target 2
REJECT_MAX_STOP_LOSS_PERCENT = 8.0         # Rule 5 max stop loss
REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_CRORES = 10.0       # Rule 7 average daily traded value (₹ crore)
REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_MUSD = 10.0        # Rule 7 for US stocks ($ millions)


def rejection_reasons(finding: dict) -> list[str]:
    """Return the list of rejected rules for a scanned stock (empty = pass)."""
    reasons = []
    if finding.get("weekly_supertrend") == "red":
        reasons.append("Weekly Supertrend RED")
    if finding.get("above_ema200") is False:
        reasons.append("Price below 200 SMA")
    if finding.get("delivery_estimate") is not None and finding["delivery_estimate"] < REJECT_MIN_DELIVERY_ESTIMATE:
        reasons.append(f"Delivery est. {finding['delivery_estimate']:.0f}% < {REJECT_MIN_DELIVERY_ESTIMATE:.0f}%")
    if finding.get("cmf20") is not None and finding["cmf20"] < REJECT_MIN_CMF:
        reasons.append(f"CMF {finding['cmf20']:+.2f} < 0")
    if finding.get("mansfield_rs") is not None and finding["mansfield_rs"] < REJECT_MIN_MANSFIELD_RS:
        reasons.append(f"MRS {finding['mansfield_rs']:+.1f} < 0")
    if finding.get("reward_risk_target_2") is not None and finding["reward_risk_target_2"] < REJECT_MIN_REWARD_RISK_TARGET_2:
        reasons.append(f"R:R to T2 {finding['reward_risk_target_2']:.1f} < 2.0")
    if finding.get("stop_loss_percent") is not None and finding["stop_loss_percent"] > REJECT_MAX_STOP_LOSS_PERCENT:
        reasons.append(f"Stop {finding['stop_loss_percent']:.1f}% > {REJECT_MAX_STOP_LOSS_PERCENT}%")
    if finding.get("currency", "INR").upper() == "USD":
        adtv = finding.get("average_daily_traded_value_musd")
        if adtv is not None and adtv < REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_MUSD:
            reasons.append(f"ADTV ${adtv:.1f}M < ${REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_MUSD:.0f}M")
    elif finding.get("average_daily_traded_value_crores") is not None and finding["average_daily_traded_value_crores"] < REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_CRORES:
        reasons.append(f"ADTV ₹{finding['average_daily_traded_value_crores']:.1f}cr < ₹{REJECT_MIN_AVERAGE_DAILY_TRADED_VALUE_CRORES}cr")
    return reasons
