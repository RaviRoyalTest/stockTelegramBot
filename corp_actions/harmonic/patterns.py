"""Harmonic pattern definitions, ZigZag pivots and pattern-matching math."""
from __future__ import annotations

# Expected ratio ranges (b/c/d) with ideal values. b = |B-A|/|A-X|,
# c = |C-B|/|B-A|, d = |D-A|/|A-X| (XA base) or |D-C|/|C-X| (XC base for Shark).
PATTERNS = {
    "Gartley": {
        "b": (0.55, 0.68), "b_ideal": 0.618,
        "c": (0.30, 0.92), "c_ideal": 0.618,
        "d": (0.72, 0.82), "d_ideal": 0.786,
        "d_base": "xa", "struct": "classic",
    },
    "Bat": {
        "b": (0.35, 0.53), "b_ideal": 0.443,
        "c": (0.30, 0.92), "c_ideal": 0.618,
        "d": (0.84, 0.92), "d_ideal": 0.886,
        "d_base": "xa", "struct": "classic",
    },
    "Butterfly": {
        "b": (0.72, 0.84), "b_ideal": 0.786,
        "c": (0.30, 0.92), "c_ideal": 0.618,
        "d": (1.21, 1.68), "d_ideal": 1.272,
        "d_base": "xa", "struct": "classic",
    },
    "Crab": {
        "b": (0.35, 0.66), "b_ideal": 0.5,
        "c": (0.30, 0.92), "c_ideal": 0.618,
        "d": (1.56, 2.66), "d_ideal": 1.618,
        "d_base": "xa", "struct": "classic",
    },
    "Shark": {
        "b": (1.05, 1.62), "b_ideal": 1.13,
        "c": (1.52, 2.30), "c_ideal": 2.0,
        "d": (0.82, 1.18), "d_ideal": 1.0,
        "d_base": "xc", "struct": "shark",
    },
}

# ZigZag deviation per timeframe (%). Higher on slower frames.
_ZIGZAG_PCT = {"5m": 1.0, "15m": 1.5, "30m": 1.5, "1h": 2.0, "4h": 2.5,
               "1d": 3.0, "1w": 4.0}
_MIN_SWING = 0.02  # minimum X->A swing as a fraction of price
_CONF_BAND = 0.10  # confirmation band as a fraction of the AB leg


def sma(values, period):
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes, period=14):
    prices = [c for c in closes if c is not None]
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def zigzag(highs, lows, pct=3.0):
    """Return alternating pivot points as (index, price, is_high) tuples."""
    pivots = []
    direction = 0
    last = None
    for i in range(len(highs)):
        h, l = highs[i], lows[i]
        if direction >= 0:
            if last is None or h > last[1]:
                last = (i, h, True)
            elif (last[1] - l) / last[1] * 100 >= pct:
                pivots.append(last)
                direction = -1
                last = (i, l, False)
        else:
            if last is None or l < last[1]:
                last = (i, l, False)
            elif (h - last[1]) / last[1] * 100 >= pct:
                pivots.append(last)
                direction = 1
                last = (i, h, True)
    if last is not None:
        pivots.append(last)
    return pivots


def in_range(value, rng):
    return value is not None and rng[0] <= value <= rng[1]


def match_window(win, pname, ref_price):
    """Check a 5-pivot window (X,A,B,C,D) against one pattern definition."""
    spec = PATTERNS[pname]
    X, A, B, C, D = win
    bearish = X[2]  # X is a pivot high -> bearish reversal pattern

    x, a, b, c, d = X[1], A[1], B[1], C[1], D[1]
    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    if xa <= 0 or ab <= 0 or bc <= 0:
        return None
    if xa / ref_price < _MIN_SWING:
        return None

    if spec["struct"] == "classic":
        # B must be a retracement between X and A; C between A and B.
        if bearish:
            if not (a < b < x) or not (a < c < b):
                return None
        else:
            if not (x < b < a) or not (b < c < a):
                return None
        if bearish and d > x * 1.02:
            return None  # D beyond X invalidates retracement patterns
        if not bearish and d < x * 0.98:
            return None
    else:  # Shark: B is an extension beyond X, C beyond A
        if bearish:
            if not (b > x) or not (c < a):
                return None
        else:
            if not (b < x) or not (c > a):
                return None

    b_ratio = ab / xa
    c_ratio = bc / ab
    if spec["d_base"] == "xa":
        ad = abs(d - a)
        d_ratio = ad / xa
    else:
        xc = abs(c - x)
        if xc <= 0:
            return None
        d_ratio = abs(d - c) / xc

    if not in_range(b_ratio, spec["b"]):
        return None
    if not in_range(c_ratio, spec["c"]):
        return None
    if not in_range(d_ratio, spec["d"]):
        return None

    def _dev(actual, ideal):
        return abs(actual - ideal) / ideal if ideal else 0.0

    score = (_dev(b_ratio, spec["b_ideal"]) * 0.4
             + _dev(c_ratio, spec["c_ideal"]) * 0.3
             + _dev(d_ratio, spec["d_ideal"]) * 0.3)
    return {
        "pattern": pname,
        "direction": "bearish" if bearish else "bullish",
        "points": {"X": x, "A": a, "B": b, "C": c, "D": d},
        "x_idx": X[0], "d_idx": D[0],
        "ratios": {"b": b_ratio, "c": c_ratio, "d": d_ratio},
        "xa": xa, "ab": ab, "bc": bc,
        "score": round(score, 3),
    }


def find_patterns(pivots, ref_price):
    """Scan all 5-pivot windows; return matched patterns sorted by recency."""
    found = []
    for i in range(len(pivots) - 4):
        win = pivots[i:i + 5]
        for pname in PATTERNS:
            m = match_window(win, pname, ref_price)
            if m:
                found.append(m)
    found.sort(key=lambda m: (m["d_idx"], m["score"]))
    return found


def completion_d(match):
    """Computed D price from the ideal ratio (XA base or XC base)."""
    pts = match["points"]
    spec = PATTERNS[match["pattern"]]
    bearish = match["direction"] == "bearish"
    xa = match["xa"]
    if spec["d_base"] == "xa":
        if bearish:
            return pts["A"] + spec["d_ideal"] * xa
        return pts["A"] - spec["d_ideal"] * xa
    # Shark: D retraces the X->C move
    xc = abs(pts["C"] - pts["X"])
    if bearish:
        return pts["C"] + spec["d_ideal"] * xc
    return pts["C"] - spec["d_ideal"] * xc


def abcd_level(match):
    """AB=CD confluence level for the D leg."""
    pts = match["points"]
    bearish = match["direction"] == "bearish"
    if bearish:
        return pts["C"] + match["ab"]
    return pts["C"] - match["ab"]


def build_plan(match, price, bar_count):
    """Entry / SL / targets / RR for the matched pattern."""
    bearish = match["direction"] == "bearish"
    pts = match["points"]
    xa = match["xa"]
    ab = match["ab"]
    band = max(_CONF_BAND * ab, 0.002 * price)

    d_level = pts["D"]
    # Confirmation trigger level (wait for price to break back through it)
    if bearish:
        entry = d_level - band
        sl = d_level + max(0.25 * ab, 0.004 * price)
        t1 = d_level - 0.382 * xa
        t2 = d_level - 0.618 * xa
        t3 = pts["A"]
    else:
        entry = d_level + band
        sl = d_level - max(0.25 * ab, 0.004 * price)
        t1 = d_level + 0.382 * xa
        t2 = d_level + 0.618 * xa
        t3 = pts["A"]

    risk = abs(entry - sl)
    rr = {}
    if risk > 0:
        rr["t1"] = abs(t1 - entry) / risk
        rr["t2"] = abs(t2 - entry) / risk
        rr["t3"] = abs(t3 - entry) / risk
    return {
        "entry": entry, "sl": sl,
        "targets": {"t1": t1, "t2": t2, "t3": t3},
        "rr": rr, "band": band,
        "d_idx": match["d_idx"], "bar_count": bar_count,
    }


def classify(match, plan, price, bars):
    """Return (status, signal) from price position vs the PRZ."""
    bearish = match["direction"] == "bearish"
    d = match["points"]["D"]
    band = plan["band"]
    bars_since_d = max(0, bars - 1 - plan["d_idx"])
    if bearish:
        invalid = price > d + band
        at_zone = price >= d - band
        confirmed = price < d - band
    else:
        invalid = price < d - band
        at_zone = price <= d + band
        confirmed = price > d + band
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
