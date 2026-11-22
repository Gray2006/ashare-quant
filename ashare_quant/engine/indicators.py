"""常用技术指标，全部基于 pandas，输入输出都保持索引对齐。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(2, window // 2)).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=max(2, span // 2)).mean()


def roc(series: pd.Series, window: int) -> pd.Series:
    return series / series.shift(window) - 1.0


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # 只涨不跌 → 100；完全不动 → 50；只跌不涨 → 0
    out = out.mask((avg_loss == 0) & (avg_gain > 0), 100.0)
    out = out.mask((avg_loss == 0) & (avg_gain == 0), 50.0)
    return out


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = ema(series, fast)
    slow_ema = ema(series, slow)
    dif = fast_ema - slow_ema
    dea = ema(dif, signal)
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(series, window)
    std = series.rolling(window, min_periods=window // 2).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid.replace(0.0, np.nan)
    return upper, mid, lower, width


def rolling_vol(returns: pd.Series, window: int = 20, periods: int = 252) -> pd.Series:
    return returns.rolling(window, min_periods=max(5, window // 2)).std() * np.sqrt(periods)


def max_drawdown(equity: pd.Series) -> float:
    equity = equity.dropna()
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


def drawdown_series(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1.0


def compute_all(bars: pd.DataFrame) -> pd.DataFrame:
    """给单只股票的日线补上常用指标列。"""
    frame = bars.copy()
    close = frame["close"]
    frame["ma5"] = sma(close, 5)
    frame["ma10"] = sma(close, 10)
    frame["ma20"] = sma(close, 20)
    frame["ma60"] = sma(close, 60)
    frame["rsi14"] = rsi(close, 14)
    dif, dea, hist = macd(close)
    frame["macd_dif"] = dif
    frame["macd_dea"] = dea
    frame["macd_hist"] = hist
    frame["atr14"] = atr(frame["high"], frame["low"], close, 14)
    frame["atr_pct"] = frame["atr14"] / close
    upper, mid, lower, width = bollinger(close)
    frame["boll_upper"] = upper
    frame["boll_mid"] = mid
    frame["boll_lower"] = lower
    frame["boll_width"] = width
    frame["vol20"] = frame["volume"].rolling(20, min_periods=10).mean()
    frame["vol_ratio"] = frame["volume"] / frame["vol20"]
    frame["ret"] = close.pct_change()
    frame["volatility20"] = rolling_vol(frame["ret"], 20)
    return frame
