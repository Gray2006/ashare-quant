"""因子库：输入为 Store.panel() 的宽表字典，输出为 date × code 的因子值。

方向约定：direction=1 表示数值越大越好，-1 表示越小越好。
所有因子只用当日及之前的数据，避免未来函数。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from . import indicators


@dataclass(frozen=True)
class Factor:
    name: str
    label: str
    category: str
    direction: int
    lookback: int
    description: str
    func: Callable[[dict[str, pd.DataFrame]], pd.DataFrame]
    historical: bool = True


def _close(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return panel["close"]


def _returns(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if "pct_chg" in panel:
        return panel["pct_chg"] / 100.0
    return _close(panel).pct_change()


def momentum(window: int):
    def calc(panel):
        return _close(panel) / _close(panel).shift(window) - 1.0
    return calc


def reversal(window: int):
    def calc(panel):
        return -(_close(panel) / _close(panel).shift(window) - 1.0)
    return calc


def volatility(panel, window: int = 20):
    return _returns(panel).rolling(window, min_periods=window // 2).std() * np.sqrt(252)


def turnover_mean(panel, window: int = 20):
    return panel["turnover"].rolling(window, min_periods=window // 2).mean()


def amount_mean(panel, window: int = 20):
    return panel["amount"].rolling(window, min_periods=window // 2).mean()


def ma_gap(panel, window: int = 20):
    close = _close(panel)
    return close / indicators.sma(close, window) - 1.0


def rsi_latest(panel, window: int = 14):
    return indicators.rsi(_close(panel), window)


def macd_hist(panel):
    _, _, hist = indicators.macd(_close(panel))
    return hist / _close(panel)


def atr_pct(panel, window: int = 14):
    close = _close(panel)
    value = indicators.atr(panel["high"], panel["low"], close, window)
    return value / close


def boll_pos(panel, window: int = 20):
    close = _close(panel)
    mid = indicators.sma(close, window)
    std = close.rolling(window, min_periods=window // 2).std()
    return (close - mid) / (2 * std)


def volume_ratio(panel, short: int = 5, long: int = 20):
    volume = panel["volume"]
    return (volume.rolling(short, min_periods=short).mean()
            / volume.rolling(long, min_periods=long // 2).mean())


def high_52w(panel, window: int = 250):
    close = _close(panel)
    return close / close.rolling(window, min_periods=60).max() - 1.0


def amihud(panel, window: int = 20):
    """非流动性：|收益| / 成交额，越大越不流动。"""
    close = _close(panel)
    ret = close.pct_change().abs()
    amount = panel["amount"].replace(0.0, np.nan)
    return (ret / amount).rolling(window, min_periods=window // 2).mean() * 1e9


FACTORS: dict[str, Factor] = {
    "momentum_20": Factor("momentum_20", "20日动量", "动量", 1, 20,
                          "过去 20 个交易日涨幅", momentum(20)),
    "momentum_60": Factor("momentum_60", "60日动量", "动量", 1, 60,
                          "过去 60 个交易日涨幅", momentum(60)),
    "momentum_120": Factor("momentum_120", "120日动量", "动量", 1, 120,
                           "过去 120 个交易日涨幅", momentum(120)),
    "reversal_5": Factor("reversal_5", "5日反转", "反转", 1, 5,
                         "过去 5 个交易日跌幅（取负）", reversal(5)),
    "volatility_20": Factor("volatility_20", "20日波动率", "风险", -1, 20,
                            "年化波动率，低波动更稳", volatility),
    "turnover_20": Factor("turnover_20", "20日换手率", "流动性", -1, 20,
                          "平均换手率，过高往往意味着拥挤", turnover_mean),
    "amount_20": Factor("amount_20", "20日成交额", "流动性", 1, 20,
                        "平均成交额，衡量可交易容量", amount_mean),
    "ma_gap_20": Factor("ma_gap_20", "20日均线偏离", "趋势", 1, 20,
                        "收盘价相对 20 日均线的偏离度", ma_gap),
    "rsi_14": Factor("rsi_14", "RSI(14)", "超买超卖", -1, 14,
                     "相对强弱指标，过高视为超买", rsi_latest),
    "macd_hist": Factor("macd_hist", "MACD柱", "趋势", 1, 26,
                        "MACD 柱值除以收盘价", macd_hist),
    "atr_pct_14": Factor("atr_pct_14", "ATR占比", "风险", -1, 14,
                         "真实波幅占价格比例", atr_pct),
    "boll_pos_20": Factor("boll_pos_20", "布林位置", "超买超卖", -1, 20,
                          "价格在布林带中的相对位置", boll_pos),
    "volume_ratio_5_20": Factor("volume_ratio_5_20", "量比(5/20)", "量价", 1, 20,
                                "5 日均量除以 20 日均量", volume_ratio),
    "high_52w": Factor("high_52w", "距一年高点", "趋势", 1, 250,
                       "收盘价相对 250 日最高价的位置", high_52w),
    "amihud_20": Factor("amihud_20", "非流动性", "流动性", -1, 20,
                        "Amihud 非流动性，越大越难成交", amihud),
}


def list_factors() -> list[dict]:
    return [
        {
            "name": f.name,
            "label": f.label,
            "category": f.category,
            "direction": f.direction,
            "lookback": f.lookback,
            "description": f.description,
            "historical": f.historical,
        }
        for f in FACTORS.values()
    ]


def compute(name: str, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if name not in FACTORS:
        raise KeyError(f"未知因子: {name}；可用: {', '.join(FACTORS)}")
    return FACTORS[name].func(panel)


def latest(name: str, panel: dict[str, pd.DataFrame]) -> pd.Series:
    frame = compute(name, panel)
    if frame.empty:
        return pd.Series(dtype=float)
    return frame.iloc[-1].dropna()


def winsorize(frame: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame.clip(
        lower=frame.quantile(lower, axis=1),
        upper=frame.quantile(upper, axis=1),
        axis=0,
    )


def zscore(frame: pd.DataFrame) -> pd.DataFrame:
    mean = frame.mean(axis=1)
    std = frame.std(axis=1).replace(0.0, np.nan)
    return frame.sub(mean, axis=0).div(std, axis=0)


def rank_pct(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True)


def normalize(frame: pd.DataFrame, method: str = "zscore") -> pd.DataFrame:
    if method == "rank":
        return rank_pct(frame)
    if method == "zscore":
        return zscore(winsorize(frame))
    return frame
