# -*- coding: utf-8 -*-
"""多重比较校正 —— 测的因子越多,「撞上显著」越容易。

问题:测 30 个因子、每个用 α=0.05,至少出现一个假阳性的概率是
1 − 0.95³⁰ ≈ **78%**。测 100 个就是 99.4%。
不校正,「挖出一个 t=2.1 的因子」几乎必然发生,而且它是噪声。

做法:Benjamini-Hochberg(BH)控制**错误发现率**(FDR)——
允许一批里有一小部分假阳性,但把比例压在 q 以内。
比 Bonferroni 温和(Bonferroni 是控制「一个都不能错」,太严,基本筛不出东西)。

p 值用正态近似从 t 值反推(样本 >30 时 t 分布接近正态)。
没有 scipy,用 math.erf 手算。
"""
from __future__ import annotations

import math


def t_to_p(t: float | None) -> float:
    """双侧 p 值。正态近似,样本大时够用。"""
    if t is None or not math.isfinite(t):
        return 1.0
    z = abs(float(t))
    # P(|Z| > z) = 2 * (1 - Φ(z)),Φ(z) = 0.5 * (1 + erf(z/√2))
    return max(0.0, min(1.0, 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))))


def bh_qvalues(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg q 值:q_i = min_{j≥i} (p_(j) * m / j)。

    返回与输入同序的 q 值。q ≤ alpha 才算通过。
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, pvalues[i] * m / rank)
        q[i] = val
        prev = val
    return q


def fdr_report(t_values: list[float | None], alpha: float = 0.05) -> dict:
    """给一批 t 值做 FDR 校正,返回汇总 + 每个的 q 值。

    同时给「不校正会通过几个」和「校正后通过几个」——
    差额就是多重比较制造的假阳性数量。
    """
    ps = [t_to_p(t) for t in t_values]
    qs = bh_qvalues(ps)
    raw_pass = [i for i, p in enumerate(ps) if p <= alpha]
    fdr_pass = [i for i, qv in enumerate(qs) if qv <= alpha]
    return {
        "n": len(ps),
        "alpha": alpha,
        "raw_pass": len(raw_pass),
        "fdr_pass": len(fdr_pass),
        "false_positives": len(raw_pass) - len(fdr_pass),
        "qvalues": [round(q, 6) for q in qs],
        "pvalues": [round(p, 6) for p in ps],
        "expected_false": round(len(ps) * alpha, 2),
        "verdict": (
            f"测 {len(ps)} 个,不校正通过 {len(raw_pass)} 个,"
            f"FDR 校正后通过 {len(fdr_pass)} 个"
            + (f"(其中 {len(raw_pass) - len(fdr_pass)} 个是多重比较制造的)"
               if len(raw_pass) > len(fdr_pass) else " · 没有假阳性被制造")
        ),
    }
