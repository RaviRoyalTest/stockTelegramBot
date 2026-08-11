"""Harmonic pattern definitions, ZigZag pivots and pattern-matching math."""
from __future__ import annotations

# Expected ratio ranges (b/c/d) with ideal values. b = |B-A|/|A-X|,
# c = |C-B|/|B-A|, d = |D-A|/|A-X| (XA base) or |D-C|/|C-X| (XC base for Shark).
PATTERNS = {
    "Gartley": {
        "b_ratio": (0.55, 0.68), "b_ideal": 0.618,
        "c_ratio": (0.30, 0.92), "c_ideal": 0.618,
        "d_ratio": (0.72, 0.82), "d_ideal": 0.786,
        "d_base_leg": "x_a_leg", "struct": "classic",
    },
    "Bat": {
        "b_ratio": (0.35, 0.53), "b_ideal": 0.443,
        "c_ratio": (0.30, 0.92), "c_ideal": 0.618,
        "d_ratio": (0.84, 0.92), "d_ideal": 0.886,
        "d_base_leg": "x_a_leg", "struct": "classic",
    },
    "Butterfly": {
        "b_ratio": (0.72, 0.84), "b_ideal": 0.786,
        "c_ratio": (0.30, 0.92), "c_ideal": 0.618,
        "d_ratio": (1.21, 1.68), "d_ideal": 1.272,
        "d_base_leg": "x_a_leg", "struct": "classic",
    },
    "Crab": {
        "b_ratio": (0.35, 0.66), "b_ideal": 0.5,
        "c_ratio": (0.30, 0.92), "c_ideal": 0.618,
        "d_ratio": (1.56, 2.66), "d_ideal": 1.618,
        "d_base_leg": "x_a_leg", "struct": "classic",
    },
    "Shark": {
        "b_ratio": (1.05, 1.62), "b_ideal": 1.13,
        "c_ratio": (1.52, 2.30), "c_ideal": 2.0,
        "d_ratio": (0.82, 1.18), "d_ideal": 1.0,
        "d_base_leg": "x_c_leg", "struct": "shark",
    },
}

# ZigZag deviation per timeframe (%). Higher on slower frames.
_ZIGZAG_DEVIATION_PERCENT = {"5m": 1.0, "15m": 1.5, "30m": 1.5, "1h": 2.0, "4h": 2.5,
               "1d": 3.0, "1w": 4.0}
_MINIMUM_SWING_FRACTION = 0.02  # minimum X->A swing as a fraction of price
_CONFIRMATION_BAND = 0.10  # confirmation band as a fraction of the AB leg


def sma(values, period):
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes, period=14):
    prices = [c_price for c_price in closes if c_price is not None]
    if len(prices) < period + 1:
        return None
    deltas = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
    gains = [delta if delta > 0 else 0.0 for delta in deltas]
    losses = [-delta if delta < 0 else 0.0 for delta in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + relative_strength)), 1)


def zigzag(highs, lows, percent=3.0):
    """Return alternating pivot points as (index, price, is_high) tuples."""
    pivots = []
    direction = 0
    last = None
    for index in range(len(highs)):
        high_price, low_price = highs[index], lows[index]
        if direction >= 0:
            if last is None or high_price > last[1]:
                last = (index, high_price, True)
            elif (last[1] - low_price) / last[1] * 100 >= percent:
                pivots.append(last)
                direction = -1
                last = (index, low_price, False)
        else:
            if last is None or low_price < last[1]:
                last = (index, low_price, False)
            elif (high_price - last[1]) / last[1] * 100 >= percent:
                pivots.append(last)
                direction = 1
                last = (index, high_price, True)
    if last is not None:
        pivots.append(last)
    return pivots


def in_range(value, ratio_range):
    return value is not None and ratio_range[0] <= value <= ratio_range[1]


def match_window(window, pattern_name, ref_price):
    """Check a 5-pivot window (X,A,B,C,D) against one pattern definition."""
    spec = PATTERNS[pattern_name]
    X, A, B, C, D = window
    bearish = X[2]  # X is a pivot high -> bearish reversal pattern

    x_price, a_price, b_price, c_price, d_price = X[1], A[1], B[1], C[1], D[1]
    x_a_distance = abs(a_price - x_price)
    a_b_distance = abs(b_price - a_price)
    b_c_distance = abs(c_price - b_price)
    if x_a_distance <= 0 or a_b_distance <= 0 or b_c_distance <= 0:
        return None
    if x_a_distance / ref_price < _MINIMUM_SWING_FRACTION:
        return None

    if spec["struct"] == "classic":
        # B must be a retracement between X and A; C between A and B.
        if bearish:
            if not (a_price < b_price < x_price) or not (a_price < c_price < b_price):
                return None
        else:
            if not (x_price < b_price < a_price) or not (b_price < c_price < a_price):
                return None
        if bearish and d_price > x_price * 1.02:
            return None  # D beyond X invalidates retracement patterns
        if not bearish and d_price < x_price * 0.98:
            return None
    else:  # Shark: B is an extension beyond X, C beyond A
        if bearish:
            if not (b_price > x_price) or not (c_price < a_price):
                return None
        else:
            if not (b_price < x_price) or not (c_price > a_price):
                return None

    b_ratio = a_b_distance / x_a_distance
    c_ratio = b_c_distance / a_b_distance
    if spec["d_base_leg"] == "x_a_leg":
        a_d_distance = abs(d_price - a_price)
        d_ratio = a_d_distance / x_a_distance
    else:
        x_c_distance = abs(c_price - x_price)
        if x_c_distance <= 0:
            return None
        d_ratio = abs(d_price - c_price) / x_c_distance

    if not in_range(b_ratio, spec["b_ratio"]):
        return None
    if not in_range(c_ratio, spec["c_ratio"]):
        return None
    if not in_range(d_ratio, spec["d_ratio"]):
        return None

    def _deviation(actual, ideal):
        return abs(actual - ideal) / ideal if ideal else 0.0

    score = (_deviation(b_ratio, spec["b_ideal"]) * 0.4
             + _deviation(c_ratio, spec["c_ideal"]) * 0.3
             + _deviation(d_ratio, spec["d_ideal"]) * 0.3)
    return {
        "pattern": pattern_name,
        "direction": "bearish" if bearish else "bullish",
        "points": {"X": x_price, "A": a_price, "B": b_price, "C": c_price, "D": d_price},
        "x_index": X[0], "d_index": D[0],
        "ratios": {"b_ratio": b_ratio, "c_ratio": c_ratio, "d_ratio": d_ratio},
        "x_a_distance": x_a_distance, "a_b_distance": a_b_distance, "b_c_distance": b_c_distance,
        "score": round(score, 3),
    }


def find_patterns(pivots, ref_price):
    """Scan all 5-pivot windows; return matched patterns sorted by recency."""
    found = []
    for index in range(len(pivots) - 4):
        window = pivots[index:index + 5]
        for pattern_name in PATTERNS:
            match = match_window(window, pattern_name, ref_price)
            if match:
                found.append(match)
    found.sort(key=lambda match: (match["d_index"], match["score"]))
    return found


def completion_d_price(match):
    """Computed D price from the ideal ratio (XA base or XC base)."""
    points = match["points"]
    spec = PATTERNS[match["pattern"]]
    bearish = match["direction"] == "bearish"
    x_a_distance = match["x_a_distance"]
    if spec["d_base_leg"] == "x_a_leg":
        if bearish:
            return points["A"] + spec["d_ideal"] * x_a_distance
        return points["A"] - spec["d_ideal"] * x_a_distance
    # Shark: D retraces the X->C move
    x_c_distance = abs(points["C"] - points["X"])
    if bearish:
        return points["C"] + spec["d_ideal"] * x_c_distance
    return points["C"] - spec["d_ideal"] * x_c_distance


def ab_cd_confluence_level(match):
    """AB=CD confluence level for the D leg."""
    points = match["points"]
    bearish = match["direction"] == "bearish"
    if bearish:
        return points["C"] + match["a_b_distance"]
    return points["C"] - match["a_b_distance"]


def build_plan(match, price, bar_count):
    """Entry / SL / targets / RR for the matched pattern."""
    bearish = match["direction"] == "bearish"
    points = match["points"]
    x_a_distance = match["x_a_distance"]
    a_b_distance = match["a_b_distance"]
    band = max(_CONFIRMATION_BAND * a_b_distance, 0.002 * price)

    d_level = points["D"]
    # Confirmation trigger level (wait for price to break back through it)
    if bearish:
        entry = d_level - band
        stop_loss = d_level + max(0.25 * a_b_distance, 0.004 * price)
        target_1 = d_level - 0.382 * x_a_distance
        target_2 = d_level - 0.618 * x_a_distance
        target_3 = points["A"]
    else:
        entry = d_level + band
        stop_loss = d_level - max(0.25 * a_b_distance, 0.004 * price)
        target_1 = d_level + 0.382 * x_a_distance
        target_2 = d_level + 0.618 * x_a_distance
        target_3 = points["A"]

    risk = abs(entry - stop_loss)
    reward_risk = {}
    if risk > 0:
        reward_risk["target_1"] = abs(target_1 - entry) / risk
        reward_risk["target_2"] = abs(target_2 - entry) / risk
        reward_risk["target_3"] = abs(target_3 - entry) / risk
    return {
        "entry": entry, "stop_loss": stop_loss,
        "targets": {"target_1": target_1, "target_2": target_2, "target_3": target_3},
        "reward_risk": reward_risk, "band": band,
        "d_index": match["d_index"], "bar_count": bar_count,
    }


def classify(match, plan, price, bars):
    """Return (status, signal) from price position vs the PRZ."""
    bearish = match["direction"] == "bearish"
    d_price = match["points"]["D"]
    band = plan["band"]
    bars_since_d = max(0, bars - 1 - plan["d_index"])
    if bearish:
        invalid = price > d_price + band
        at_zone = price >= d_price - band
        confirmed = price < d_price - band
    else:
        invalid = price < d_price - band
        at_zone = price <= d_price + band
        confirmed = price > d_price + band
    if invalid:
        return "Pattern invalidated", "NO TRADE"
    if confirmed:
        if bars_since_d <= 10:
            return "Reversal confirmed", ("SELL" if bearish else "BUY")
        return "Pattern completed", "NO TRADE"
    if at_zone:
        if bars_since_d <= 1:
            return "PRZ reached", "WAIT"
        return "Pattern completed", "WAIT"
    return "Pattern completed", "WAIT"
