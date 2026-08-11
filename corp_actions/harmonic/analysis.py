"""Harmonic pattern analysis: fetch OHLC, detect patterns, build the result dict."""
from __future__ import annotations

from ..sources import _HFT_LADDER, get_ohlc
from .patterns import (
    PATTERNS,
    _MIN_SWING,
    _ZIGZAG_PCT,
    abcd_level,
    build_plan,
    classify,
    completion_d,
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
            if not (in_range(br, spec["b"]) and in_range(cr, spec["c"])):
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
    ohlc = get_ohlc(exchange, symbol, timeframe)
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
        result["matches"] = [m["pattern"] for m in matches]
        result["pattern"] = match["pattern"]
        result["direction"] = match["direction"]
        result["points"] = match["points"]
        result["ratios"] = match["ratios"]
        result["plan"] = plan
        result["status"] = status
        result["signal"] = signal
        result["d_comp"] = completion_d(match)
        result["abcd"] = abcd_level(match)
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
            result["plan"] = build_plan(fake, price, len(closes))
            result["ratios"] = {"b": ab / xa,
                                "c": abs(pts["C"] - pts["B"]) / ab if ab > 0 else None}

    if result["status"] is None:
        result["status"] = "No harmonic pattern detected"
        result["signal"] = "NO TRADE"

    if not light:
        # Higher-timeframe direction (best effort, cached fetch)
        htf = _HFT_LADDER.get(tf)
        if htf:
            try:
                ho = get_ohlc(exchange, symbol, htf)
                if ho and len(ho["close"]) > 20:
                    hc = ho["close"]
                    h20 = sma(hc, 20)
                    if h20 is not None:
                        result["htf"] = f"{htf} trend: {'up' if hc[-1] > h20 else 'down'}"
            except Exception:
                pass

        # Confirmation notes
        notes = []
        rsi_val = result["rsi"]
        if rsi_val is not None:
            if rsi_val <= 30:
                notes.append(f"RSI {rsi_val:g} (oversold)")
            elif rsi_val <= 45:
                notes.append(f"RSI {rsi_val:g} (weak/bullish zone)")
            elif rsi_val >= 70:
                notes.append(f"RSI {rsi_val:g} (overbought)")
            elif rsi_val >= 60:
                notes.append(f"RSI {rsi_val:g} (strong zone)")
            else:
                notes.append(f"RSI {rsi_val:g} (neutral)")
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
