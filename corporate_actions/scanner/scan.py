"""Per-stock scan: build the full indicator field set and the trade plan."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from . import indicators as indicators

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
    df["date_time"] = pd.to_datetime(ohlc["timestamp"], unit="s", utc=True)
    price = closes[-1]
    finding = {
        "symbol": ohlc["symbol"], "name": ohlc["name"], "price": price,
        "timeframe": ohlc.get("timeframe", "1d"),
    }
    finding["change_pct"] = ((price / df["open"].iloc[-1]) - 1.0) * 100.0 if df["open"].iloc[-1] else None

    # Trend & structure
    ema20 = indicators.safe_last(indicators.ema(df["close"], 20))
    ema50 = indicators.safe_last(indicators.ema(df["close"], 50))
    ema100 = indicators.safe_last(indicators.ema(df["close"], 100))
    ema200 = indicators.safe_last(indicators.ema(df["close"], 200))
    finding.update(ema20=ema20, ema50=ema50, ema100=ema100, ema200=ema200)
    finding["above_ema20"] = bool(price > ema20) if ema20 else None
    finding["above_ema50"] = bool(price > ema50) if ema50 else None
    finding["above_ema_200"] = bool(price > ema200) if ema200 else None

    # Simple moving averages (golden / death cross for the detail cards)
    finding["sma50"] = indicators.safe_last(df["close"].rolling(50).mean())
    finding["sma200"] = indicators.safe_last(df["close"].rolling(200).mean())
    if finding["sma50"] is not None and finding["sma200"] is not None:
        finding["sma_golden"] = bool(finding["sma50"] > finding["sma200"])

    # Momentum
    finding["rsi14"] = indicators.safe_last(indicators.rsi(df["close"], 14))
    finding["macd_line"], finding["macd_signal"], finding["macd_hist"] = indicators.macd(df["close"])
    finding["macd_bull"] = bool(finding["macd_line"] > finding["macd_signal"]) if finding["macd_line"] is not None and finding["macd_signal"] is not None else None
    finding["macd_hist_rising"] = bool(finding["macd_hist"] and finding["macd_hist"] > 0) if finding["macd_hist"] is not None else None

    # Accumulation / money flow
    finding["cmf20"] = indicators.safe_last(indicators.cmf(df, 20))
    finding["mfi14"] = indicators.safe_last(indicators.mfi(df, 14))
    finding["obv_trend"] = indicators.obv_trend(df)
    # Delivery % is not available from public Yahoo data; proxy it from the
    # money-flow measures so Rule 2 can still be applied (clearly labelled).
    finding["delivery_estimate"] = indicators.delivery_proxy(finding["cmf20"], finding["mfi14"])
    finding["delivery_proxy"] = True

    # Volatility / ATR
    atr14 = indicators.atr(df, 14)
    finding["atr14"] = indicators.safe_last(atr14)
    finding["atr_percent"] = (finding["atr14"] / price * 100.0) if finding["atr14"] else None

    # ADX
    positive_direction_index, negative_direction_index, adx_series = indicators.adx(df, 14)
    finding["adx14"] = indicators.safe_last(adx_series)
    finding["pdi"], finding["mdi"] = indicators.safe_last(positive_direction_index), indicators.safe_last(negative_direction_index)
    finding["adx_strength"] = indicators.adx_strength(finding["adx14"])

    # TTM squeeze (Bollinger inside Keltner)
    finding["squeeze_on"] = indicators.ttm_squeeze(df, atr14)
    finding["bollinger_position"] = indicators.bb_position(df)

    # Aroon
    aroon_up, aroon_down = indicators.aroon(df, 25)
    finding["aroon_up"], finding["aroon_down"] = indicators.safe_last(aroon_up), indicators.safe_last(aroon_down)

    # Stochastic oscillator %K / %D
    finding["stoch_k"], finding["stoch_d"] = indicators.stochastic(df)

    # Bollinger bands: upper / mid / lower + %B position
    finding["bb_upper"], finding["bb_mid"], finding["bb_lower"], finding["bb_percent_b"] = indicators.bollinger_bands(df)

    # Commodity Channel Index
    finding["cci20"] = indicators.cci(df)

    # Parabolic SAR direction
    finding["psar_dir"] = indicators.psar_direction(df)

    # Donchian 52-week channel
    finding["donchian_high"] = indicators.rolling_last(df["high"], 252, lambda value: float(value.max()))
    finding["donchian_low"] = indicators.rolling_last(df["low"], 252, lambda value: float(value.min()))
    finding["distance_from_52w_high"] = (price / finding["donchian_high"] - 1.0) * 100.0 if finding["donchian_high"] else None

    # Weekly supertrend (red = reject)
    try:
        weekly = indicators.weekly_df(df)
        if len(weekly) >= 15:
            weekly_direction, _ = indicators.supertrend(weekly, 10, 3.0)
            finding["weekly_supertrend_up"] = bool(indicators.safe_last(weekly_direction, 1) >= 1)
            finding["weekly_supertrend"] = "green" if finding["weekly_supertrend_up"] else "red"
        else:
            finding["weekly_supertrend"] = None
    except Exception:
        finding["weekly_supertrend"] = None

    # Guppy MMA (short vs long EMA groups)
    finding["gmma_bull"] = indicators.gmma(df["close"])

    # Anchored VWAP
    finding["avwap"] = indicators.anchored_vwap(df)
    finding["above_avwap"] = bool(price > finding["avwap"]) if finding["avwap"] else None

    # Mansfield relative strength
    finding["mansfield_rs"] = indicators.mansfield_relative_strength(df["close"], index_close) if index_close is not None else None

    # Liquidity (ADTV in ₹ crore) + volume ratio vs the 20-day average
    finding["average_daily_traded_value_crores"] = indicators.daily_traded_value_crore(df)
    finding["volume_20avg"] = float(df["volume"].tail(20).mean())
    finding["volume_ratio"] = indicators.volume_ratio(df)

    # 52-week range position
    if finding["donchian_low"] and finding["donchian_high"] and finding["donchian_high"] > finding["donchian_low"]:
        finding["percent_52w_range"] = (price - finding["donchian_low"]) / (finding["donchian_high"] - finding["donchian_low"]) * 100.0
    else:
        finding["percent_52w_range"] = None

    return finding


def build_plan(finding: dict) -> dict:
    """Entry / SL / targets from ATR (all derived values used by rules/scoring)."""
    price = finding["price"]
    atr = finding.get("atr14") or price * 0.02
    entry = price
    stop_loss = price - 1.5 * atr
    target_1 = price + 1.5 * atr
    target_2 = price + 3.0 * atr
    target_3 = price + 4.5 * atr
    risk = max(entry - stop_loss, 1e-9)
    finding["entry"], finding["stop_loss"], finding["target_1"], finding["target_2"], finding["target_3"] = entry, stop_loss, target_1, target_2, target_3
    finding["stop_loss_percent"] = abs(entry - stop_loss) / price * 100.0
    finding["reward_risk_target_1"] = abs(target_1 - entry) / risk
    finding["reward_risk_target_2"] = abs(target_2 - entry) / risk
    finding["reward_risk_target_3"] = abs(target_3 - entry) / risk
    return finding
