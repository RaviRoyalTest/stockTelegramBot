"""Per-indicator deep-dive report for /indicator (pure formatting, no I/O).

One indicator at a time: current value(s) with a clear signal, the short-term
trend of the indicator itself, a plain-language explanation of what the value
means for this stock, and a "how to read the levels" legend. All indicators
are computed from the daily OHLC dict (same math as the NIFTY 500 scanner in
scanner/indicators.py); fetching the candles happens in the bot handler.
"""
from __future__ import annotations

from difflib import get_close_matches

import pandas as pd

from ..core.text import escape
from . import indicators

# Canonical indicator keys in display order.
INDICATOR_KEYS = (
    "rsi", "macd", "stochastic", "bollinger", "cci", "adx", "aroon", "psar",
    "supertrend", "moving_average", "gmma", "vwap", "atr", "donchian",
    "squeeze", "cmf", "mfi", "obv",
)

_ALIASES = {
    "rsi": "rsi", "relative strength index": "rsi",
    "macd": "macd", "moving average convergence divergence": "macd",
    "stochastic": "stochastic", "stoch": "stochastic", "kd": "stochastic",
    "kdj": "stochastic",
    "bollinger": "bollinger", "bollinger band": "bollinger",
    "bollinger bands": "bollinger", "bb": "bollinger",
    "cci": "cci", "commodity channel index": "cci",
    "adx": "adx", "average directional index": "adx",
    "aroon": "aroon",
    "psar": "psar", "sar": "psar", "parabolic sar": "psar",
    "parabolic": "psar",
    "supertrend": "supertrend", "st": "supertrend",
    "gmma": "gmma", "guppy": "gmma",
    "guppy multiple moving average": "gmma",
    "ema": "moving_average", "sma": "moving_average",
    "moving average": "moving_average", "moving averages": "moving_average",
    "ma": "moving_average",
    "vwap": "vwap", "anchored vwap": "vwap", "avwap": "vwap",
    "volume weighted average price": "vwap",
    "atr": "atr", "average true range": "atr", "volatility": "atr",
    "donchian": "donchian", "52w": "donchian", "52 week": "donchian",
    "52-week": "donchian", "range": "donchian", "52 week range": "donchian",
    "squeeze": "squeeze", "ttm": "squeeze", "ttm squeeze": "squeeze",
    "cmf": "cmf", "chaikin money flow": "cmf",
    "mfi": "mfi", "money flow index": "mfi",
    "obv": "obv", "on balance volume": "obv",
}


def match_indicator(name: str | None) -> str | None:
    """Resolve a user-typed indicator name to a canonical key.

    Case/space-insensitive against a full alias table, then typo-tolerant
    (difflib) against the aliases - so 'stocastic', 'BB', 'sar' all resolve.
    Returns None when nothing matches.
    """
    if not name:
        return None
    normalized = " ".join((name or "").lower().split())
    if normalized in _ALIASES:
        return _ALIASES[normalized]
    candidates = list(_ALIASES.keys()) + list(INDICATOR_KEYS)
    close = get_close_matches(normalized, candidates, n=1, cutoff=0.62)
    return _ALIASES.get(close[0], close[0]) if close else None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _df(ohlc: dict) -> pd.DataFrame:
    return pd.DataFrame({
        "open": ohlc["open"], "high": ohlc["high"], "low": ohlc["low"],
        "close": ohlc["close"], "volume": ohlc["volume"],
    })


def _trend_str(series, lookback: int = 5) -> str | None:
    """'rising' / 'falling' / 'flat' for the last `lookback` values (or None)."""
    try:
        values = list(series.dropna())
    except Exception:
        return None
    if len(values) < lookback + 1:
        return None
    earlier, latest = values[-lookback], values[-1]
    scale = max(abs(earlier), abs(latest), 1e-9)
    delta = (latest - earlier) / scale
    if delta > 0.01:
        return "rising"
    if delta < -0.01:
        return "falling"
    return "flat"


def _run_length(values, target) -> int:
    """How many consecutive trailing values equal `target` (direction runs)."""
    count = 0
    for value in reversed(values):
        if value == target:
            count += 1
        else:
            break
    return count


def _row(label: str, text: str, emoji: str = "") -> tuple:
    return (label, text, emoji)


# --------------------------------------------------------------------------
# per-indicator renderers: each returns (headline, emoji, rows, trend)
# --------------------------------------------------------------------------

def _render_rsi(df):
    value = indicators.safe_last(indicators.rsi(df["close"], 14))
    if value is None:
        return None
    zone = ("Oversold" if value <= 30 else ("Overbought" if value >= 70
            else ("High" if value >= 60 else ("Low" if value <= 45 else "Neutral"))))
    emoji = "\U0001F7E2" if value <= 45 else ("\U0001F534" if value >= 60 else "\U0001F7E1")
    return (
        f"RSI {value:.1f} \u2014 {zone}", emoji,
        [_row("Value", f"{value:.1f}", emoji)],
        _trend_str(indicators.rsi(df["close"], 14)),
    )


def _render_macd(df):
    line, signal, hist = indicators.macd(df["close"])
    if line is None or signal is None:
        return None
    bull = line >= signal
    emoji = "\U0001F7E2" if bull else "\U0001F534"
    headline = "Bullish crossover" if bull else "Bearish crossover"
    rows = [
        _row("MACD line", f"{line:.2f}", emoji),
        _row("Signal line", f"{signal:.2f}"),
        _row("Histogram", f"{hist:+.2f}" if hist is not None else "-",
             "\U0001F7E2" if (hist or 0) > 0 else ("\U0001F534" if (hist or 0) < 0 else "")),
    ]
    line_series = indicators.ema(df["close"], 12) - indicators.ema(df["close"], 26)
    trend = _trend_str(line_series)
    return headline, emoji, rows, trend


def _render_stochastic(df):
    k, d = indicators.stochastic(df)
    if k is None:
        return None
    zone = "Overbought" if k >= 80 else ("Oversold" if k <= 20 else "Neutral")
    emoji = "\U0001F534" if k >= 80 else ("\U0001F7E2" if k <= 20 else "\U0001F7E1")
    k_series, _ = indicators.stochastic(df)
    return (
        f"%K {k:.0f} \u2014 {zone}", emoji,
        [_row("%K (fast)", f"{k:.0f}", emoji),
         _row("%D (slow)", f"{d:.0f}" if d is not None else "-")],
        _trend_str(k_series),
    )


def _render_bollinger(df):
    upper, mid, lower, percent_b = indicators.bollinger_bands(df)
    if upper is None or lower is None:
        return None
    if percent_b is None:
        zone, emoji = "Neutral", "\U0001F7E1"
    elif percent_b >= 100:
        zone, emoji = "Above upper band (overbought)", "\U0001F534"
    elif percent_b <= 0:
        zone, emoji = "Below lower band (oversold)", "\U0001F7E2"
    elif percent_b >= 80:
        zone, emoji = "Upper half (strong)", "\U0001F7E2"
    elif percent_b <= 20:
        zone, emoji = "Lower half (weak)", "\U0001F534"
    else:
        zone, emoji = "Middle band", "\U0001F7E1"
    close_series = df["close"]
    mid_series = close_series.rolling(20).mean()
    trend = _trend_str(mid_series)
    return (
        f"%B {percent_b:.0f} \u2014 {zone}", emoji,
        [_row("Upper band", f"{upper:,.2f}"),
         _row("Middle band", f"{mid:,.2f}"),
         _row("Lower band", f"{lower:,.2f}"),
         _row("%B position", f"{percent_b:.0f}", emoji)],
        trend,
    )


def _render_cci(df):
    value = indicators.cci(df)
    if value is None:
        return None
    zone = ("Overbought" if value >= 100 else ("Oversold" if value <= -100
            else ("Bullish" if value > 0 else "Bearish")))
    emoji = "\U0001F534" if value >= 100 else ("\U0001F7E2" if value <= -100
            else ("\U0001F7E2" if value > 0 else "\U0001F534"))
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    mean = typical.rolling(20).mean()
    mean_dev = typical.rolling(20).apply(
        lambda values: float(abs(values - values.mean()).mean()), raw=True,
    )
    series = (typical - mean) / mean_dev.replace(0.0, 1e-9) / 0.015
    return (
        f"CCI {value:.0f} \u2014 {zone}", emoji,
        [_row("Value", f"{value:.0f}", emoji)],
        _trend_str(series),
    )


def _render_adx(df):
    pdi, mdi, adx_series = indicators.adx(df, 14)
    adx = indicators.safe_last(adx_series)
    if adx is None:
        return None
    strength = ("Strong trend" if adx >= 40 else ("Trending" if adx >= 25
                else ("Developing" if adx >= 20 else "Range-bound")))
    pdi_value = indicators.safe_last(pdi)
    mdi_value = indicators.safe_last(mdi)
    bullish = pdi_value is not None and mdi_value is not None and pdi_value > mdi_value
    emoji = "\U0001F7E2" if bullish else "\U0001F534"
    return (
        f"ADX {adx:.1f} \u2014 {strength}", emoji,
        [_row("ADX(14)", f"{adx:.1f}"),
         _row("+DI", f"{pdi_value:.1f}" if pdi_value is not None else "-"),
         _row("-DI", f"{mdi_value:.1f}" if mdi_value is not None else "-")],
        _trend_str(adx_series),
    )


def _render_aroon(df):
    up, down = indicators.aroon(df, 25)
    up_value, down_value = indicators.safe_last(up), indicators.safe_last(down)
    if up_value is None or down_value is None:
        return None
    bullish = up_value >= down_value
    emoji = "\U0001F7E2" if bullish else "\U0001F534"
    return (
        f"Up {up_value:.0f} vs Down {down_value:.0f} \u2014 "
        f"{'Bullish' if bullish else 'Bearish'}", emoji,
        [_row("Aroon Up", f"{up_value:.0f}", emoji),
         _row("Aroon Down", f"{down_value:.0f}")],
        _trend_str(up),
    )


def _render_psar(df):
    direction = indicators.psar_direction(df)
    if direction is None:
        return None
    bull = direction == "bull"
    emoji = "\U0001F7E2" if bull else "\U0001F534"
    high, low = df["high"], df["low"]
    count = _run_length(
        [indicators.psar_direction(df.iloc[: index + 1]) for index in range(len(df) - 20, len(df))],
        direction,
    ) if len(df) > 20 else 0
    trend = f"{direction} for the last {count} sessions" if count > 1 else None
    return (
        f"{'Bullish' if bull else 'Bearish'} (SAR {'below' if bull else 'above'} price)",
        emoji,
        [_row("Direction", "Up / Bullish" if bull else "Down / Bearish", emoji)],
        trend,
    )


def _render_supertrend(df):
    try:
        direction_series, line_series = indicators.supertrend(df, 10, 3.0)
        direction = indicators.safe_last(direction_series, 1)
        line = indicators.safe_last(line_series)
    except Exception:
        return None
    bull = direction >= 1
    emoji = "\U0001F7E2" if bull else "\U0001F534"
    count = _run_length(list(direction_series.dropna()), 1) if bull else \
        _run_length(list(direction_series.dropna()), -1)
    trend = f"uptrend for the last {count} sessions" if count > 1 else None
    return (
        f"{'Uptrend' if bull else 'Downtrend'}", emoji,
        [_row("Direction", "Up (buy the dips)" if bull else "Down (avoid)", emoji),
         _row("Supertrend line", f"{line:,.2f}" if line is not None else "-")],
        trend,
    )


def _render_moving_average(df):
    close = df["close"]
    price = float(close.iloc[-1])
    ema20, ema50, ema100, ema200 = (
        indicators.safe_last(indicators.ema(close, span)) for span in (20, 50, 100, 200)
    )
    sma50 = indicators.safe_last(close.rolling(50).mean())
    sma200 = indicators.safe_last(close.rolling(200).mean())
    if ema200 is None:
        return None
    above_all = all(value is not None and price > value for value in (ema20, ema50, ema100, ema200))
    golden = sma50 is not None and sma200 is not None and sma50 > sma200
    emoji = "\U0001F7E2" if (above_all and golden) else ("\U0001F7E1" if above_all else "\U0001F534")
    rows = [
        _row("Price", f"{price:,.2f}"),
        _row("EMA 20", f"{ema20:,.2f}" if ema20 is not None else "-"),
        _row("EMA 50", f"{ema50:,.2f}" if ema50 is not None else "-"),
        _row("EMA 100", f"{ema100:,.2f}" if ema100 is not None else "-"),
        _row("EMA 200", f"{ema200:,.2f}"),
        _row("SMA 50", f"{sma50:,.2f}" if sma50 is not None else "-"),
        _row("SMA 200", f"{sma200:,.2f}" if sma200 is not None else "-"),
        _row("SMA 50 vs 200",
             "Golden cross (bullish)" if golden else ("Death cross (bearish)" if sma50 is not None and sma200 is not None else "-"),
             "\U0001F7E2" if golden else ("\U0001F534" if sma50 is not None and sma200 is not None and not golden else "")),
    ]
    ema20_series = indicators.ema(close, 20)
    return ("Price above all key EMAs" if above_all else "Price below some key EMAs",
            emoji, rows, _trend_str(ema20_series))


def _render_gmma(df):
    short = [indicators.safe_last(indicators.ema(df["close"], period)) for period in (3, 5, 8, 10, 12, 15)]
    long = [indicators.safe_last(indicators.ema(df["close"], period)) for period in (30, 35, 40, 45, 50, 60)]
    if any(value is None for value in short + long):
        return None
    bull = min(short) > max(long)
    emoji = "\U0001F7E2" if bull else "\U0001F534"
    short_avg = sum(short) / len(short)
    long_avg = sum(long) / len(long)
    spread = (short_avg / long_avg - 1.0) * 100.0
    return (
        f"{'Bullish' if bull else 'Bearish'} (short EMAs {'above' if bull else 'below'} long EMAs)",
        emoji,
        [_row("Short group avg", f"{short_avg:,.2f}"),
         _row("Long group avg", f"{long_avg:,.2f}"),
         _row("Spread", f"{spread:+.1f}%", emoji)],
        None,
    )


def _render_vwap(df):
    avwap = indicators.anchored_vwap(df)
    if avwap is None:
        return None
    price = float(df["close"].iloc[-1])
    above = price >= avwap
    emoji = "\U0001F7E2" if above else "\U0001F534"
    distance = (price / avwap - 1.0) * 100.0
    return (
        f"Price {distance:+.1f}% {'above' if above else 'below'} anchored VWAP", emoji,
        [_row("Anchored VWAP", f"{avwap:,.2f}"),
         _row("Price", f"{price:,.2f}"),
         _row("Distance", f"{distance:+.1f}%", emoji)],
        None,
    )


def _render_atr(df):
    atr = indicators.safe_last(indicators.atr(df, 14))
    if atr is None:
        return None
    price = float(df["close"].iloc[-1])
    atr_pct = atr / price * 100.0
    zone = ("Very high volatility" if atr_pct >= 6 else ("High volatility" if atr_pct >= 4
            else ("Moderate volatility" if atr_pct >= 2 else "Low volatility")))
    emoji = "\U0001F534" if atr_pct >= 6 else ("\U0001F7E1" if atr_pct >= 4 else "\U0001F7E2")
    return (
        f"ATR {atr_pct:.1f}% of price \u2014 {zone}", emoji,
        [_row("ATR(14)", f"{atr:,.2f}"),
         _row("ATR as % of price", f"{atr_pct:.1f}%", emoji)],
        _trend_str(indicators.atr(df, 14)),
    )


def _render_donchian(df):
    high = indicators.rolling_last(df["high"], 252, lambda values: float(max(values)))
    low = indicators.rolling_last(df["low"], 252, lambda values: float(min(values)))
    if high is None or low is None or high <= low:
        return None
    price = float(df["close"].iloc[-1])
    position = (price - low) / (high - low) * 100.0
    zone = ("Near 52W high" if position >= 85 else ("Upper half" if position >= 50
            else ("Lower half" if position >= 15 else "Near 52W low")))
    emoji = ("\U0001F534" if position >= 85 else ("\U0001F7E2" if position <= 15 else "\U0001F7E1"))
    distance = (price / high - 1.0) * 100.0
    return (
        f"{position:.0f}% up the 52-week range \u2014 {zone}", emoji,
        [_row("52W High", f"{high:,.2f}"),
         _row("52W Low", f"{low:,.2f}"),
         _row("Position in range", f"{position:.0f}%", emoji),
         _row("Distance from high", f"{distance:.1f}%")],
        None,
    )


def _render_squeeze(df):
    atr = indicators.atr(df, 14)
    squeeze = indicators.ttm_squeeze(df, atr)
    if squeeze is None:
        return None
    emoji = "\U0001F7E1" if squeeze else "\U0001F7E2"
    return (
        ("Squeeze ON \u2014 coils, breakout pending" if squeeze else "No squeeze \u2014 trending"),
        emoji,
        [_row("State", "ON (volatility compressing)" if squeeze else "OFF (normal volatility)", emoji)],
        None,
    )


def _render_cmf(df):
    value = indicators.safe_last(indicators.cmf(df, 20))
    if value is None:
        return None
    zone = ("Buying pressure" if value > 0.05 else ("Selling pressure" if value < -0.05 else "Neutral"))
    emoji = "\U0001F7E2" if value > 0.05 else ("\U0001F534" if value < -0.05 else "\U0001F7E1")
    return (
        f"CMF {value:+.2f} \u2014 {zone}", emoji,
        [_row("Value", f"{value:+.2f}", emoji)],
        _trend_str(indicators.cmf(df, 20)),
    )


def _render_mfi(df):
    value = indicators.safe_last(indicators.mfi(df, 14))
    if value is None:
        return None
    zone = ("Overbought" if value >= 80 else ("Oversold" if value <= 20
            else ("Accumulation" if value > 55 else "Neutral")))
    emoji = "\U0001F534" if value >= 80 else ("\U0001F7E2" if value <= 20
            else ("\U0001F7E2" if value > 55 else "\U0001F7E1"))
    return (
        f"MFI {value:.1f} \u2014 {zone}", emoji,
        [_row("Value", f"{value:.1f}", emoji)],
        _trend_str(indicators.mfi(df, 14)),
    )


def _render_obv(df):
    trend = indicators.obv_trend(df)
    if trend is None:
        return None
    rising = trend == "rising"
    emoji = "\U0001F7E2" if rising else "\U0001F534"
    return (
        f"OBV {'rising' if rising else 'falling'} (volume confirming price)", emoji,
        [_row("Trend", "Rising \u2014 buyers accumulating" if rising else "Falling \u2014 sellers distributing", emoji)],
        None,
    )


_RENDERERS = {
    "rsi": _render_rsi,
    "macd": _render_macd,
    "stochastic": _render_stochastic,
    "bollinger": _render_bollinger,
    "cci": _render_cci,
    "adx": _render_adx,
    "aroon": _render_aroon,
    "psar": _render_psar,
    "supertrend": _render_supertrend,
    "moving_average": _render_moving_average,
    "gmma": _render_gmma,
    "vwap": _render_vwap,
    "atr": _render_atr,
    "donchian": _render_donchian,
    "squeeze": _render_squeeze,
    "cmf": _render_cmf,
    "mfi": _render_mfi,
    "obv": _render_obv,
}

_LABELS = {
    "rsi": "RSI (14) \u2014 Relative Strength Index",
    "macd": "MACD (12,26,9)",
    "stochastic": "Stochastic Oscillator (14,3,3)",
    "bollinger": "Bollinger Bands (20, 2\u03c3)",
    "cci": "CCI (20) \u2014 Commodity Channel Index",
    "adx": "ADX (14) \u2014 Average Directional Index",
    "aroon": "Aroon (25)",
    "psar": "Parabolic SAR (0.02, 0.2)",
    "supertrend": "Supertrend (10, 3.0)",
    "moving_average": "Moving Averages \u2014 EMA & SMA",
    "gmma": "GMMA \u2014 Guppy Multiple Moving Average",
    "vwap": "Anchored VWAP",
    "atr": "ATR (14) \u2014 Average True Range",
    "donchian": "Donchian Channel \u2014 52-Week Range",
    "squeeze": "TTM Squeeze",
    "cmf": "CMF (20) \u2014 Chaikin Money Flow",
    "mfi": "MFI (14) \u2014 Money Flow Index",
    "obv": "OBV \u2014 On-Balance Volume",
}

_MEANING = {
    "rsi": "RSI measures the speed and size of recent gains vs losses on a 0-100 "
           "scale using average (Wilder-smoothed) moves. Above 60 momentum is strong "
           "but crowding in; above 70 the stock is overbought and often due a pause. "
           "Below 45 momentum is weak; below 30 it is oversold and often bounces.",
    "macd": "MACD is the gap between the 12-day and 26-day exponential averages; the "
            "signal line is its 9-day average. When MACD is above the signal line "
            "buyers are in control, below it sellers are. The histogram shows whether "
            "that momentum is accelerating (growing) or fading (shrinking).",
    "stochastic": "Stochastic compares the close to the high-low range of the last 14 "
                  "sessions. Above 80 the stock is closing near its highs (overbought); "
                  "below 20 near its lows (oversold). A %K/%D cross in the 20-50 zone "
                  "is the classic early-bullish signal.",
    "bollinger": "Bollinger Bands wrap the 20-day average with two standard deviations "
                 "of volatility. %B above 100 means price is above the upper band "
                 "(stretched, overbought); below 0 below the lower band (oversold). "
                 "The bands squeeze before breakouts.",
    "cci": "CCI measures how far price sits from its typical (high+low+close)/3 level. "
           "Above +100 is unusually strong (overbought), below -100 unusually weak "
           "(oversold). Sustained positive readings confirm an uptrend.",
    "adx": "ADX measures trend strength, not direction. Above 25 the stock is trending "
           "(trades with the trend work); below 20 it is range-bound. +DI above -DI "
           "means the buyers' directional movement dominates \u2014 the uptrend side.",
    "aroon": "Aroon measures how recently price made a new 25-session high (Up) or low "
             "(Down). Up above Down and near 100 means the stock is still making "
             "fresh highs \u2014 a strong uptrend. Down above Up signals distribution.",
    "psar": "Parabolic SAR places a stop-and-reverse dot below price in uptrends and "
            "above it in downtrends. A dot below price means the trend is up and "
            "traders trail their stop under it; the flip of the dot marks the "
            "trend reversal.",
    "supertrend": "Supertrend plots a trailing line from ATR bands; price above it is "
                  "an uptrend (buy dips), below it a downtrend (avoid). The flip of "
                  "the line is the trend-change signal used by the /scan500 rules.",
    "moving_average": "Moving averages smooth price to show the underlying trend. Price "
                      "above the EMAs (especially the 200-day) means the long-term "
                      "trend is up; a rising EMA ladder (20 > 50 > 100 > 200) is a "
                      "clean uptrend. SMA50 above SMA200 is the golden cross.",
    "gmma": "GMMA stacks six short EMAs (3-15) against six long EMAs (30-60). Short "
            "group above the long group = buyers of every horizon are in control "
            "(bullish); below = bearish. The wider the gap, the stronger the trend.",
    "vwap": "Anchored VWAP is the average traded price since the lowest low of the "
            "last year. Price above it means most holders are in profit (support); "
            "below it means the stock is under water (resistance).",
    "atr": "ATR is the average true daily range in rupees - how much the stock "
           "typically moves in a session. It sets the size of your stop loss: "
           "~1.5x ATR is a standard buffer. Rising ATR = volatility expanding.",
    "donchian": "The Donchian channel marks the 52-week high and low. The % position "
                "shows where price sits between them. New highs can break out further; "
                "the low acts as the long-term floor.",
    "squeeze": "The TTM Squeeze compares Bollinger Bands to Keltner bands. When the "
               "Bollinger width is inside Keltner, volatility is compressed - the "
               "coil before a breakout. The breakout direction is confirmed by price "
               "leaving the bands with volume.",
    "cmf": "Chaikin Money Flow adds up-volume vs down-volume over 20 sessions, scaled "
           "by where each close sits in its day's range. Positive = money flowing in "
           "(accumulation); negative = flowing out (distribution).",
    "mfi": "Money Flow Index is RSI weighted by volume: how much money is pushing "
           "price up vs down. Above 80 overbought, below 20 oversold, and sustained "
           "readings above 55 confirm accumulation.",
    "obv": "On-Balance Volume adds a day's full volume on up closes and subtracts it "
           "on down closes. A rising OBV means volume is confirming the advance; "
           "OBV falling while price rises warns the rally lacks buyers.",
}

_LEGENDS = {
    "rsi": [
        "RSI \u2264 30 \u2192 \U0001F7E2 oversold (bounce zone)",
        "RSI 45-60 \u2192 \U0001F7E1 neutral",
        "RSI \u2265 60 \u2192 \U0001F534 high (strong but crowding)",
        "RSI \u2265 70 \u2192 \U0001F534 overbought (pullback risk)",
    ],
    "macd": [
        "MACD above Signal + Histogram rising \u2192 \U0001F7E2 strong bullish momentum",
        "MACD above Signal + Histogram falling \u2192 \U0001F7E1 bullish, cooling",
        "MACD below Signal \u2192 \U0001F534 bearish momentum",
        "Crossovers near the zero line are the most reliable",
    ],
    "stochastic": [
        "%K \u2265 80 \u2192 \U0001F534 overbought (do not chase)",
        "%K \u2264 20 \u2192 \U0001F7E2 oversold (watch for a %K/%D cross)",
        "%K rising inside 20-50 \u2192 \U0001F7E2 early bullish",
        "Cross above %D = buy signal \u00b7 cross below = sell signal",
    ],
    "bollinger": [
        "%B > 100 \u2192 \U0001F534 above upper band (stretched)",
        "%B 80-100 \u2192 \U0001F7E2 strong, riding the upper band",
        "%B 20-80 \u2192 \U0001F7E1 middle of the range",
        "%B < 0 \u2192 \U0001F7E2 below lower band (oversold)",
    ],
    "cci": [
        "CCI \u2265 +100 \u2192 \U0001F534 overbought",
        "CCI 0 to +100 \u2192 \U0001F7E2 bullish momentum",
        "CCI -100 to 0 \u2192 \U0001F534 bearish momentum",
        "CCI \u2264 -100 \u2192 \U0001F7E2 oversold",
    ],
    "adx": [
        "ADX \u2265 40 \u2192 strong trend (trend trades work best)",
        "ADX 25-40 \u2192 trending \u00b7 ADX 20-25 \u2192 developing",
        "ADX < 20 \u2192 range-bound (avoid trend entries)",
        "+DI > -DI \u2192 \U0001F7E2 uptrend side \u00b7 -DI > +DI \u2192 \U0001F534 downtrend side",
    ],
    "aroon": [
        "Up \u2265 70 and above Down \u2192 \U0001F7E2 strong uptrend",
        "Down \u2265 70 and above Up \u2192 \U0001F534 strong downtrend",
        "Both high \u2192 choppy / ranging",
        "Aroon cross marks the start of a new trend",
    ],
    "psar": [
        "Dot below price \u2192 \U0001F7E2 uptrend - trail stop under the dot",
        "Dot above price \u2192 \U0001F534 downtrend - stay out",
        "Dot flip = trend reversal signal",
    ],
    "supertrend": [
        "Price above the line \u2192 \U0001F7E2 uptrend (buy dips)",
        "Price below the line \u2192 \U0001F534 downtrend (avoid)",
        "Line flip = trend change - the /scan500 rejection rule",
    ],
    "moving_average": [
        "Price above EMA200 \u2192 long-term uptrend intact",
        "Rising ladder EMA20 > 50 > 100 > 200 \u2192 clean uptrend",
        "SMA50 above SMA200 \u2192 \U0001F7E2 golden cross (bullish)",
        "SMA50 below SMA200 \u2192 \U0001F534 death cross (bearish)",
    ],
    "gmma": [
        "Short group above long group \u2192 \U0001F7E2 bullish",
        "Short group below long group \u2192 \U0001F534 bearish",
        "Groups intertwined \u2192 consolidation / no trend",
    ],
    "vwap": [
        "Price above anchored VWAP \u2192 \U0001F7E2 buyers in profit (support)",
        "Price below anchored VWAP \u2192 \U0001F534 sellers in control (resistance)",
        "Re-tests of VWAP often hold or reject",
    ],
    "atr": [
        "ATR \u2264 2% of price \u2192 \U0001F7E2 low volatility",
        "ATR 2-4% \u2192 \U0001F7E1 moderate (typical)",
        "ATR \u2265 6% \u2192 \U0001F534 very high - size positions down",
        "Use ~1.5x ATR as your stop distance",
    ],
    "donchian": [
        "\u226585% of range \u2192 \U0001F534 near 52W high (breakout or exhaustion)",
        "50-85% \u2192 \U0001F7E1 upper half (bullish)",
        "15-50% \u2192 \U0001F7E1 lower half (weak)",
        "\u226415% \u2192 \U0001F7E2 near 52W low (value zone)",
    ],
    "squeeze": [
        "Squeeze ON \u2192 \U0001F7E1 volatility compressed - breakout pending",
        "Squeeze OFF \u2192 \U0001F7E2 normal volatility, trend in progress",
        "Watch volume for the breakout confirmation",
    ],
    "cmf": [
        "CMF > +0.05 \u2192 \U0001F7E2 buying / accumulation",
        "CMF < -0.05 \u2192 \U0001F534 selling / distribution",
        "Between \u2192 \U0001F7E1 neutral",
    ],
    "mfi": [
        "MFI \u2265 80 \u2192 \U0001F534 overbought",
        "MFI > 55 \u2192 \U0001F7E2 accumulation",
        "MFI \u2264 20 \u2192 \U0001F7E2 oversold",
    ],
    "obv": [
        "OBV rising \u2192 \U0001F7E2 volume confirms the advance",
        "OBV falling while price rises \u2192 \U0001F534 warning (no buyers)",
        "OBV diverging from price is a leading sign of a turn",
    ],
}

_EMOJI = {
    "rsi": "\U0001F50C", "macd": "\U0001F52C", "stochastic": "\U0001F3C1",
    "bollinger": "\U0001F3B0", "cci": "\U0001F4C8", "adx": "\U0001F4CF",
    "aroon": "\U0001F3AF", "psar": "\U0001F7E1", "supertrend": "\U0001F4C8",
    "moving_average": "\u2696\ufe0f", "gmma": "\U0001F9ED", "vwap": "\u2696\ufe0f",
    "atr": "\U0001F4C9", "donchian": "\U0001F4C8", "squeeze": "\U0001F4A6",
    "cmf": "\U0001F4B5", "mfi": "\U0001F4B0", "obv": "\U0001F4CA",
}


def available_indicator_names() -> str:
    """Comma-separated list of indicator names for the usage hint."""
    return ", ".join(INDICATOR_KEYS)


def build_indicator_report(symbol: str, company: str, price, change_pct,
                           ohlc: dict, key: str, currency: str = "\u20b9") -> list[str]:
    """Full single-indicator deep-dive lines for Telegram (HTML).

    `ohlc` is the daily candle dict from sources.get_ohlc; `key` is a
    canonical indicator key (see match_indicator). Pure - no I/O here.
    """
    df = _df(ohlc)
    render = _RENDERERS[key]
    result = render(df)
    lines = []
    company_name = escape(company or symbol)
    symbol_text = escape(symbol)
    price_text = f"{currency}{price:,.2f}" if price is not None else "-"
    change_text = ""
    if change_pct is not None:
        arrow = "\u25b2" if change_pct >= 0 else "\u25bc"
        color = "\U0001F7E2" if change_pct >= 0 else "\U0001F534"
        change_text = f"  {color}{arrow} {change_pct:+.2f}%"
    lines.append(f"\U0001F4CA <b>{company_name}</b> (<code>{symbol_text}</code>)")
    lines.append(f"Price: <b>{price_text}</b>{change_text}  \u00b7  Daily candles")
    lines.append("")

    if result is None:
        lines.append(f"<i>{_LABELS[key]} \u2014 not enough history to compute.</i>")
        lines.append("")
        lines.append(f"\U0001F4A1 <i>Tip: /indicator {symbol_text} shows the full "
                     f"indicator card for this stock.</i>")
        return lines

    headline, emoji, rows, trend = result
    lines.append(f"{_EMOJI[key]} <b>{_LABELS[key]}</b> \u2014 <b>{headline}</b> {emoji}")
    for label, text, row_emoji in rows:
        suffix = f" {row_emoji}" if row_emoji else ""
        lines.append(f"  \u2022 {label}: <b>{text}</b>{suffix}")
    if trend:
        direction_emoji = "\U0001F7E2" if trend == "rising" else ("\U0001F53B" if trend == "falling" else "\U0001F7E1\u25b6")
        lines.append(f"  \u2022 5-session trend: {direction_emoji} {trend}")
    lines.append("")

    lines.append("\U0001F4A1 <b>What it means</b>")
    lines.append(_MEANING[key])
    lines.append("")
    lines.append("\U0001F4D6 <b>How to read the levels</b>")
    lines.extend(f"  \u2022 {line}" for line in _LEGENDS[key])
    lines.append("")
    lines.append(f"\U0001F4A1 <i>Tip: /indicator {symbol_text} for the full indicator "
                 f"card \u00b7 /fundamentalreport {symbol_text} for fundamentals.</i>")
    return lines
