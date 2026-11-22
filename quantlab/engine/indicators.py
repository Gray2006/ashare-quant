"""技术指标。全部函数都是「只用当根及之前数据」的因果实现。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=1).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9,
        m1: int = 3, m2: int = 3):
    ll = low.rolling(n, min_periods=1).min()
    hh = high.rolling(n, min_periods=1).max()
    rsv = (close - ll) / (hh - ll).replace(0, np.nan) * 100
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def boll(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = ma(close, n)
    std = close.rolling(n, min_periods=1).std(ddof=0)
    return mid + k * std, mid, mid - k * std


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def ret(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1


def volatility(close: pd.Series, n: int = 20, annual: int = 252) -> pd.Series:
    return close.pct_change().rolling(n, min_periods=n // 2).std(ddof=0) * np.sqrt(annual)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """给 OHLCV DataFrame 批量补常用指标列。

    输入列: open/high/low/close/volume
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    c, h, l, v = out["close"], out["high"], out["low"], out["volume"]
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = ma(c, n)
    out["dif"], out["dea"], out["macd"] = macd(c)
    out["k"], out["d"], out["j"] = kdj(h, l, c)
    out["rsi6"] = rsi(c, 6)
    out["rsi14"] = rsi(c, 14)
    out["boll_up"], out["boll_mid"], out["boll_low"] = boll(c)
    out["atr14"] = atr(h, l, c)
    out["vol_ma5"] = ma(v, 5)
    out["vol_ma20"] = ma(v, 20)
    out["ret5"] = ret(c, 5)
    out["ret20"] = ret(c, 20)
    out["ret60"] = ret(c, 60)
    out["vol20"] = volatility(c, 20)
    return out


def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    """东财 K 线 list[dict] → DataFrame(时间升序,datetime 列 + 索引)。"""
    if not bars:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "amount"])
    df = pd.DataFrame(bars)
    keep = ["datetime", "open", "high", "low", "close", "volume", "amount"]
    # 换手率留着:通达信 CAPITAL(流通股本)要靠 volume / 换手率 反推
    keep += [k for k in ("turnover", "float_cap", "market_cap") if k in df.columns]
    for k in keep:
        if k not in df.columns:
            df[k] = np.nan
    df = df[keep].copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df
