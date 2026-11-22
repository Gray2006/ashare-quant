# -*- coding: utf-8 -*-
"""组合优化:风险平价 / 均值方差 / 最大分散化。

对标市面量化的组合层:
  风险平价   —— 每个资产对组合风险的贡献相等,不押单一资产
  均值方差   —— 预期收益 / 协方差,经典 Markowitz
  最大分散化 —— 最大化分散化比率

只算权重,不下单。权重怎么用由调用方决定。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _cov(returns: pd.DataFrame) -> np.ndarray:
    cov = returns.cov().values
    # 正则化:对角线加小量,防止协方差奇异导致求逆爆炸
    return cov + np.eye(len(cov)) * 1e-8


def risk_parity(returns: pd.DataFrame, iters: int = 300) -> dict[str, float]:
    """风险平价:每个资产对组合风险的贡献相等。"""
    if returns is None or returns.empty or returns.shape[1] < 2:
        return {}
    cov = _cov(returns)
    n = cov.shape[0]
    w = np.ones(n) / n
    for _ in range(iters):
        port_var = float(w @ cov @ w)
        if port_var <= 0:
            break
        mrc = cov @ w / np.sqrt(port_var)
        rc = w * mrc
        target = port_var / n
        w = w * np.sqrt(target / np.maximum(rc, 1e-12))
        w = w / w.sum()
    return {c: round(float(x), 4) for c, x in zip(returns.columns, w)}


def mean_variance(returns: pd.DataFrame, risk_aversion: float = 1.0) -> dict[str, float]:
    """均值方差:max w'μ - λ/2 w'Σw,无卖空约束下解析解。"""
    if returns is None or returns.empty or returns.shape[1] < 2:
        return {}
    mu = returns.mean().values
    cov = _cov(returns)
    try:
        w = np.linalg.solve(cov, mu) / max(risk_aversion, 1e-9)
    except np.linalg.LinAlgError:
        return {}
    w = np.maximum(w, 0.0)          # 不做空
    s = w.sum()
    if s <= 0:
        return {}
    w = w / s
    return {c: round(float(x), 4) for c, x in zip(returns.columns, w)}


def max_diversification(returns: pd.DataFrame) -> dict[str, float]:
    """最大分散化:max w'σ / sqrt(w'Σw)。"""
    if returns is None or returns.empty or returns.shape[1] < 2:
        return {}
    cov = _cov(returns)
    sigma = np.sqrt(np.diag(cov))
    try:
        w = np.linalg.solve(cov, sigma)
    except np.linalg.LinAlgError:
        return {}
    w = np.maximum(w, 0.0)
    s = w.sum()
    if s <= 0:
        return {}
    w = w / s
    return {c: round(float(x), 4) for c, x in zip(returns.columns, w)}


def cap_weights(weights: dict[str, float], max_single: float = 0.25) -> dict[str, float]:
    """单票权重上限:超过的部分匀给其他票。"""
    if not weights:
        return {}
    w = dict(weights)
    n = len(w)
    if max_single * n < 1:
        max_single = 1.0 / n
    for _ in range(10):
        excess = 0.0
        for k, v in w.items():
            if v > max_single:
                excess += v - max_single
                w[k] = max_single
        if excess <= 1e-9:
            break
        free = [k for k, v in w.items() if v < max_single - 1e-9]
        if not free:
            break
        free_sum = sum(w[k] for k in free)
        for k in free:
            w[k] += (excess * (w[k] / free_sum) if free_sum > 0
                     else excess / len(free))
    s = sum(w.values())
    return {k: round(v / s, 4) for k, v in w.items()} if s > 0 else weights
