"""技術指標。公式見各函式說明，實作為本倉庫原創，沒有複製其他專案原始碼。"""

from __future__ import annotations

import numpy as np

from ymzz.errors import ConfigError


def _as_float_array(values) -> np.ndarray:
    return np.asarray(values, dtype=float)


def sma(values, period: int) -> np.ndarray:
    """簡單移動平均。"""
    if period < 1:
        raise ConfigError("均線週期至少為 1")
    arr = _as_float_array(values)
    out = np.full(arr.shape, np.nan)
    if len(arr) < period:
        return out
    window = np.cumsum(arr)
    out[period - 1] = window[period - 1] / period
    if len(arr) > period:
        out[period:] = (window[period:] - window[:-period]) / period
    return out


def rolling_std(values, period: int) -> np.ndarray:
    """母體標準差（除以 N，ddof=0），與布林常用口徑一致。"""
    if period < 2:
        raise ConfigError("標準差週期至少為 2")
    arr = _as_float_array(values)
    out = np.full(arr.shape, np.nan)
    for i in range(period - 1, len(arr)):
        out[i] = float(np.std(arr[i - period + 1 : i + 1], ddof=0))
    return out


def ema(values, period: int) -> np.ndarray:
    """指數移動平均。第一個值用連續 period 根的 SMA 起算，其後係數 2/(period+1)。"""
    if period < 1:
        raise ConfigError("EMA 週期至少為 1")
    arr = _as_float_array(values)
    out = np.full(arr.shape, np.nan)
    run = 0
    start = None
    for i, value in enumerate(arr):
        if np.isfinite(value):
            run += 1
            if run >= period:
                start = i
                break
        else:
            run = 0
    if start is None:
        return out
    prev = float(np.mean(arr[start - period + 1 : start + 1]))
    out[start] = prev
    alpha = 2.0 / (period + 1.0)
    for i in range(start + 1, len(arr)):
        if not np.isfinite(arr[i]) or not np.isfinite(prev):
            prev = np.nan
            out[i] = np.nan
            continue
        prev = float(arr[i]) * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def rsi(values, period: int) -> np.ndarray:
    """Wilder RSI。

    漲跌用收盤差。第一個平均值是前 `period` 個漲跌的算術平均，
    其後 avg = (前值 * (period-1) + 本根) / period。
    平均跌幅為 0 且漲幅大於 0 時 RSI 為 100；兩者都是 0 時為 50。
    文獻：J. Welles Wilder Jr., New Concepts in Technical Trading Systems, 1978。
    """
    if period < 2:
        raise ConfigError("RSI 週期至少為 2")
    arr = _as_float_array(values)
    out = np.full(arr.shape, np.nan)
    if len(arr) <= period:
        return out
    delta = np.diff(arr, prepend=np.nan)
    gain = np.where(np.isnan(delta), np.nan, np.maximum(delta, 0.0))
    loss = np.where(np.isnan(delta), np.nan, np.maximum(-delta, 0.0))
    avg_gain = float(np.mean(gain[1 : period + 1]))
    avg_loss = float(np.mean(loss[1 : period + 1]))

    def _value(avg_g: float, avg_l: float) -> float:
        if avg_g == 0.0 and avg_l == 0.0:
            return 50.0
        if avg_l == 0.0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _value(avg_gain, avg_loss)
    for i in range(period + 1, len(arr)):
        avg_gain = (avg_gain * (period - 1) + float(gain[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(loss[i])) / period
        out[i] = _value(avg_gain, avg_loss)
    return out


def macd(values, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD。DIF = EMA(fast) - EMA(slow)，DEA = DIF 的 EMA(signal)，柱 = DIF - DEA。

    柱狀圖是差值，不是第三個獨立決策輸入。Gerald Appel 的定義；EMA 以 SMA 起算。
    """
    if fast < 1 or slow < 2 or signal < 1 or fast >= slow:
        raise ConfigError("MACD 需要 1 ≤ 快線 < 慢線，且訊號週期 ≥ 1")
    dif = ema(values, fast) - ema(values, slow)
    dea = ema(dif, signal)
    hist = dif - dea
    return dif, dea, hist


def atr(highs, lows, closes, period: int):
    """真實波幅的簡單平均。本根高低，以及相對前一根收盤的跳空，取最大，再做 SMA。

    只用已經發生的價格。週期未滿的位置是空值。
    """
    if period < 1:
        raise ConfigError("ATR 週期至少為 1")
    high = _as_float_array(highs)
    low = _as_float_array(lows)
    close = _as_float_array(closes)
    if not (len(high) == len(low) == len(close)):
        raise ConfigError("高低收的長度必須一樣")
    tr = np.empty(len(close), dtype=float)
    for i in range(len(close)):
        span = float(high[i] - low[i])
        if i == 0:
            tr[i] = span
            continue
        tr[i] = max(span, abs(float(high[i] - close[i - 1])), abs(float(low[i] - close[i - 1])))
    return sma(tr, period)


def bollinger(values, period: int = 20, mult: float = 2.0):
    """布林通道。中軌為 SMA，上下軌為中軌 ± mult * 母體標準差。John Bollinger。"""
    if period < 2 or mult <= 0:
        raise ConfigError("布林週期至少為 2，倍數必須為正")
    mid = sma(values, period)
    std = rolling_std(values, period)
    upper = mid + mult * std
    lower = mid - mult * std
    return mid, upper, lower
