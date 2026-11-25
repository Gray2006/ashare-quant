# -*- coding: utf-8 -*-
"""概念漂移检测(简化版 DDG-DA) —— 蒸馏自 qlib 的 DDG-DA。

问题:我们一直假设「因子有效性恒定」,用全样本算一个 IC。
但市场风格会变 —— 2023 年的动量因子在 2025 年可能反向。

DDG-DA 的做法:检测分布漂移,漂移了就用近期数据重训。
这里只做**检测**部分(不重训),因为我们的问题是「有没有信号」,
不是「信号什么时候失效」。

三种检测:
  1 滚动 IC 的符号翻转   —— 最直观,因子从赚变亏
  2 CUSUM 累积和         —— 检测均值突变点
  3 分段稳定性           —— 每段 IC 是否有同号
"""
from __future__ import annotations

import numpy as np


def rolling_ic(ic: list[float], window: int = 40) -> np.ndarray:
    """滚动均值,用来观察 IC 的方向有没有变。"""
    x = np.asarray(ic, dtype="float64")
    if len(x) < window:
        return np.array([])
    return np.convolve(x, np.ones(window) / window, mode="valid")


def sign_flips(ic: list[float], window: int = 40) -> int:
    """滚动 IC 穿过 0 的次数。越多说明因子越不稳定。"""
    r = rolling_ic(ic, window)
    if len(r) < 2:
        return 0
    s = np.sign(r)
    s = s[s != 0]
    return int(np.sum(np.diff(s) != 0)) if len(s) > 1 else 0


def cusum_break(ic: list[float], threshold: float = 5.0,
                slack: float = 0.5) -> int:
    """标准 CUSUM(双向累积和),检测均值突变,返回突变点下标(没有则 -1)。

    s_pos = max(0, s_pos + z - slack)
    s_neg = min(0, s_neg + z + slack)
    任一超过 ±threshold 就报。slack 是允许的漂移量(标准差的倍数)。

    原来用「累积和偏离整体均值」,正常波动也会触发 —— 稳定序列反而更早报警。
    """
    x = np.asarray(ic, dtype="float64")
    if len(x) < 20:
        return -1
    sd = float(np.std(x, ddof=1))
    if sd <= 0:
        return -1
    z = (x - float(np.mean(x))) / sd
    s_pos = s_neg = 0.0
    for i, v in enumerate(z):
        s_pos = max(0.0, s_pos + float(v) - slack)
        s_neg = min(0.0, s_neg + float(v) + slack)
        if s_pos > threshold or s_neg < -threshold:
            return int(i)
    return -1


def segment_stability(ic: list[float], n_seg: int = 3) -> dict:
    """把 IC 序列切 n 段,看每段均值和符号。"""
    x = np.asarray(ic, dtype="float64")
    n = len(x)
    if n < n_seg * 10:
        return {"ok": False, "msg": "样本太少"}
    segs = [x[i * n // n_seg:(i + 1) * n // n_seg] for i in range(n_seg)]
    means = [float(np.mean(s)) for s in segs]
    same = sum(1 for m in means if m * means[0] > 0)
    return {
        "ok": True,
        "means": [round(m, 4) for m in means],
        "same_sign": same,
        "all_same": same == n_seg,
        "spread": round(max(means) - min(means), 4),
        "verdict": ("三段同号,稳定" if same == n_seg
                    else f"只有 {same}/{n_seg} 段同号,不稳定"),
    }


def drift_report(ic: list[float], window: int = 40) -> dict:
    """综合检测:符号翻转 + CUSUM + 分段稳定性。"""
    return {
        "n": len(ic),
        "sign_flips": sign_flips(ic, window),
        "cusum_break": cusum_break(ic),
        "segments": segment_stability(ic),
    }
