"""Per-stock scan: build the full indicator field set and the trade plan."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from . import indicators as ind

MIN_BARS = 220               # need ~1 year of dailies for 200 EMA + 52w channel


def scan_stock(ohlc, index_close=None) -> Optional[dict]:
    """Compute the full technical field set for one symbol.

    Returns a dict with every indicator used by the score/reject/report
    pipeline, or None when there is not enough data.
    """
    if not ohlc:
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
    ema20 = ind.safe_last(ind.ema(df["close"], 20))
    ema50 = ind.safe_last(ind.ema(df["close"], 50))
    ema100 = ind.safe_last(ind.ema(df["close"], 100))
    ema200 = ind.safe_last(ind.ema(df["close"], 200))
    f.update(ema20=ema20, ema50=ema50, ema100=ema100, ema200=ema200)
    f["above_ema20"] = bool(price > ema20) if ema20 else None
    f["above_ema50"] = bool(price > ema50) if ema50 else None
    f["above_ema200"] = bool(price > ema200) if ema200 else None

    # Momentum
    f["rsi14"] = ind.safe_last(ind.rsi(df["close"], 14))
    f["macd_line"], f["macd_signal"], f["macd_hist"] = ind.macd(df["close"])
    f["macd_bull"] = bool(f["macd_line"] > f["macd_signal"]) if f["macd_line"] is not None and f["macd_signal"] is not None else None
    f["macd_hist_rising"] = bool(f["macd_hist"] and f["macd_hist"] > 0) if f["macd_hist"] is not None else None

    # Accumulation / money flow
    f["cmf20"] = ind.safe_last(ind.cmf(df, 20))
    f["mfi14"] = ind.safe_last(ind.mfi(df, 14))
    f["obv_trend"] = ind.obv_trend(df)
    # Delivery % is not available from public Yahoo data; proxy it from the
    # money-flow measures so Rule 2 can still be applied (clearly labelled).
    f["delivery_est"] = ind.delivery_proxy(f["cmf20"], f["mfi14"])
    f["delivery_proxy"] = True

    # Volatility / ATR
    atr14 = ind.atr(df, 14)
    f["atr14"] = ind.safe_last(atr14)
    f["atr_pct"] = (f["atr14"] / price * 100.0) if f["atr14"] else None

    # ADX
    pdi, mdi, adx_s = ind.adx(df, 14)
    f["adx14"] = ind.safe_last(adx_s)
    f["pdi"], f["mdi"] = ind.safe_last(pdi), ind.safe_last(mdi)
    f["adx_strength"] = ind.adx_strength(f["adx14"])

    # TTM squeeze (Bollinger inside Keltner)
    f["squeeze_on"] = ind.ttm_squeeze(df, atr14)
    f["bb_pos"] = ind.bb_position(df)

    # Aroon
    a_up, a_dn = ind.aroon(df, 25)
    f["aroon_up"], f["aroon_dn"] = ind.safe_last(a_up), ind.safe_last(a_dn)

    # Donchian 52-week channel
    f["donchian_hi"] = ind.rolling_last(df["high"], 252, lambda v: float(v.max()))
    f["donchian_lo"] = ind.rolling_last(df["low"], 252, lambda v: float(v.min()))
    f["dist_52w_hi"] = (price / f["donchian_hi"] - 1.0) * 100.0 if f["donchian_hi"] else None

    # Weekly supertrend (red = reject)
    try:
        wk = ind.weekly_df(df)
        if len(wk) >= 15:
            wdir, _ = ind.supertrend(wk, 10, 3.0)
            f["wk_supertrend_up"] = bool(ind.safe_last(wdir, 1) >= 1)
            f["wk_supertrend"] = "green" if f["wk_supertrend_up"] else "red"
        else:
            f["wk_supertrend"] = None
    except Exception:
        f["wk_supertrend"] = None

    # Guppy MMA (short vs long EMA groups)
    f["gmma_bull"] = ind.gmma(df["close"])

    # Anchored VWAP
    f["avwap"] = ind.anchored_vwap(df)
    f["above_avwap"] = bool(price > f["avwap"]) if f["avwap"] else None

    # Mansfield relative strength
    f["mrs"] = ind.mrs(df["close"], index_close) if index_close is not None else None

    # Liquidity (ADTV in ₹ crore)
    f["adtv_cr"] = ind.adtv_cr(df)
    f["volume_20avg"] = float(df["volume"].tail(20).mean())

    # 52-week range position
    if f["donchian_lo"] and f["donchian_hi"] and f["donchian_hi"] > f["donchian_lo"]:
        f["pct_52w"] = (price - f["donchian_lo"]) / (f["donchian_hi"] - f["donchian_lo"]) * 100.0
    else:
        f["pct_52w"] = None

    return f


def build_plan(f: dict) -> dict:
    """Entry / SL / targets from ATR (all derived values used by rules/scoring)."""
    price = f["price"]
    atr = f.get("atr14") or price * 0.02
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
