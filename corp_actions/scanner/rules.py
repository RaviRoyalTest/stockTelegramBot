"""Strict 'do not buy / do not show' rejection rules for the scanner."""

REJ_MIN_DELIVERY = 40.0      # Rule 2 (proxy: CMF/MFI accumulation)
REJ_MIN_CMF = 0.00           # Rule 3
REJ_MIN_MRS = 0.00           # Rule 4
REJ_MIN_RR_T2 = 2.0          # Rule 5 risk:reward to Target 2
REJ_MAX_SL_PCT = 8.0         # Rule 5 max stop loss
REJ_MIN_ADTV_CR = 10.0       # Rule 7 average daily traded value (₹ crore)


def rejection_reasons(f: dict) -> list[str]:
    """Return the list of rejected rules for a scanned stock (empty = pass)."""
    reasons = []
    if f.get("wk_supertrend") == "red":
        reasons.append("Weekly Supertrend RED")
    if f.get("above_ema200") is False:
        reasons.append("Price below 200 SMA")
    if f.get("delivery_est") is not None and f["delivery_est"] < REJ_MIN_DELIVERY:
        reasons.append(f"Delivery est. {f['delivery_est']:.0f}% < {REJ_MIN_DELIVERY:.0f}%")
    if f.get("cmf20") is not None and f["cmf20"] < REJ_MIN_CMF:
        reasons.append(f"CMF {f['cmf20']:+.2f} < 0")
    if f.get("mrs") is not None and f["mrs"] < REJ_MIN_MRS:
        reasons.append(f"MRS {f['mrs']:+.1f} < 0")
    if f.get("rr_t2") is not None and f["rr_t2"] < REJ_MIN_RR_T2:
        reasons.append(f"R:R to T2 {f['rr_t2']:.1f} < 2.0")
    if f.get("sl_pct") is not None and f["sl_pct"] > REJ_MAX_SL_PCT:
        reasons.append(f"Stop {f['sl_pct']:.1f}% > {REJ_MAX_SL_PCT}%")
    if f.get("adtv_cr") is not None and f["adtv_cr"] < REJ_MIN_ADTV_CR:
        reasons.append(f"ADTV ₹{f['adtv_cr']:.1f}cr < ₹{REJ_MIN_ADTV_CR}cr")
    return reasons
