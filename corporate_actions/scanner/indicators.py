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
        value = series.iloc[-1]
        return None if (value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))) else float(value)
    except Exception:
        return default


def rolling_last(series, window, function):
    """Apply a rolling function and return only the final value."""
    if series is None or series.empty:
        return None
    vals = series.values
    count = len(vals)
    if count < window:
        return None
    return float(function(vals[count - window:count]))


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(close, period=14):
    delta = close.diff()
    avg_gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False).mean()
    relative_strength = avg_gain / avg_loss.replace(0.0, 1e-9)
    return 100.0 - 100.0 / (1.0 + relative_strength)


def true_range(df):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(df, period=14):
    true_range_series = true_range(df)
    return true_range_series.ewm(alpha=1.0 / period, adjust=False).mean()


def adx(df, period=14):
    true_range_series = true_range(df)
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr_series = true_range_series.ewm(alpha=1.0 / period, adjust=False).mean().replace(0.0, 1e-9)
    positive_direction_index = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_series
    negative_direction_index = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_series
    directional_index = 100.0 * (positive_direction_index - negative_direction_index).abs() / (positive_direction_index + negative_direction_index).replace(0.0, 1e-9)
    adx_series = directional_index.ewm(alpha=1.0 / period, adjust=False).mean()
    return positive_direction_index, negative_direction_index, adx_series


def cmf(df, period=20):
    high_low_range = (df["high"] - df["low"]).replace(0.0, 1e-9)
    money_flow_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / high_low_range
    money_flow_volume = money_flow_multiplier * df["volume"]
    return money_flow_volume.rolling(period).sum() / df["volume"].rolling(period).sum().replace(0.0, 1e-9)


def mfi(df, period=14):
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    money_flow = typical_price * df["volume"]
    delta = typical_price.diff()
    positive_flow = money_flow.where(delta > 0, 0.0).rolling(period).sum()
    negative_flow = money_flow.where(delta < 0, 0.0).rolling(period).sum()
    return 100.0 - 100.0 / (1.0 + positive_flow / negative_flow.replace(0.0, 1e-9))


def obv(df):
    sign = np.sign(df["close"].diff()).fillna(0.0)
    return (sign * df["volume"]).cumsum()


def aroon(df, period=25):
    window = period + 1
    def _position_of_high(vals):
        return int(vals.argmax())

    def _position_of_low(vals):
        return int(vals.argmin())

    high_pos = df["high"].rolling(window).apply(_position_of_high, raw=True)
    low_pos = df["low"].rolling(window).apply(_position_of_low, raw=True)
    aroon_up = 100.0 * (window - 1 - high_pos) / period
    aroon_down = 100.0 * (window - 1 - low_pos) / period
    return aroon_up, aroon_down


def supertrend(df, period=10, multiplier=3.0):
    """Supertrend over a DataFrame with OHLC. Returns (direction_df, line_df)."""
    high, low, close = df["high"], df["low"], df["close"]
    atr_series = atr(df, period)
    midpoint = (high + low) / 2.0
    upper_band = midpoint + multiplier * atr_series
    lower_band = midpoint - multiplier * atr_series
    bar_count = len(df)
    supertrend_line = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index)
    if bar_count == 0:
        return direction, supertrend_line
    previous_supertrend = None
    for index in range(bar_count):
        upper_band_value = upper_band.iloc[index]
        lower_band_value = lower_band.iloc[index]
        prev_up = upper_band.iloc[index - 1] if index > 0 else upper_band_value
        prev_dn = lower_band.iloc[index - 1] if index > 0 else lower_band_value
        current_close = close.iloc[index]
        if previous_supertrend is None or math.isnan(previous_supertrend):
            supertrend_line.iloc[index] = upper_band_value
            direction.iloc[index] = 1
        else:
            if previous_supertrend == prev_up:
                direction.iloc[index] = -1 if current_close < prev_up else 1
            else:
                direction.iloc[index] = 1 if current_close > prev_dn else -1
            if direction.iloc[index] == 1:
                supertrend_line.iloc[index] = max(lower_band_value, previous_supertrend) if not math.isnan(previous_supertrend) else lower_band_value
            else:
                supertrend_line.iloc[index] = min(upper_band_value, previous_supertrend) if not math.isnan(previous_supertrend) else upper_band_value
        previous_supertrend = supertrend_line.iloc[index]
    return direction, supertrend_line


def weekly_df(daily):
    """Resample daily OHLC to weekly bars (week ending Friday)."""
    weekly_frame = pd.DataFrame({
        "open": daily["open"], "high": daily["high"],
        "low": daily["low"], "close": daily["close"], "volume": daily["volume"],
    })
    if "date_time" in daily.columns:
        weekly_frame["date_time"] = daily["date_time"]
    else:
        weekly_frame["date_time"] = pd.to_datetime(daily["timestamp"], unit="s", utc=True)
    weekly_frame = weekly_frame.set_index("date_time")
    weekly_result = weekly_frame.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["open", "high", "low", "close"])
    return weekly_result


def anchored_vwap(df, lookback=252):
    """VWAP anchored at the lowest low over the last `lookback` bars."""
    subset = df.tail(lookback)
    anchor_label = subset["low"].idxmin()
    anchor_pos = df.index.get_loc(anchor_label)
    segment = df.iloc[anchor_pos:]
    typical_price = (segment["high"] + segment["low"] + segment["close"]) / 3.0
    total_value = (typical_price * segment["volume"]).sum()
    volume = segment["volume"].sum()
    return float(total_value / volume) if volume > 0 else None


def mansfield_relative_strength(sym_close, idx_close):
    """Mansfield relative strength vs an index benchmark."""
    sym_close = pd.Series(sym_close) if not isinstance(sym_close, pd.Series) else sym_close
    idx_close = pd.Series(idx_close) if not isinstance(idx_close, pd.Series) else idx_close
    count = min(200, len(sym_close), len(idx_close))
    if count < 120:
        return None
    sym_values = sym_close.values[-count:]
    idx_values = idx_close.values[-count:]
    relative_strength = pd.Series(sym_values / idx_values)
    sma20 = relative_strength.rolling(20).mean()
    last_sma = safe_last(sma20)
    if not last_sma:
        return None
    return (float(relative_strength.iloc[-1]) / last_sma - 1.0) * 100.0


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
    signal_line = ema(line, 9)
    histogram = line - signal_line
    return safe_last(line), safe_last(signal_line), safe_last(histogram)


def obv_trend(df):
    try:
        obv_series = obv(df)
        last = float(obv_series.iloc[-1])
        previous_20 = float(obv_series.iloc[-21]) if len(obv_series) > 21 else float(obv_series.iloc[0])
        return "rising" if last >= previous_20 else "falling"
    except Exception:
        return None


def delivery_proxy(cmf, mfi):
    """Estimate delivery participation from money-flow measures (0-100)."""
    if cmf is None or mfi is None:
        return None
    estimate = 30.0 + cmf * 25.0 + (mfi - 50.0) * 0.25
    return round(max(0.0, min(100.0, estimate)), 1)


def ttm_squeeze(df, atr_series):
    try:
        std_dev = df["close"].rolling(20).std()
        bollinger_width = 4.0 * std_dev
        keltner_width = 3.0 * atr_series
        last_bollinger_width = safe_last(bollinger_width)
        last_keltner_width = safe_last(keltner_width)
        if last_bollinger_width is None or last_keltner_width is None:
            return None
        return bool(last_bollinger_width < last_keltner_width)
    except Exception:
        return None


def bb_position(df):
    try:
        middle = df["close"].rolling(20).mean()
        std_dev = df["close"].rolling(20).std()
        upper, lower = middle + 2 * std_dev, middle - 2 * std_dev
        upper, lower, middle = safe_last(upper), safe_last(lower), safe_last(middle)
        if upper is None or lower is None or upper == lower:
            return None
        return (df["close"].iloc[-1] - lower) / (upper - lower) * 100.0
    except Exception:
        return None


def gmma(close):
    """Guppy Multiple Moving Average: short group above long group = bullish."""
    short = [safe_last(ema(close, period)) for period in (3, 5, 8, 10, 12, 15)]
    long = [safe_last(ema(close, period)) for period in (30, 35, 40, 45, 50, 60)]
    if any(value is None for value in short + long):
        return None
    return bool(min(short) > max(long))


def daily_traded_value_crore(df):
    """Average daily traded value in ₹ crore over the last 20 sessions."""
    try:
        last20 = df.tail(20)
        value = (last20["close"] * last20["volume"]).mean()
        return round(value / 1e7, 2)  # 1 crore = 10^7
    except Exception:
        return None


def stochastic(df, period=14, smooth_k=3, smooth_d=3):
    """Stochastic oscillator %K / %D (0-100) as (last_k, last_d)."""
    lowest = df["low"].rolling(period).min()
    highest = df["high"].rolling(period).max()
    raw_k = 100.0 * (df["close"] - lowest) / (highest - lowest).replace(0.0, 1e-9)
    k = raw_k.rolling(smooth_k).mean()
    d = k.rolling(smooth_d).mean()
    return safe_last(k), safe_last(d)


def bollinger_bands(df, period=20, num_std=2):
    """Bollinger upper/mid/lower bands + %B position (0-100)."""
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    percent_b = 100.0 * (df["close"] - lower) / (upper - lower).replace(0.0, 1e-9)
    return safe_last(upper), safe_last(mid), safe_last(lower), safe_last(percent_b)


def cci(df, period=20):
    """Commodity Channel Index (typical price vs mean deviation, /0.015)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    mean = typical.rolling(period).mean()
    mean_dev = typical.rolling(period).apply(
        lambda values: float(np.abs(values - values.mean()).mean()), raw=True,
    )
    return safe_last((typical - mean) / mean_dev.replace(0.0, 1e-9) / 0.015)


def psar_direction(df, step=0.02, max_step=0.2):
    """Parabolic SAR trend: 'bull' when the SAR rides below price, 'bear' above."""
    high, low = df["high"].values, df["low"].values
    count = len(df)
    if count < 3:
        return None
    bull = high[1] >= high[0]
    extreme = high[0] if bull else low[0]
    sar = low[0] if bull else high[0]
    acceleration = step
    for index in range(1, count):
        if bull:
            sar = sar + acceleration * (extreme - sar)
            sar = min(sar, low[index - 1]) if index == 1 else min(sar, low[index - 1], low[index - 2])
            if high[index] > extreme:
                extreme = high[index]
                acceleration = min(acceleration + step, max_step)
            if low[index] < sar:
                bull, sar, extreme, acceleration = False, extreme, low[index], step
        else:
            sar = sar + acceleration * (extreme - sar)
            sar = max(sar, high[index - 1]) if index == 1 else max(sar, high[index - 1], high[index - 2])
            if low[index] < extreme:
                extreme = low[index]
                acceleration = min(acceleration + step, max_step)
            if high[index] > sar:
                bull, sar, extreme, acceleration = True, extreme, high[index], step
    return "bull" if bull else "bear"


def volume_ratio(df, lookback=20):
    """Latest bar volume vs the previous `lookback`-bar average (1.0 = average)."""
    if len(df) < lookback + 1:
        return None
    average = float(df["volume"].tail(lookback).mean())
    if average <= 0:
        return None
    return float(df["volume"].iloc[-1]) / average
