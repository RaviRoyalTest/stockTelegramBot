"""Harmonic chart-pattern scanner (Gartley, Bat, Butterfly, Crab, Shark).

Detects classic harmonic reversal setups from OHLC bars, computes the
Potential Reversal Zone (PRZ) and builds a structured trade plan modelled on
TradingView's "MN - Auto Harmonic Patterns and PRZ Alert".

This is a best-effort mathematical scanner, NOT a trading recommendation.
A detected pattern is a possibility, not an automatic entry: entries always
wait for confirmation from the PRZ.
"""

from . import notifier, sources


# ---- pattern definitions --------------------------------------------------
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


# ---- technical helpers ----------------------------------------------------
def _sma(values, period):
    if not values or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes, period=14):
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


# ---- ZigZag pivots --------------------------------------------------------
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


# ---- pattern matching -----------------------------------------------------
def _in_range(value, rng):
    return value is not None and rng[0] <= value <= rng[1]


def _match_window(win, pname, ref_price):
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

    if not _in_range(b_ratio, spec["b"]):
        return None
    if not _in_range(c_ratio, spec["c"]):
        return None
    if not _in_range(d_ratio, spec["d"]):
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


def _find_patterns(pivots, ref_price):
    """Scan all 5-pivot windows; return matched patterns sorted by recency."""
    found = []
    for i in range(len(pivots) - 4):
        win = pivots[i:i + 5]
        for pname in PATTERNS:
            m = _match_window(win, pname, ref_price)
            if m:
                found.append(m)
    found.sort(key=lambda m: (m["d_idx"], m["score"]))
    return found


# ---- PRZ / status / plan --------------------------------------------------
def _completion_d(match):
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


def _abcd_level(match):
    """AB=CD confluence level for the D leg."""
    pts = match["points"]
    bearish = match["direction"] == "bearish"
    if bearish:
        return pts["C"] + match["ab"]
    return pts["C"] - match["ab"]


def _build_plan(match, price, bar_count):
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


def _classify(match, plan, price, bars):
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


# ---- public analysis ------------------------------------------------------
def _find_forming(pivots, price, lookback=30):
    """Most recent X,A,B,C structure still approaching its projected D.

    Unlike _find_patterns this does not require a confirmed D pivot — it
    reports structures whose projected PRZ has not been reached yet, so the
    report is honest about the pattern being incomplete (NO TRADE).
    """
    if len(pivots) < 4:
        return None
    last_idx = pivots[-1][0]
    best = None
    for i in range(len(pivots) - 3):
        X, A, B, C = pivots[i:i + 4]
        if last_idx - C[0] > lookback:
            continue
        bearish = X[2]
        x, a, b, c = X[1], A[1], B[1], C[1]
        xa, ab = abs(a - x), abs(b - a)
        if xa / price < _MIN_SWING or ab <= 0:
            continue
        for pname, spec in PATTERNS.items():
            if spec["struct"] != "classic":
                continue
            if bearish and not (a < b < x and a < c < b):
                continue
            if not bearish and not (x < b < a and b < c < a):
                continue
            br, cr = ab / xa, abs(c - b) / ab
            if not (_in_range(br, spec["b"]) and _in_range(cr, spec["c"])):
                continue
            if spec["d_base"] == "xa":
                d_proj = a + spec["d_ideal"] * xa if bearish else a - spec["d_ideal"] * xa
            else:
                xc = abs(c - x)
                d_proj = c + spec["d_ideal"] * xc if bearish else c - spec["d_ideal"] * xc
            band = max(0.1 * ab, 0.004 * price)
            if bearish and price > d_proj + band:
                continue  # D already blown through
            if not bearish and price < d_proj - band:
                continue
            cand = (f"{pname} (forming)", "bearish" if bearish else "bullish",
                    {"X": x, "A": a, "B": b, "C": c, "D": None}, d_proj, xa, ab, C[0])
            if best is None or cand[6] > best[6]:
                best = cand
    if best is None:
        return None
    return best[:6]


def analyze(exchange: str, symbol: str, timeframe: str = "1d", pct: float | None = None,
            light: bool = False):
    """Fetch OHLC and build a full harmonic analysis result dict.

    Returns a dict with everything format_report() needs, or None when no
    price data is available. With light=True the higher-timeframe fetch and
    the confirmation notes are skipped, which roughly halves the network
    calls - used by the bulk index screener (/harmonic all|100|500) where
    only the pattern / direction / status / price are needed.
    """
    ohlc = sources.get_ohlc(exchange, symbol, timeframe)
    if not ohlc:
        return None
    closes = ohlc["close"]
    price = closes[-1]
    tf = ohlc["timeframe"]
    zz = _ZIGZAG_PCT.get(tf, 3.0) if pct is None else pct
    pivots = zigzag(ohlc["high"], ohlc["low"], zz)

    result = {
        "exchange": ohlc["exchange"], "symbol": ohlc["symbol"],
        "name": ohlc["name"], "timeframe": tf, "interval": ohlc["interval"],
        "price": price, "bars": len(closes),
        "rsi": _rsi(closes), "sma20": _sma(closes, 20), "sma50": _sma(closes, 50),
        "volume": ohlc["volume"], "prev_close": closes[-2] if len(closes) > 1 else None,
        "pivots": pivots,
        "matches": [], "pattern": None, "plan": None, "status": None,
        "signal": None, "notes": [],
    }

    matches = _find_patterns(pivots, price)
    if matches:
        match = matches[-1]  # most recent
        plan = _build_plan(match, price, len(closes))
        status, signal = _classify(match, plan, price, len(closes))
        result["matches"] = [m["pattern"] for m in matches]
        result["pattern"] = match["pattern"]
        result["direction"] = match["direction"]
        result["points"] = match["points"]
        result["ratios"] = match["ratios"]
        result["plan"] = plan
        result["status"] = status
        result["signal"] = signal
        result["d_comp"] = _completion_d(match)
        result["abcd"] = _abcd_level(match)
        # PRZ zone around the D completion + AB=CD confluence
        d_ref = match["points"]["D"]
        band = max(plan["band"], 0.004 * price)
        lo = min(d_ref, result["abcd"]) - band
        hi = max(d_ref, result["abcd"]) + band
        result["prz"] = {"lower": lo, "upper": hi, "mid": (lo + hi) / 2,
                         "dist": price - (lo + hi) / 2}
    else:
        # Forming structure: a recent X,A,B,C whose D has not completed yet
        forming = _find_forming(pivots, price) if len(pivots) >= 4 else None
        if forming:
            pname, direction, pts, d_proj, xa, ab = forming
            result["pattern"] = pname
            result["direction"] = direction
            result["points"] = pts
            result["d_comp"] = d_proj
            band = max(0.1 * ab, 0.004 * price)
            lo = d_proj - band
            hi = d_proj + band
            result["prz"] = {"lower": lo, "upper": hi, "mid": (lo + hi) / 2,
                             "dist": price - (lo + hi) / 2}
            if abs(price - d_proj) <= band:
                result["status"] = "D point/PRZ approaching"
            else:
                result["status"] = "Pattern forming"
            result["signal"] = "NO TRADE"
            fake = {"points": {**pts, "D": d_proj}, "xa": xa, "ab": ab,
                    "d_idx": 0, "direction": direction}
            result["plan"] = _build_plan(fake, price, len(closes))
            result["ratios"] = {"b": ab / xa,
                                "c": abs(pts["C"] - pts["B"]) / ab if ab > 0 else None}

    if result["status"] is None:
        result["status"] = "No harmonic pattern detected"
        result["signal"] = "NO TRADE"

    if not light:
        # Higher-timeframe direction (best effort, cached fetch)
        htf = sources._HFT_LADDER.get(tf)
        if htf:
            try:
                ho = sources.get_ohlc(exchange, symbol, htf)
                if ho and len(ho["close"]) > 20:
                    hc = ho["close"]
                    h20 = _sma(hc, 20)
                    if h20 is not None:
                        result["htf"] = f"{htf} trend: {'up' if hc[-1] > h20 else 'down'}"
            except Exception:
                pass

        # Confirmation notes
        notes = []
        rsi = result["rsi"]
        if rsi is not None:
            if rsi <= 30:
                notes.append(f"RSI {rsi:g} (oversold)")
            elif rsi <= 45:
                notes.append(f"RSI {rsi:g} (weak/bullish zone)")
            elif rsi >= 70:
                notes.append(f"RSI {rsi:g} (overbought)")
            elif rsi >= 60:
                notes.append(f"RSI {rsi:g} (strong zone)")
            else:
                notes.append(f"RSI {rsi:g} (neutral)")
        if result.get("htf"):
            notes.append("HTF: " + result["htf"])
        sma20, sma50 = result["sma20"], result["sma50"]
        if sma20 is not None:
            notes.append("Price above SMA20 (short-term up)" if price > sma20
                         else "Price below SMA20 (short-term down)")
        if sma50 is not None:
            notes.append("Price above SMA50 (medium-term up)" if price > sma50
                         else "Price below SMA50 (medium-term down)")
        vols = [v for v in ohlc["volume"] if v]
        if len(vols) >= 20:
            avg_v = sum(vols[-20:]) / 20
            last_v = vols[-1]
            if avg_v > 0:
                if last_v > avg_v * 1.25:
                    notes.append("Volume rising (last bar > 1.25× 20-bar avg)")
                elif last_v < avg_v * 0.75:
                    notes.append("Volume below average (weak conviction)")
                else:
                    notes.append("Volume in line with the 20-bar average")
        # nearest pivots as support / resistance
        highs_near = [p[1] for p in pivots if p[2] and p[1] > price]
        lows_near = [p[1] for p in pivots if not p[2] and p[1] < price]
        if highs_near:
            notes.append(f"Resistance near: ₹{min(highs_near):,.2f}")
        if lows_near:
            notes.append(f"Support near: ₹{max(lows_near):,.2f}")
        result["notes"] = notes
    return result


# ---- report formatting ----------------------------------------------------
def format_report(r) -> list[str]:
    """Render the harmonic analysis as HTML lines for Telegram."""
    sym = f"{r['symbol']}.NS" if r["exchange"] == "NSE" else f"{r['symbol']}.BO"
    lines = []
    lines.append(
        f"\U0001F3C6 <b>HARMONIC PATTERN SCAN</b>\n"
        f"\U0001F4CA {notifier.escape(r['name'].upper())} ({notifier.escape(sym)})"
    )
    lines.append(f"\U0001F5D3 Timeframe: <b>{r['timeframe'].upper()}</b>  \u00b7  {r['bars']} bars  \u00b7  RSI {r['rsi']:g}" if r["rsi"] is not None else
                 f"\U0001F5D3 Timeframe: <b>{r['timeframe'].upper()}</b>  \u00b7  {r['bars']} bars")
    lines.append(f"Last Price: <b>{notifier.fmt_money(r['price'])}</b>")
    lines.append("")

    # 4. Pattern
    lines.append("<b>\U0001F3AF HARMONIC PATTERN</b>")
    lines.append(f"<b>{notifier.escape(r['pattern'])}</b>" if r["pattern"] else
                 "<i>No complete harmonic pattern detected</i>")
    if r.get("matches"):
        lines.append("Detected: " + ", ".join(notifier.escape(m) for m in r["matches"]))
    lines.append("")

    # 5. Pattern points
    lines.append("<b>\U0001F4CB PATTERN POINTS</b>")
    pts = r.get("points")
    if pts:
        for label in ("X", "A", "B", "C"):
            v = pts.get(label)
            if v is not None:
                lines.append(f"  {label}: {notifier.fmt_money(v)}")
        d = pts.get("D")
        if d is not None:
            lines.append(f"  D: <b>{notifier.fmt_money(d)}</b> (completion)")
        else:
            lines.append(f"  D: <b>{notifier.fmt_money(r['d_comp'])}</b> (projected)")
    lines.append("")

    # 6. Fibonacci ratios
    lines.append("<b>\U0001F522 FIBONACCI RATIOS</b>")
    spec = PATTERNS.get(str(r["pattern"]).split(" ")[0]) if r["pattern"] else None
    ratios = r.get("ratios")
    if ratios and spec:
        lines.append(f"  B retr. of XA: <b>{ratios['b']:.2f}</b>  (ideal {spec['b_ideal']:.2f})")
        lines.append(f"  C retr. of AB: <b>{ratios['c']:.2f}</b>  (range {spec['c'][0]:.2f}\u2013{spec['c'][1]:.2f})")
        dbase = "of XA" if spec["d_base"] == "xa" else "of XC"
        if ratios.get("d") is not None:
            lines.append(f"  D {dbase}: <b>{ratios['d']:.2f}</b>  (ideal {spec['d_ideal']:.2f})")
        else:
            lines.append(f"  D {dbase}: <b>{spec['d_ideal']:.2f}</b> (projected until D completes)")
    elif r.get("points"):
        lines.append("  Projected D at ideal Gartley 0.786 retracement.")
    else:
        lines.append("  No ratios to report.")
    lines.append("")

    # 7. PRZ
    prz = r.get("prz")
    if prz:
        lines.append("<b>\U0001F6E1 PRZ (Potential Reversal Zone)</b>")
        lines.append(f"  Upper: <b>{notifier.fmt_money(prz['upper'])}</b>")
        lines.append(f"  Lower: <b>{notifier.fmt_money(prz['lower'])}</b>")
        lines.append(f"  Midpoint: <b>{notifier.fmt_money(prz['mid'])}</b>")
        dist = prz["dist"]
        tag = "inside PRZ" if prz["lower"] <= r["price"] <= prz["upper"] else \
            ("above PRZ" if dist > 0 else "below PRZ")
        lines.append(f"  Current price: {tag} ({dist:+,.2f} vs midpoint)")
    lines.append("")

    # 8. Status
    lines.append("<b>\U0001F504 PATTERN STATUS</b>")
    lines.append(f"<b>{notifier.escape(r['status'])}</b>")
    if "forming" in (r.get("status") or "").lower() or "approaching" in (r.get("status") or "").lower():
        lines.append("  <i>D has not completed \u2014 no entry is valid until price reaches the PRZ "
                     "and confirms a reversal.</i>")
    lines.append("")

    # 9-13. Trade plan (only when the setup is still actionable)
    plan = r.get("plan")
    direction = r.get("direction")
    status = r.get("status") or ""
    stale = status in ("Pattern completed", "Pattern invalidated") and plan
    near_prz = False
    prz = r.get("prz")
    if prz:
        near_prz = abs(r["price"] - prz["mid"]) <= 0.10 * r["price"]
    if plan and direction and not (stale and not near_prz):
        dirlbl = "Bullish reversal setup" if direction == "bullish" else "Bearish reversal setup"
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        if stale:
            lines.append("  <i>Setup already played out \u2014 levels shown for reference only.</i>")
        lines.append(f"  Direction: <b>{dirlbl}</b>")
        lines.append(f"  Entry (on confirmation): <b>{notifier.fmt_money(plan['entry'])}</b>")
        lines.append(f"  Stop Loss: <b>{notifier.fmt_money(plan['sl'])}</b>")
        lines.append("  Targets:")
        t = plan["targets"]
        rr = plan["rr"]
        for name, key in (("T1", "t1"), ("T2", "t2"), ("T3", "t3")):
            rr_str = f"  (R:R ≈ {rr[key]:.1f}:1)" if key in rr and rr[key] else ""
            lines.append(f"    {name}: <b>{notifier.fmt_money(t[key])}</b>{rr_str}")
        lines.append("  <i>Do NOT enter just because price touches the PRZ \u2014 wait for a "
                     "rejection/reversal candle, a break of the confirmation level, or volume.")
    elif plan and direction:
        lines.append("<b>\U0001F4C8 TRADE PLAN</b>")
        lines.append("  <i>Setup is no longer actionable \u2014 price has moved well past the PRZ. "
                     "Watch for a fresh XA swing before considering this pattern.</i>")
    lines.append("")

    # 14. Confirmation
    lines.append("<b>\U0001F50D CONFIRMATION</b>")
    if r["notes"]:
        lines.extend("  \u2022 " + n for n in r["notes"])
    else:
        lines.append("  No confirmation data available.")

    # 15. Final signal
    sig = r["signal"]
    emoji = {"BUY": "\U0001F7E2", "SELL": "\U0001F534",
             "WAIT": "\U0001F7E1", "NO TRADE": "\u26AA"}.get(sig, "\u26AA")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"{emoji} <b>FINAL SIGNAL: {sig}</b>")
    lines.append("")
    lines.append(f"\U0001F4A1 <i>Tip: {notifier.escape(r['symbol'])} on the {r['timeframe']} chart. "
                 "A PRZ is a potential area, not a guaranteed reversal.</i>")
    return lines


# ---- bulk screener -------------------------------------------------------
# The index-wide scan (/harmonic all|100|500) renders one compact line per
# stock instead of the full report, so the whole universe fits in a few
# Telegram messages.

# Most actionable first. Statuses come from analyze()/_classify/_find_forming.
SCAN_PRIORITY = {
    "Reversal confirmed": 0,       # signal BUY/SELL - price has confirmed
    "PRZ reached": 1,              # signal WAIT - price is inside the zone
    "D point/PRZ approaching": 2,  # forming, D near its projection
    "Pattern forming": 3,          # forming, D still projected ahead
    "Pattern completed": 4,        # played out - levels for reference only
}

# Rows shown per scan. Keeps the "smaller version" readable even on NIFTY 500.
SCAN_MAX_ROWS = 25


def format_scan_row(r) -> str:
    """One compact line per stock for the bulk harmonic screener.

    e.g. "▲ TCS ₹4,000.00 — Gartley (bullish) — Reversal confirmed · SELL"
    """
    arrow = "\u25b2" if r.get("direction") == "bullish" else "\u25bc"
    pattern = notifier.escape(r.get("pattern") or "?")
    status = notifier.escape(r.get("status") or "")
    return (
        f"{arrow} <b>{notifier.escape(r['symbol'])}</b> "
        f"{notifier.fmt_money(r['price'])} \u2014 <b>{pattern}</b> "
        f"({r.get('direction') or '?'}) \u2014 {status} \u00b7 {r.get('signal') or 'NO TRADE'}"
    )
