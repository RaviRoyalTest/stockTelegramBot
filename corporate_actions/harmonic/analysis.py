"""Harmonic pattern analysis: fetch OHLC, detect patterns, build the result dict."""
from __future__ import annotations

from ..core.numbers import format_money
from ..sources import _HIGHER_TIMEFRAME_LADDER, get_ohlc
from .patterns import (
    PATTERNS,
    _MINIMUM_SWING_FRACTION,
    _ZIGZAG_DEVIATION_PERCENT,
    ab_cd_confluence_level,
    build_plan,
    classify,
    completion_d_price,
    find_patterns,
    in_range,
    rsi,
    sma,
    zigzag,
)


def _find_forming(pivots, price, lookback=30):
    """Most recent X,A,B,C structure still approaching its projected D.

    Unlike find_patterns this does not require a confirmed D pivot — it
    reports structures whose projected PRZ has not been reached yet, so the
    report is honest about the pattern being incomplete (NO TRADE).
    """
    if len(pivots) < 4 or not price or price <= 0:
        return None
    last_index = pivots[-1][0]
    best = None
    for index in range(len(pivots) - 3):
        X, A, B, C = pivots[index:index + 4]
        if last_index - C[0] > lookback:
            continue
        bearish = X[2]
        x_price, a_price, b_price, c_price = X[1], A[1], B[1], C[1]
        x_a_distance, a_b_distance = abs(a_price - x_price), abs(b_price - a_price)
        if x_a_distance / price < _MINIMUM_SWING_FRACTION or a_b_distance <= 0:
            continue
        for pattern_name, spec in PATTERNS.items():
            if spec["struct"] != "classic":
                continue
            if bearish and not (a_price < b_price < x_price and a_price < c_price < b_price):
                continue
            if not bearish and not (x_price < b_price < a_price and b_price < c_price < a_price):
                continue
            b_ratio, c_ratio = a_b_distance / x_a_distance, abs(c_price - b_price) / a_b_distance
            if not (in_range(b_ratio, spec["b_ratio"]) and in_range(c_ratio, spec["c_ratio"])):
                continue
            if spec["d_base_leg"] == "x_a_leg":
                d_projection = a_price + spec["d_ideal"] * x_a_distance if bearish else a_price - spec["d_ideal"] * x_a_distance
            else:
                x_c_distance = abs(c_price - x_price)
                d_projection = c_price + spec["d_ideal"] * x_c_distance if bearish else c_price - spec["d_ideal"] * x_c_distance
            band = max(0.1 * a_b_distance, 0.004 * price)
            if bearish and price > d_projection + band:
                continue  # D already blown through
            if not bearish and price < d_projection - band:
                continue
            candidate = (f"{pattern_name} (forming)", "bearish" if bearish else "bullish",
                    {"X": x_price, "A": a_price, "B": b_price, "C": c_price, "D": None}, d_projection, x_a_distance, a_b_distance, C[0])
            if best is None or candidate[6] > best[6]:
                best = candidate
    if best is None:
        return None
    return best[:6]


def analyze(exchange: str, symbol: str, timeframe: str = "1d", percent: float | None = None,
            light: bool = False):
    """Fetch OHLC and build a full harmonic analysis result dict.

    Returns a dict with everything format_report() needs, or None when no
    price data is available. With light=True the higher-timeframe fetch and
    the confirmation notes are skipped, which roughly halves the network
    calls - used by the bulk index screener (/harmonic all|100|500) where
    only the pattern / direction / status / price are needed.
    """
    ohlc = get_ohlc(exchange, symbol, timeframe)
    if not ohlc:
        return None
    closes = ohlc["close"]
    price = closes[-1]
    timeframe = ohlc["timeframe"]
    zigzag_percent = _ZIGZAG_DEVIATION_PERCENT.get(timeframe, 3.0) if percent is None else percent
    pivots = zigzag(ohlc["high"], ohlc["low"], zigzag_percent)

    result = {
        "exchange": ohlc["exchange"], "symbol": ohlc["symbol"],
        "name": ohlc["name"], "timeframe": timeframe, "interval": ohlc["interval"],
        "price": price, "bars": len(closes),
        "rsi": rsi(closes), "sma20": sma(closes, 20), "sma50": sma(closes, 50),
        "volume": ohlc["volume"], "prev_close": closes[-2] if len(closes) > 1 else None,
        "pivots": pivots,
        "matches": [], "pattern": None, "plan": None, "status": None,
        "signal": None, "notes": [],
    }
    result["change_pct"] = (
        (price / result["prev_close"] - 1) * 100
        if result["prev_close"] else None
    )

    matches = find_patterns(pivots, price)
    if matches:
        match = matches[-1]  # most recent
        plan = build_plan(match, price, len(closes))
        status, signal = classify(match, plan, price, len(closes))
        result["matches"] = [match["pattern"] for match in matches]
        result["pattern"] = match["pattern"]
        result["direction"] = match["direction"]
        result["points"] = match["points"]
        result["ratios"] = match["ratios"]
        result["plan"] = plan
        result["status"] = status
        result["signal"] = signal
        result["d_completion_price"] = completion_d_price(match)
        result["ab_cd_confluence"] = ab_cd_confluence_level(match)
        # PRZ zone around the D completion + AB=CD confluence
        d_reference = match["points"]["D"]
        band = max(plan["band"], 0.004 * price)
        lower = min(d_reference, result["ab_cd_confluence"]) - band
        upper = max(d_reference, result["ab_cd_confluence"]) + band
        result["potential_reversal_zone"] = {"lower": lower, "upper": upper, "mid": (lower + upper) / 2,
                         "distance": price - (lower + upper) / 2}
    else:
        # Forming structure: a recent X,A,B,C whose D has not completed yet
        forming = _find_forming(pivots, price) if len(pivots) >= 4 else None
        if forming:
            pattern_name, direction, points, d_projection, x_a_distance, a_b_distance = forming
            result["pattern"] = pattern_name
            result["direction"] = direction
            result["points"] = points
            result["d_completion_price"] = d_projection
            band = max(0.1 * a_b_distance, 0.004 * price)
            lower = d_projection - band
            upper = d_projection + band
            result["potential_reversal_zone"] = {"lower": lower, "upper": upper, "mid": (lower + upper) / 2,
                             "distance": price - (lower + upper) / 2}
            if abs(price - d_projection) <= band:
                result["status"] = "D point/PRZ approaching"
            else:
                result["status"] = "Pattern forming"
            result["signal"] = "NO TRADE"
            fake = {"points": {**points, "D": d_projection}, "x_a_distance": x_a_distance, "a_b_distance": a_b_distance,
                    "d_index": 0, "direction": direction}
            result["plan"] = build_plan(fake, price, len(closes))
            result["ratios"] = {"b_ratio": a_b_distance / x_a_distance,
                                "c_ratio": abs(points["C"] - points["B"]) / a_b_distance if a_b_distance > 0 else None}

    if result["status"] is None:
        result["status"] = "No harmonic pattern detected"
        result["signal"] = "NO TRADE"

    if not light:
        # Higher-timeframe direction (best effort, cached fetch)
        higher_timeframe = _HIGHER_TIMEFRAME_LADDER.get(timeframe)
        if higher_timeframe:
            try:
                higher_ohlc = get_ohlc(exchange, symbol, higher_timeframe)
                if higher_ohlc and len(higher_ohlc["close"]) > 20:
                    higher_closes = higher_ohlc["close"]
                    higher_sma_20 = sma(higher_closes, 20)
                    if higher_sma_20 is not None:
                        result["higher_timeframe_note"] = f"{higher_timeframe} trend: {'up' if higher_closes[-1] > higher_sma_20 else 'down'}"
            except Exception:
                pass

        # Confirmation notes
        notes = []
        rsi_value = result["rsi"]
        if rsi_value is not None:
            if rsi_value <= 30:
                notes.append(f"RSI {rsi_value:g} (oversold)")
            elif rsi_value <= 45:
                notes.append(f"RSI {rsi_value:g} (weak/bullish zone)")
            elif rsi_value >= 70:
                notes.append(f"RSI {rsi_value:g} (overbought)")
            elif rsi_value >= 60:
                notes.append(f"RSI {rsi_value:g} (strong zone)")
            else:
                notes.append(f"RSI {rsi_value:g} (neutral)")
        if result.get("higher_timeframe_note"):
            notes.append("HTF: " + result["higher_timeframe_note"])
        sma20, sma50 = result["sma20"], result["sma50"]
        if sma20 is not None:
            notes.append("Price above SMA20 (short-term up)" if price > sma20
                         else "Price below SMA20 (short-term down)")
        if sma50 is not None:
            notes.append("Price above SMA50 (medium-term up)" if price > sma50
                         else "Price below SMA50 (medium-term down)")
        volumes = [volume_value for volume_value in ohlc["volume"] if volume_value]
        if len(volumes) >= 20:
            average_volume = sum(volumes[-20:]) / 20
            latest_volume = volumes[-1]
            if average_volume > 0:
                if latest_volume > average_volume * 1.25:
                    notes.append("Volume rising (last bar > 1.25× 20-bar avg)")
                elif latest_volume < average_volume * 0.75:
                    notes.append("Volume below average (weak conviction)")
                else:
                    notes.append("Volume in line with the 20-bar average")
        # nearest pivots as support / resistance
        currency = "USD" if ohlc.get("exchange", "").upper() == "US" else "INR"
        highs_near = [pivot[1] for pivot in pivots if pivot[2] and pivot[1] > price]
        lows_near = [pivot[1] for pivot in pivots if not pivot[2] and pivot[1] < price]
        if highs_near:
            notes.append(f"Resistance near: {format_money(min(highs_near), currency)}")
        if lows_near:
            notes.append(f"Support near: {format_money(max(lows_near), currency)}")
        result["notes"] = notes
    return result
