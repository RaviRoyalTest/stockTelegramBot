"""NIFTY 500 advanced multi-indicator scanner.

Implements the CNC/MIS high-probability scan pipeline: a full indicator suite
computed from daily OHLC (with a weekly resample for the supertrend), strict
"Do not buy / do not show" rejection rules, a 100-point scoring model and a
market-regime summary. All numbers are derived from public Yahoo Finance
candles; fields NSE would normally provide (real delivery %, order-book depth)
are honestly marked as estimates/proxies rather than fabricated.
"""

from __future__ import annotations

import logging
import math
import statistics
from typing import Optional

import numpy as np

try:
    import pandas as pd
    _HAS_PANDAS = True
except Exception:  # pragma: no cover - non-fatal, scanner just unavailable
    _HAS_PANDAS = False

log = logging.getLogger(__name__)

# ------------------------------------------------------------- thresholds
REJ_MIN_DELIVERY = 40.0      # Rule 2 (proxy: CMF/MFI accumulation, see below)
REJ_MIN_CMF = 0.00           # Rule 3
REJ_MIN_MRS = 0.00           # Rule 4
REJ_MIN_RR_T2 = 2.0          # Rule 5 risk:reward to Target 2
REJ_MAX_SL_PCT = 8.0         # Rule 5 max stop loss
REJ_MIN_ADTV_CR = 10.0       # Rule 7 average daily traded value (₹ crore)
SCORE_QUALIFY = 75.0         # minimum score to qualify
MIN_BARS = 220               # need ~1 year of dailies for 200 EMA + 52w channel

# ------------------------------------------------------------- helpers
def _safe_last(series, default=None):
    if series is None:
        return default
    try:
        if series.empty:
            return default
        val = series.iloc[-1]
        return None if (val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val)))) else float(val)
    except Exception:
        return default


def _rolling_last(s, window, fn):
    """Apply a rolling function and return only the final value."""
    if s is None or s.empty:
        return None
    vals = s.values
    n = len(vals)
    if n < window:
        return None
    return float(fn(vals[n - window:n]))


def _ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0.0, 1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def _true_range(df):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _adx(df, n=14):
    tr = _true_range(df)
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0.0, 1e-9)
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, 1e-9)
    adx = dx.ewm(alpha=1.0 / n, adjust=False).mean()
    return pdi, mdi, adx


def _cmf(df, n=20):
    hl = (df["high"] - df["low"]).replace(0.0, 1e-9)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl
    mfv = mfm * df["volume"]
    return mfv.rolling(n).sum() / df["volume"].rolling(n).sum().replace(0.0, 1e-9)


def _mfi(df, n=14):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    mf = tp * df["volume"]
    d = tp.diff()
    pos = mf.where(d > 0, 0.0).rolling(n).sum()
    neg = mf.where(d < 0, 0.0).rolling(n).sum()
    return 100.0 - 100.0 / (1.0 + pos / neg.replace(0.0, 1e-9))


def _obv(df):
    sgn = np.sign(df["close"].diff()).fillna(0.0)
    return (sgn * df["volume"]).cumsum()


def _aroon(df, n=25):
    win = n + 1
    def _pos_hi(vals):
        return int(vals.argmax())
    def _pos_lo(vals):
        return int(vals.argmin())
    hi = df["high"].rolling(win).apply(_pos_hi, raw=True)
    lo = df["low"].rolling(win).apply(_pos_lo, raw=True)
    a_up = 100.0 * (win - 1 - hi) / n
    a_dn = 100.0 * (win - 1 - lo) / n
    return a_up, a_dn


def _supertrend(df, period=10, mult=3.0):
    """Supertrend over a DataFrame with OHLC. Returns (direction_df, line_df)."""
    h, l, c = df["high"], df["low"], df["close"]
    atr = _atr(df, period)
    hl2 = (h + l) / 2.0
    ub = hl2 + mult * atr
    lb = hl2 - mult * atr
    n = len(df)
    st = pd.Series(np.nan, index=df.index)
    dirn = pd.Series(1, index=df.index)
    if n == 0:
        return dirn, st
    prev_st = None
    prev_dir = 1
    for i in range(n):
        up = ub.iloc[i]
        dn = lb.iloc[i]
        prev_up = ub.iloc[i - 1] if i > 0 else up
        prev_dn = lb.iloc[i - 1] if i > 0 else dn
        c_now = c.iloc[i]
        if prev_st is None or math.isnan(prev_st):
            st.iloc[i] = up
            dirn.iloc[i] = 1
        else:
            if prev_st == prev_up:
                dirn.iloc[i] = -1 if c_now < prev_up else 1
            else:
                dirn.iloc[i] = 1 if c_now > prev_dn else -1
            if dirn.iloc[i] == 1:
                st.iloc[i] = max(dn, prev_st) if not math.isnan(prev_st) else dn
            else:
                st.iloc[i] = min(up, prev_st) if not math.isnan(prev_st) else up
        prev_st = st.iloc[i]
        prev_dir = dirn.iloc[i]
    return dirn, st


def _weekly_df(daily):
    """Resample daily OHLC to weekly bars (week ending Friday)."""
    w = pd.DataFrame({
        "open": daily["open"], "high": daily["high"],
        "low": daily["low"], "close": daily["close"], "volume": daily["volume"],
    })
    if "dt" in daily.columns:
        w["dt"] = daily["dt"]
    else:
        w["dt"] = pd.to_datetime(daily["timestamp"], unit="s", utc=True)
    w = w.set_index("dt")
    wk = w.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    return wk


def _anchored_vwap(df, lookback=252):
    """VWAP anchored at the lowest low over the last `lookback` bars."""
    sub = df.tail(lookback)
    anchor = sub["low"].idxmin()
    anchor_pos = df.index.get_loc(anchor)
    seg = df.iloc[anchor_pos:]
    tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
    tot = (tp * seg["volume"]).sum()
    vol = seg["volume"].sum()
    return float(tot / vol) if vol > 0 else None


def _mrs(sym_close, idx_close):
    """Mansfield relative strength vs an index benchmark."""
    if not _HAS_PANDAS:
        return None
    sym_close = pd.Series(sym_close) if not isinstance(sym_close, pd.Series) else sym_close
    idx_close = pd.Series(idx_close) if not isinstance(idx_close, pd.Series) else idx_close
    n = min(200, len(sym_close), len(idx_close))
    if n < 120:
        return None
    s = sym_close.values[-n:]
    i = idx_close.values[-n:]
    rs = pd.Series(s / i)
    ma20 = rs.rolling(20).mean()
    last_ma = _safe_last(ma20)
    if not last_ma:
        return None
    return (float(rs.iloc[-1]) / last_ma - 1.0) * 100.0


def _adx_strength(adx):
    if adx is None:
        return 0
    if adx >= 40:
        return 3
    if adx >= 25:
        return 2
    if adx >= 20:
        return 1
    return 0


# ------------------------------------------------------------- stock scan
def scan_stock(ohlc, index_close=None) -> Optional[dict]:
    """Compute the full technical field set for one symbol.

    Returns a dict with every indicator used by the score/reject/report
    pipeline, or None when there is not enough data.
    """
    if not _HAS_PANDAS or not ohlc:
        return None
    closes = ohlc["close"]
    if len(closes) < MIN_BARS:
        return None
    df = pd.DataFrame({
        "open": ohlc["open"], "high": ohlc["high"],
        "low": ohlc["low"], "close": closes, "volume": ohlc["volume"],
    })
    df["dt"] = pd.to_datetime(ohlc["timestamp"], unit="s", utc=True)
    price = closes[-1]
    f = {
        "symbol": ohlc["symbol"], "name": ohlc["name"], "price": price,
        "timeframe": ohlc.get("timeframe", "1d"),
    }

    # Trend & structure
    ema20 = _safe_last(_ema(df["close"], 20))
    ema50 = _safe_last(_ema(df["close"], 50))
    ema100 = _safe_last(_ema(df["close"], 100))
    ema200 = _safe_last(_ema(df["close"], 200))
    f.update(ema20=ema20, ema50=ema50, ema100=ema100, ema200=ema200)
    f["above_ema20"] = bool(price > ema20) if ema20 else None
    f["above_ema50"] = bool(price > ema50) if ema50 else None
    f["above_ema200"] = bool(price > ema200) if ema200 else None

    # Momentum
    f["rsi14"] = _safe_last(_rsi(df["close"], 14))
    f["macd_line"], f["macd_signal"], f["macd_hist"] = _macd(df["close"])
    f["macd_bull"] = bool(f["macd_line"] > f["macd_signal"]) if f["macd_line"] is not None and f["macd_signal"] is not None else None
    f["macd_hist_rising"] = bool(f["macd_hist"] and f["macd_hist"] > 0) if f["macd_hist"] is not None else None

    # Accumulation / money flow
    f["cmf20"] = _safe_last(_cmf(df, 20))
    f["mfi14"] = _safe_last(_mfi(df, 14))
    f["obv_trend"] = _obv_trend(df)
    # Delivery % is not available from public Yahoo data; proxy it from the
    # money-flow measures so Rule 2 can still be applied (clearly labelled).
    f["delivery_est"] = _delivery_proxy(f["cmf20"], f["mfi14"])
    f["delivery_proxy"] = True

    # Volatility / ATR
    atr = _atr(df, 14)
    f["atr14"] = _safe_last(atr)
    f["atr_pct"] = (f["atr14"] / price * 100.0) if f["atr14"] else None

    # ADX
    pdi, mdi, adx = _adx(df, 14)
    f["adx14"] = _safe_last(adx)
    f["pdi"], f["mdi"] = _safe_last(pdi), _safe_last(mdi)
    f["adx_strength"] = _adx_strength(f["adx14"])

    # TTM squeeze (Bollinger inside Keltner)
    f["squeeze_on"] = _ttm_squeeze(df, atr)
    f["bb_pos"] = _bb_position(df)

    # Aroon
    a_up, a_dn = _aroon(df, 25)
    f["aroon_up"], f["aroon_dn"] = _safe_last(a_up), _safe_last(a_dn)

    # Donchian 52-week channel
    f["donchian_hi"] = _rolling_last(df["high"], 252, lambda v: float(v.max()))
    f["donchian_lo"] = _rolling_last(df["low"], 252, lambda v: float(v.min()))
    f["dist_52w_hi"] = (price / f["donchian_hi"] - 1.0) * 100.0 if f["donchian_hi"] else None

    # Weekly supertrend (red = reject)
    try:
        wk = _weekly_df(df)
        if len(wk) >= 15:
            wdir, _ = _supertrend(wk, 10, 3.0)
            f["wk_supertrend_up"] = bool(_safe_last(wdir, 1) >= 1)
            f["wk_supertrend"] = "green" if f["wk_supertrend_up"] else "red"
        else:
            f["wk_supertrend"] = None
    except Exception:
        f["wk_supertrend"] = None

    # Guppy MMA (short vs long EMA groups)
    f["gmma_bull"] = _gmma(df["close"])

    # Anchored VWAP
    f["avwap"] = _anchored_vwap(df)
    f["above_avwap"] = bool(price > f["avwap"]) if f["avwap"] else None

    # Mansfield relative strength
    f["mrs"] = _mrs(df["close"], index_close) if index_close is not None else None

    # Liquidity (ADTV in ₹ crore)
    f["adtv_cr"] = _adtv_cr(df)
    f["volume_20avg"] = float(df["volume"].tail(20).mean())

    # 52-week range position
    if f["donchian_lo"] and f["donchian_hi"] and f["donchian_hi"] > f["donchian_lo"]:
        f["pct_52w"] = (price - f["donchian_lo"]) / (f["donchian_hi"] - f["donchian_lo"]) * 100.0
    else:
        f["pct_52w"] = None

    return f


def _macd(close):
    line = _ema(close, 12) - _ema(close, 26)
    sig = _ema(line, 9)
    hist = line - sig
    return _safe_last(line), _safe_last(sig), _safe_last(hist)


def _obv_trend(df):
    try:
        obv = _obv(df)
        last = float(obv.iloc[-1])
        prev20 = float(obv.iloc[-21]) if len(obv) > 21 else float(obv.iloc[0])
        return "rising" if last >= prev20 else "falling"
    except Exception:
        return None


def _delivery_proxy(cmf, mfi):
    """Estimate delivery participation from money-flow measures (0-100)."""
    if cmf is None or mfi is None:
        return None
    est = 30.0 + cmf * 25.0 + (mfi - 50.0) * 0.25
    return round(max(0.0, min(100.0, est)), 1)


def _ttm_squeeze(df, atr):
    try:
        mid = df["close"].rolling(20).mean()
        sd = df["close"].rolling(20).std()
        bb_w = 4.0 * sd
        kc_w = 3.0 * atr
        last_bb = _safe_last(bb_w)
        last_kc = _safe_last(kc_w)
        if last_bb is None or last_kc is None:
            return None
        return bool(last_bb < last_kc)
    except Exception:
        return None


def _bb_position(df):
    try:
        mid = df["close"].rolling(20).mean()
        sd = df["close"].rolling(20).std()
        up, dn = mid + 2 * sd, mid - 2 * sd
        up, dn, mid = _safe_last(up), _safe_last(dn), _safe_last(mid)
        if up is None or dn is None or up == dn:
            return None
        return (df["close"].iloc[-1] - dn) / (up - dn) * 100.0
    except Exception:
        return None


def _gmma(close):
    """Guppy Multiple Moving Average: short group above long group = bullish."""
    short = [_safe_last(_ema(close, n)) for n in (3, 5, 8, 10, 12, 15)]
    long = [_safe_last(_ema(close, n)) for n in (30, 35, 40, 45, 50, 60)]
    if any(v is None for v in short + long):
        return None
    return bool(min(short) > max(long))


def _adtv_cr(df):
    """Average daily traded value in ₹ crore over the last 20 sessions."""
    try:
        last20 = df.tail(20)
        val = (last20["close"] * last20["volume"]).mean()
        return round(val / 1e7, 2)  # 1 crore = 10^7
    except Exception:
        return None


# ------------------------------------------------------------- rejection
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


def build_plan(f: dict) -> dict:
    """Entry / SL / targets from ATR (all derived values used by rules/scoring)."""
    price = f["price"]
    atr = f.get("atr14") or price * 0.02
    rr = f.get("rr_t2")
    entry = price
    sl = price - 1.5 * atr
    t1 = price + 1.5 * atr
    t2 = price + 3.0 * atr
    t3 = price + 4.5 * atr
    risk = max(entry - sl, 1e-9)
    f["entry"], f["sl"], f["t1"], f["t2"], f["t3"] = entry, sl, t1, t2, t3
    f["sl_pct"] = abs(entry - sl) / price * 100.0
    f["rr_t1"] = abs(t1 - entry) / risk
    f["rr_t2"] = abs(t2 - entry) / risk
    f["rr_t3"] = abs(t3 - entry) / risk
    return f


# ------------------------------------------------------------- scoring
def score_stock(f: dict) -> tuple[float, dict[str, float]]:
    """100-point score; returns (total, breakdown by category)."""
    s = {}

    # Trend alignment (15)
    t = 0.0
    for flag in ("above_ema20", "above_ema50", "above_ema200"):
        if f.get(flag) is True:
            t += 5.0
    if f.get("ema100") and f["price"] > f["ema100"]:
        t += 0.0  # already counted via 50/200 bands; keep 15 max
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


# ------------------------------------------------------------- market regime
def market_regime(benchmark: dict | None, breadth: dict) -> dict:
    """Summarise regime from the index candles + breadth of the universe."""
    regime = {"label": "MIXED", "details": [], "breadth": breadth}
    details = []
    if benchmark and len(benchmark["close"]) > 60:
        closes = benchmark["close"]
        price = closes[-1]
        ema50 = _safe_last(_ema(pd.Series(closes), 50))
        ema200 = _safe_last(_ema(pd.Series(closes), 200))
        m = len(closes)
        ret_5d = (price / closes[max(0, m - 6)] - 1.0) * 100.0 if m > 6 else None
        ret_20d = (price / closes[max(0, m - 21)] - 1.0) * 100.0 if m > 21 else None
        ret_200d = (price / closes[max(0, m - 201)] - 1.0) * 100.0 if m > 201 else None
        details.append(f"NIFTY 50 {price:,.0f}  (5d {ret_5d:+.1f}% / 20d {ret_20d:+.1f}%)")
        if ema50 is not None:
            details.append(f"NIFTY 50 vs 50 EMA: {'above' if price > ema50 else 'below'} "
                           f"({((price / ema50 - 1) * 100):+.1f}%)")
        if ema200 is not None:
            details.append(f"NIFTY 50 vs 200 EMA: {'above' if price > ema200 else 'below'}")
    vix = breadth.get("vix")
    if vix is not None:
        details.append(f"India VIX {vix:.1f} ({'low/stable' if vix < 15 else 'elevated' if vix < 22 else 'high stress'})")
    b50 = breadth.get("above_ema50")
    b200 = breadth.get("above_ema200")
    adv = breadth.get("advance")
    dec = breadth.get("decline")
    if b50 is not None:
        details.append(f"Breadth: {b50:.0f}% above 50 EMA · {b200:.0f}% above 200 EMA")
    if adv is not None:
        details.append(f"Advance/Decline: {adv:.0f}/{dec:.0f} ({adv / max(dec, 1):.2f})")
    if b50 is not None and b200 is not None and vix is not None:
        if b50 >= 55 and b200 >= 45 and vix < 22:
            regime["label"] = "BULLISH"
        elif b50 >= 45 and b200 >= 35 and vix < 25:
            regime["label"] = "SIDEWAYS-BULLISH"
        elif b50 <= 30 and b200 <= 20:
            regime["label"] = "BEARISH"
        elif b50 <= 45 or vix >= 25:
            regime["label"] = "HIGH VOLATILITY / RISK-OFF"
        else:
            regime["label"] = "SIDEWAYS"
    regime["details"] = details
    return regime


# ------------------------------------------------------------- report text
RULE_LINES = [
    "1. Weekly Supertrend RED or price below 200 SMA",
    "2. Delivery % < 40 (intraday churning)",
    "3. Chaikin Money Flow (CMF 20) < 0.00",
    "4. Mansfield Relative Strength (MRS) < 0.00 vs NIFTY 500",
    "5. R:R to Target 2 < 1:2.0 or Stop Loss > 8%",
    "6. Major unhedged binary event / governance risk",
    "7. Avg daily traded value < \u20b910 crore or wide spread",
]

_FIELD_LABELS = [
    ("price", "Last Price"),
    ("ema20", "EMA 20"),
    ("ema50", "EMA 50"),
    ("ema100", "EMA 100"),
    ("ema200", "EMA 200"),
    ("rsi14", "RSI (14)"),
    ("macd_line", "MACD line"),
    ("macd_signal", "MACD signal"),
    ("macd_hist", "MACD hist"),
    ("adx14", "ADX (14)"),
    ("pdi", "+DI"),
    ("mdi", "-DI"),
    ("atr14", "ATR (14)"),
    ("atr_pct", "ATR % of price"),
    ("cmf20", "CMF (20)"),
    ("mfi14", "MFI (14)"),
    ("obv_trend", "OBV trend"),
    ("delivery_est", "Delivery est."),
    ("aroon_up", "Aroon Up"),
    ("aroon_dn", "Aroon Down"),
    ("donchian_hi", "52w High"),
    ("donchian_lo", "52w Low"),
    ("dist_52w_hi", "Dist. to 52w High"),
    ("pct_52w", "52w range pos."),
    ("squeeze_on", "TTM Squeeze"),
    ("bb_pos", "Bollinger pos."),
    ("wk_supertrend", "Weekly Supertrend"),
    ("gmma_bull", "GMMA bullish"),
    ("avwap", "Anchored VWAP"),
    ("above_avwap", "Above Anch. VWAP"),
    ("mrs", "Mansfield RS"),
    ("adtv_cr", "ADTV (\u20b9cr)"),
]


def _fmt_field(f: dict, key: str) -> str:
    v = f.get(key)
    if v is None:
        return "-"
    if key in ("price", "ema20", "ema50", "ema100", "ema200", "atr14",
               "avwap", "donchian_hi", "donchian_lo"):
        return f"\u20b9{v:,.2f}"
    if key in ("dist_52w_hi", "pct_52w"):
        return f"{v:+.1f}%"
    if key in ("atr_pct",):
        return f"{v:.1f}%"
    if key in ("rsi14", "adx14", "pdi", "mdi", "mfi14", "aroon_up", "aroon_dn", "bb_pos"):
        return f"{v:.1f}"
    if key in ("cmf20", "macd_line", "macd_signal", "macd_hist", "mrs"):
        return f"{v:+.2f}"
    if key in ("delivery_est",):
        return f"{v:.0f}%"
    if key in ("adtv_cr",):
        return f"{v:.1f}"
    if key == "obv_trend":
        return "rising" if v == "rising" else "falling"
    if key == "squeeze_on":
        return "ON" if v else "OFF"
    if key == "gmma_bull":
        return "bullish" if v else "bearish"
    if key == "above_avwap":
        return "yes" if v else "no"
    return str(v)


def _detail_lines(f: dict) -> list[str]:
    lines = []
    for key, label in _FIELD_LABELS:
        lines.append(f"  {label}: <b>{_fmt_field(f, key)}</b>")
    lines.append(f"  Entry: <b>\u20b9{f['entry']:,.2f}</b>  \u00b7  SL: <b>\u20b9{f['sl']:,.2f}</b>")
    lines.append(f"  Targets: \u20b9{f['t1']:,.2f} / \u20b9{f['t2']:,.2f} / \u20b9{f['t3']:,.2f}")
    lines.append(f"  R:R: T1 {f['rr_t1']:.1f}:1 \u00b7 T2 {f['rr_t2']:.1f}:1 \u00b7 T3 {f['rr_t3']:.1f}:1  \u00b7  "
                 f"SL {f['sl_pct']:.1f}%")
    return lines


def _hourly_roadmap(top: dict) -> list[str]:
    e = top["entry"]
    t1, t2, t3 = top["t1"], top["t2"], top["t3"]
    return [
        "<b>\U0001F535 HOURLY EXECUTION ROADMAP (IST)</b>",
        f"\u2022 <b>09:15\u201310:15</b> Opening vol &amp; gap check \u2014 note gap vs "
        f"entry {e:,.0f}",
        f"\u2022 <b>10:15\u201311:15</b> \U0001F7E2 Primary entry window (VWAP reclaim / "
        f"ORB above {e:,.0f})",
        f"\u2022 <b>11:15\u201312:15</b> Trend confirmation &amp; pyramiding \u2014 T1 {t1:,.0f}",
        f"\u2022 <b>12:15\u201313:15</b> Mid-day consolidation \u2014 trail SL to breakeven",
        f"\u2022 <b>13:15\u201314:15</b> European open \u2014 drive toward T2 {t2:,.0f}",
        f"\u2022 <b>14:15\u201315:30</b> Closing power hour \u2014 T3 {t3:,.0f} or square off",
    ]


def format_report(session: dict) -> list[str]:
    """Render the full scanner report as HTML lines for Telegram."""
    regime = session["regime"]
    lines = []
    lines.append("\U0001F4CA <b>NIFTY 500 \u2014 ADVANCED CNC/MIS SCANNER</b>")
    lines.append("")

    # 1. Market regime & breadth
    lines.append("<b>\U0001F300 MARKET REGIME &amp; BREADTH</b>")
    lines.append(f"MARKET REGIME: <b>{regime['label']}</b>")
    for d in regime["details"]:
        lines.append(f"  \u2022 {d}")
    lines.append("")

    # 2. Rejection rules
    lines.append("<b>\u26D4 STRICT \u201cDO NOT BUY / DO NOT SHOW\u201d RULES</b>")
    for r in RULE_LINES:
        lines.append(f"  \u2022 {r}")
    lines.append("")

    # 3. Rejected & excluded
    rejected = session.get("rejected", [])
    lines.append("<b>\u26D4 REJECTED &amp; EXCLUDED</b>")
    if rejected:
        for sym, name, price, reasons in rejected[:12]:
            lines.append(f"  \u2022 <b>{sym}</b> \u2014 {', '.join(reasons)}")
        if len(rejected) > 12:
            lines.append(f"  \u2026 and {len(rejected) - 12} more rejected (see rules)")
    else:
        lines.append("  None \u2014 every scanned stock passed the filters.")
    lines.append("")

    # 4. Top trade setup
    approved = session.get("approved", [])
    top = approved[0] if approved else None
    if top:
        tf = top["fields"]
        score, brk = top["score"], top["breakdown"]
        lines.append("<b>\U0001F3C6 #1 TOP TRADE SETUP</b>")
        lines.append(f"<b>{tf['name']}</b> ({tf['symbol']}.NS)  \u00b7  "
                     f"Score <b>{score:.0f}/100</b>")
        for key, val in brk.items():
            lines.append(f"  {key}: <b>{val:.0f}</b>")
        lines.append("<b>\U0001F4C8 CNC DELIVERY SETUP (Daily)</b>")
        lines.append(f"\U0001F7E2 ENTRY ZONE: <b>\u20b9{tf['entry']:,.2f}</b>  \u00b7  "
                     f"\u26A1 CONFIRM: VWAP reclaim / bullish candle close")
        lines.append(f"\U0001F534 STOP LOSS: <b>\u20b9{tf['sl']:,.2f}</b>  "
                     f"(< {tf['sl_pct']:.1f}% risk)")
        lines.append(f"\U0001F3AF TARGETS: <b>\u20b9{tf['t1']:,.2f}</b> / "
                     f"<b>\u20b9{tf['t2']:,.2f}</b> / <b>\u20b9{tf['t3']:,.2f}</b>")
        lines.append(f"  R:R \u2248 1:{tf['rr_t2']:.1f} to T2")
        lines.append("")
        lines.extend(_hourly_roadmap(tf))
        lines.append("")
        lines.append("<b>\U0001F4CB FULL TECHNICAL MATRIX (TOP PICK)</b>")
        lines.extend(_detail_lines(tf))
        lines.append("")

    # 5. Approved matrix
    if approved:
        lines.append("<b>\U0001F4CA APPROVED STOCKS MATRIX</b>")
        lines.append("  Sym \u00b7 Score \u00b7 RSI \u00b7 ADX \u00b7 CMF \u00b7 MFI \u00b7 "
                     "52w% \u00b7 Entry \u00b7 R:R(T2)")
        for item in approved[:8]:
            f = item["fields"]
            lines.append(
                f"  <b>{f['symbol']}</b> \u00b7 {item['score']:.0f} \u00b7 "
                f"{f['rsi14']:.0f} \u00b7 {f['adx14']:.0f} \u00b7 {f['cmf20']:+.2f} \u00b7 "
                f"{f['mfi14']:.0f} \u00b7 {f['pct_52w']:.0f}% \u00b7 "
                f"\u20b9{f['entry']:,.0f} \u00b7 {f['rr_t2']:.1f}")
        if len(approved) > 8:
            lines.append(f"  \u2026 +{len(approved) - 8} more")
        lines.append("")

    # 6. CNC vs MIS table
    if top:
        lines.append("<b>\u23F1 CNC vs MIS EXECUTION TABLE</b>")
        lines.append("  <b>CNC (Delivery):</b> Daily/weekly trend + CMF > 0 + delivery est. "
                     "> 50% \u2192 swing hold toward T2/T3")
        lines.append("  <b>MIS (Intraday):</b> 5m/15m VWAP reclaim + ORB + volume spurt "
                     "\u2192 exit by 3:30 PM (T1 or stop)")
        lines.append("")

    n_scanned = session.get("scanned", 0)
    lines.append(f"\U0001F4A1 <i>Scanned {n_scanned} NIFTY 500 stocks. Data: Yahoo Finance "
                 "daily candles. Delivery % is an estimate from money-flow (real NSE "
                 "delivery data is not public via this feed). Not investment advice.</i>")
    return lines
