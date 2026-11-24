# -*- coding: utf-8 -*-
"""Purged K-Fold:带持有期剔除的交叉验证。

从 López de Prado《Advances in Financial Machine Learning》蒸馏来的一条:
金融序列的标签是**重叠**的 —— 5 日持有的样本,第 i 天和第 i+1 天的标签
共享 4 天价格路径。直接随机切训练/测试,测试集的信息早就漏进训练集了,
算出来的准确率、t 值全是虚的。

做法:
  · 按时间切折(不能随机打乱)
  · 训练集剔除「标签窗口与测试集重叠」的样本(purge)
  · 测试集边界再留一段 embargo,防止边界效应

用法:
    for tr, te in purged_kfold(n, n_splits=5, horizon=5):
        ...
"""
from __future__ import annotations


def purged_kfold(n: int, n_splits: int = 5, horizon: int = 0,
                 embargo: int = 0) -> list[tuple[list[int], list[int]]]:
    """返回 [(训练索引, 测试索引)]。

    n         样本数(按时间排序)
    n_splits  折数
    horizon   标签持有期(样本 i 用到 i+horizon 的信息)
    embargo   测试集后额外隔离的样本数
    """
    if n <= 0 or n_splits < 2:
        return []
    fold = max(1, n // n_splits)
    out: list[tuple[list[int], list[int]]] = []
    for k in range(n_splits):
        lo = k * fold
        hi = n if k == n_splits - 1 else (k + 1) * fold
        if hi <= lo:
            continue
        test = list(range(lo, hi))
        # 训练集:测试段之前要先退 horizon(+embargo),之后要再退 horizon
        train = [i for i in range(n)
                 if i + horizon < lo or i >= hi + embargo + horizon]
        if train and test:
            out.append((train, test))
    return out


def walk_forward_splits(n: int, n_splits: int = 5, horizon: int = 0,
                        min_train: int = 20) -> list[tuple[list[int], list[int]]]:
    """滚动前推:训练集只含测试集之前的数据,绝不看未来。

    比 purged_kfold 更严 —— 前者允许训练集包含测试集之后的数据(只是剔了重叠),
    后者严格按时间顺序,更接近实盘。
    """
    if n <= min_train + horizon:
        return []
    fold = max(1, (n - min_train) // n_splits)
    out: list[tuple[list[int], list[int]]] = []
    for k in range(n_splits):
        lo = min_train + k * fold
        hi = n if k == n_splits - 1 else lo + fold
        if hi <= lo:
            continue
        train = list(range(0, max(0, lo - horizon)))
        test = list(range(lo, hi))
        if len(train) >= min_train and test:
            out.append((train, test))
    return out


def stability(series: list[float], horizon: int = 5,
              n_splits: int = 5) -> dict:
    """把收益序列按时间切折,看每一折的均值和 t 稳不稳。

    全样本 t=2 但只有一折在赚,和每折都小赚,是两回事。
    """
    import math

    def tstat(xs):
        xs = [x for x in xs if x is not None]
        n = len(xs)
        if n < 3:
            return 0.0
        m = sum(xs) / n
        var = sum((x - m) ** 2 for x in xs) / (n - 1)
        sd = math.sqrt(var)
        return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0

    folds = []
    for _tr, te in purged_kfold(len(series), n_splits=n_splits, horizon=horizon):
        seg = [series[i] for i in te if i < len(series)]
        if not seg:
            continue
        m = sum(seg) / len(seg)
        folds.append({"n": len(seg), "mean": round(m, 6), "t": round(tstat(seg), 2)})
    if not folds:
        return {"n_folds": 0}
    pos = sum(1 for f in folds if f["mean"] > 0)
    return {
        "n_folds": len(folds),
        "folds": folds,
        "positive_folds": pos,
        "all_positive": pos == len(folds),
        "worst_mean": min(f["mean"] for f in folds),
        "verdict": ("每折都赚,稳定" if pos == len(folds)
                    else f"只有 {pos}/{len(folds)} 折在赚,不稳"),
    }
