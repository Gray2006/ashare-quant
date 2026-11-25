# -*- coding: utf-8 -*-
"""HRP(Hierarchical Risk Parity) —— 蒸馏自 López de Prado。

为什么不用马科维茨:它要**求协方差矩阵的逆**,而 N 只票的协方差矩阵
在样本不足时是病态的,求逆会把噪声放大成极端权重(半个仓位压在一只票上)。

HRP 绕开求逆:
  1. 相关性转距离 d = sqrt((1-ρ)/2)
  2. 层次聚类(单链接)把相近的票先并成簇
  3. 准对角化:让相近的票在序列里挨着
  4. 递归二分:每个簇按簇内方差反比分配权重

不需要 scipy —— 单链接聚类用并查集手写。
"""
from __future__ import annotations

import numpy as np


def corr_to_dist(corr: np.ndarray) -> np.ndarray:
    """相关性 → 距离矩阵。"""
    d = np.sqrt(np.clip((1.0 - corr) / 2.0, 0.0, 1.0))
    np.fill_diagonal(d, 0.0)
    return d


def _single_linkage(dist: np.ndarray) -> list[tuple[int, int, float]]:
    """单链接层次聚类,返回合并顺序 [(a, b, 距离)]。"""
    n = dist.shape[0]
    clusters = {i: [i] for i in range(n)}
    order = []
    d = dist.copy()
    active = set(range(n))
    while len(active) > 1:
        best = None
        for a in active:
            for b in active:
                if a >= b:
                    continue
                if best is None or d[a, b] < best[2]:
                    best = (a, b, float(d[a, b]))
        if best is None:
            break
        a, b, dv = best
        order.append((a, b, dv))
        # 合并 b 到 a:单链接取最小距离
        for k in active:
            if k in (a, b):
                continue
            v = min(d[a, k], d[b, k])
            d[a, k] = d[k, a] = v
        clusters[a].extend(clusters.pop(b))
        active.discard(b)
    return order


def quasi_diag(order: list[tuple[int, int, float]], n: int) -> list[int]:
    """按合并顺序把票排成「相近的挨着」的序列。"""
    clusters = {i: [i] for i in range(n)}
    for a, b, _dv in order:
        clusters[a].extend(clusters.pop(b, []))
    # 剩下的簇按 a 升序拼起来
    out = []
    for k in sorted(clusters):
        out.extend(clusters[k])
    return out


def _cluster_var(cov: np.ndarray, idx: list[int]) -> float:
    if not idx:
        return 0.0
    sub = cov[np.ix_(idx, idx)]
    ivp = 1.0 / np.diag(sub)
    ivp = ivp / ivp.sum()
    return float(ivp @ sub @ ivp)


def _rec_bisect(cov: np.ndarray, idx: list[int]) -> np.ndarray:
    w = np.zeros(cov.shape[0])
    if not idx:
        return w
    if len(idx) == 1:
        w[idx[0]] = 1.0
        return w
    half = len(idx) // 2
    left, right = idx[:half], idx[half:]
    v1, v2 = _cluster_var(cov, left), _cluster_var(cov, right)
    if v1 + v2 <= 0:
        alpha = 0.5
    else:
        alpha = 1.0 - v1 / (v1 + v2)
    w1 = _rec_bisect(cov, left) * alpha
    w2 = _rec_bisect(cov, right) * (1.0 - alpha)
    w[idx] = (w1 + w2)[idx]
    return w


def hrp_weights(returns: np.ndarray) -> np.ndarray:
    """输入 T×N 的收益率矩阵,返回 N 个权重(和为 1)。

    样本不足或退化时退回等权。
    """
    r = np.asarray(returns, dtype="float64")
    if r.ndim != 2 or r.shape[1] < 2 or r.shape[0] < 20:
        n = r.shape[1] if r.ndim == 2 else 1
        return np.full(max(n, 1), 1.0 / max(n, 1))
    # 去掉全 NaN 或零波动的列
    ok = [j for j in range(r.shape[1])
          if np.isfinite(r[:, j]).sum() >= 20 and np.nanstd(r[:, j]) > 1e-12]
    if len(ok) < 2:
        return np.full(r.shape[1], 1.0 / r.shape[1])
    sub = r[:, ok]
    # 用成对完整样本算相关
    corr = np.eye(len(ok))
    for a in range(len(ok)):
        for b in range(a + 1, len(ok)):
            m = np.isfinite(sub[:, a]) & np.isfinite(sub[:, b])
            if m.sum() < 20:
                continue
            c = float(np.corrcoef(sub[m, a], sub[m, b])[0, 1])
            if np.isfinite(c):
                corr[a, b] = corr[b, a] = max(-0.999, min(0.999, c))
    cov = np.cov(np.nan_to_num(sub, nan=0.0).T)
    dist = corr_to_dist(corr)
    order = _single_linkage(dist)
    seq = quasi_diag(order, len(ok))
    w_sub = _rec_bisect(cov, seq)
    w_sub = np.clip(w_sub, 0.0, None)
    if w_sub.sum() <= 0:
        w_sub = np.full(len(ok), 1.0 / len(ok))
    else:
        w_sub = w_sub / w_sub.sum()
    out = np.zeros(r.shape[1])
    for k, j in enumerate(ok):
        out[j] = w_sub[k]
    return out
