"""Technical indicator computations (pandas-based, pure functions).

Each function takes a DataFrame/series of OHLC data and returns the indicator
series or its final value - no I/O, no side effects.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def safe_last(series, default=None):
    if series is None:
        return default
    try:
        if series.empty:
            return default
        val = series.iloc[-1]
        return None if (val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val)))) else float(val)
    except Exception:
        return default


def rolling_last(s, window, fn):
    """Apply a rolling function and return only the final value."""
    if s is None or s.empty:
        return None
    vals = s.values
    n = len(vals)
    if n < window:
        return None
    return float(fn(vals[n - window:n]))


def ema(s, span):
    return s.ewm(span=span, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0.0, 1e-9)
    return 100.0 - 100.0 / (1.0 + rs)


def true_range(df):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def adx(df, n=14):
    tr = true_range(df)
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    atr_s = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0.0, 1e-9)
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr_s
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / atr_s
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0.0, 1e-9)
    adx_s = dx.ewm(alpha=1.0 / n, adjust=False).mean()
    return pdi, mdi, adx_s


def cmf(df, n=20):
    hl = (df["high"] - df["low"]).replace(0.0, 1e-9)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl
    mfv = mfm * df["volume"]
    return mfv.rolling(n).sum() / df["volume"].rolling(n).sum().replace(0.0, 1e-9)


def mfi(df, n=14):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    mf = tp * df["volume"]
    d = tp.diff()
    pos = mf.where(d > 0, 0.0).rolling(n).sum()
    neg = mf.where(d < 0, 0.0).rolling(n).sum()
    return 100.0 - 100.0 / (1.0 + pos / neg.replace(0.0, 1e-9))


def obv(df):
    sgn = np.sign(df["close"].diff()).fillna(0.0)
    return (sgn * df["volume"]).cumsum()


def aroon(df, n=25):
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


def supertrend(df, period=10, mult=3.0):
    """Supertrend over a DataFrame with OHLC. Returns (direction_df, line_df)."""
    h, l, c = df["high"], df["low"], df["close"]
    atr_s = atr(df, period)
    hl2 = (h + l) / 2.0
    ub = hl2 + mult * atr_s
    lb = hl2 - mult * atr_s
    n = len(df)
    st = pd.Series(np.nan, index=df.index)
    dirn = pd.Series(1, index=df.index)
    if n == 0:
        return dirn, st
    prev_st = None
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
    return dirn, st


def weekly_df(daily):
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


def anchored_vwap(df, lookback=252):
    """VWAP anchored at the lowest low over the last `lookback` bars."""
    sub = df.tail(lookback)
    anchor = sub["low"].idxmin()
    anchor_pos = df.index.get_loc(anchor)
    seg = df.iloc[anchor_pos:]
    tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
    tot = (tp * seg["volume"]).sum()
    vol = seg["volume"].sum()
    return float(tot / vol) if vol > 0 else None


def mrs(sym_close, idx_close):
    """Mansfield relative strength vs an index benchmark."""
    sym_close = pd.Series(sym_close) if not isinstance(sym_close, pd.Series) else sym_close
    idx_close = pd.Series(idx_close) if not isinstance(idx_close, pd.Series) else idx_close
    n = min(200, len(sym_close), len(idx_close))
    if n < 120:
        return None
    s = sym_close.values[-n:]
    i = idx_close.values[-n:]
    rs = pd.Series(s / i)
    ma20 = rs.rolling(20).mean()
    last_ma = safe_last(ma20)
    if not last_ma:
        return None
    return (float(rs.iloc[-1]) / last_ma - 1.0) * 100.0


def adx_strength(adx):
    if adx is None:
        return 0
    if adx >= 40:
        return 3
    if adx >= 25:
        return 2
    if adx >= 20:
        return 1
    return 0


def macd(close):
    line = ema(close, 12) - ema(close, 26)
    sig = ema(line, 9)
    hist = line - sig
    return safe_last(line), safe_last(sig), safe_last(hist)


def obv_trend(df):
    try:
        obv_s = obv(df)
        last = float(obv_s.iloc[-1])
        prev20 = float(obv_s.iloc[-21]) if len(obv_s) > 21 else float(obv_s.iloc[0])
        return "rising" if last >= prev20 else "falling"
    except Exception:
        return None


def delivery_proxy(cmf, mfi):
    """Estimate delivery participation from money-flow measures (0-100)."""
    if cmf is None or mfi is None:
        return None
    est = 30.0 + cmf * 25.0 + (mfi - 50.0) * 0.25
    return round(max(0.0, min(100.0, est)), 1)


def ttm_squeeze(df, atr_s):
    try:
        sd = df["close"].rolling(20).std()
        bb_w = 4.0 * sd
        kc_w = 3.0 * atr_s
        last_bb = safe_last(bb_w)
        last_kc = safe_last(kc_w)
        if last_bb is None or last_kc is None:
            return None
        return bool(last_bb < last_kc)
    except Exception:
        return None


def bb_position(df):
    try:
        mid = df["close"].rolling(20).mean()
        sd = df["close"].rolling(20).std()
        up, dn = mid + 2 * sd, mid - 2 * sd
        up, dn, mid = safe_last(up), safe_last(dn), safe_last(mid)
        if up is None or dn is None or up == dn:
            return None
        return (df["close"].iloc[-1] - dn) / (up - dn) * 100.0
    except Exception:
        return None


def gmma(close):
    """Guppy Multiple Moving Average: short group above long group = bullish."""
    short = [safe_last(ema(close, n)) for n in (3, 5, 8, 10, 12, 15)]
    long = [safe_last(ema(close, n)) for n in (30, 35, 40, 45, 50, 60)]
    if any(v is None for v in short + long):
        return None
    return bool(min(short) > max(long))


def adtv_cr(df):
    """Average daily traded value in ₹ crore over the last 20 sessions."""
    try:
        last20 = df.tail(20)
        val = (last20["close"] * last20["volume"]).mean()
        return round(val / 1e7, 2)  # 1 crore = 10^7
    except Exception:
        return None
