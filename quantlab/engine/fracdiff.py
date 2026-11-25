# -*- coding: utf-8 -*-
"""分数阶差分(Fractional Differentiation) —— 蒸馏自 López de Prado。

这是「微积分对量化有什么用」最实在的答案。

问题:价格序列不平稳(均值方差随时间变),不能直接回归;一阶差分
(收益率)平稳了,但**记忆全丢了** —— 昨天的涨跌对今天的影响被抹掉。

分数阶差分取中间:d ∈ (0,1),既让序列近似平稳,又保留长记忆。
d=0 是原序列(不平稳、记忆全在),d=1 是收益率(平稳、记忆全丢)。

实现:二项式展开的权重 w_k = -w_{k-1} * (d - k + 1) / k,
权重衰减到阈值以下就截断(否则要算到无穷)。
"""
from __future__ import annotations

import numpy as np


def frac_weights(d: float, threshold: float = 1e-4, max_len: int = 500) -> np.ndarray:
    """分数阶差分的卷积权重。"""
    w = [1.0]
    for k in range(1, max_len):
        val = -w[-1] * (d - k + 1.0) / k
        if abs(val) < threshold:
            break                      # 先判再收:否则会留下接近 0 的多余权重
        w.append(val)
    return np.array(w, dtype="float64")


def frac_diff(series, d: float, threshold: float = 1e-4) -> np.ndarray:
    """对一维序列做分数阶差分,返回与输入等长的数组(前段为 NaN)。"""
    x = np.asarray(series, dtype="float64")
    w = frac_weights(d, threshold)
    n, m = len(x), len(w)
    out = np.full(n, np.nan)
    if n < m:
        return out
    for i in range(m - 1, n):
        seg = x[i - m + 1:i + 1][::-1]      # 最近的在前
        if not np.all(np.isfinite(seg)):
            continue
        out[i] = float(np.dot(w, seg))
    return out


def frac_diff_panel(panel: dict, d: float = 0.4, threshold: float = 1e-4) -> dict:
    """对整个 close 面板做分数阶差分,返回同形状的 DataFrame。"""
    import pandas as pd

    close = panel.get("close")
    if close is None or close.empty:
        return {}
    cols = {}
    for code in close.columns:
        cols[code] = pd.Series(frac_diff(close[code].to_numpy(), d, threshold),
                               index=close.index)
    return {"close": pd.DataFrame(cols)}


def best_d(series, d_range=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
           threshold: float = 1e-4, alpha: float = 0.05) -> float:
    """找最小的 d 让序列通过 ADF 平稳性检验。

    没有 statsmodels,用简化判据:自相关函数衰减到 0.05 以下的滞后阶数。
    平稳序列的 ACF 衰减快。
    """
    x = np.asarray(series, dtype="float64")
    x = x[np.isfinite(x)]
    if len(x) < 50:
        return 1.0

    def acf_decay(arr) -> int:
        a = arr - arr.mean()
        var = float(np.dot(a, a))
        if var <= 0:
            return 0
        for k in range(1, min(40, len(a) // 2)):
            c = float(np.dot(a[:-k], a[k:])) / var
            if abs(c) < 0.05:
                return k
        return 40

    for d in d_range:
        y = frac_diff(x, d, threshold)
        y = y[np.isfinite(y)]
        if len(y) < 50:
            continue
        if acf_decay(y) <= 5:          # 5 阶内衰减完,当平稳
            return float(d)
    return 1.0
