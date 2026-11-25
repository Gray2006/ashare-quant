# -*- coding: utf-8 -*-
"""三重障碍标注(Triple Barrier) —— 蒸馏自 López de Prado。

问题:我们一直用「未来 5 日收益」当标签。但真实交易不是这样 ——
有止盈、有止损、有时间限制。用固定收益训练出来的因子,
在真实交易里可能完全不是那回事。

三重障碍:给每个样本放三条线
  上障碍  entry × (1 + tp)   碰到 → 止盈离场,标签 +1
  下障碍  entry × (1 - sl)   碰到 → 止损离场,标签 -1
  垂直障碍 entry 后 h 天     都没碰 → 时间到期,标签 0(按到期收益定符号)

标签是「**先碰到哪条**」,不是「最后涨了多少」。

这个改动比换因子更根本:它改的是「什么算成功」。
"""
from __future__ import annotations

import numpy as np


def triple_barrier(highs, lows, closes, entry: float, tp: float, sl: float,
                   horizon: int) -> tuple[int, float]:
    """对一段未来价格路径做三重障碍判定。

    highs/lows/closes  未来 horizon 根的价格序列(第 0 根是入场当根之后)
    entry              入场价
    tp / sl            止盈、止损幅度(正数,如 0.05 = 5%)
    horizon            最长持有天数

    返回 (label, realized_ret)
      label  1 先碰止盈 / -1 先碰止损 / 0 时间到期
      realized_ret  实际离场收益
    """
    up = entry * (1.0 + tp)
    dn = entry * (1.0 - sl)
    n = min(len(highs), len(lows), len(closes), int(horizon))
    for i in range(n):
        h = float(highs[i])
        lo = float(lows[i])
        # 同一根里两个都碰到:保守起见算止损(实盘里你没法保证先成交哪个)
        if lo <= dn:
            return -1, (dn / entry - 1.0)
        if h >= up:
            return 1, (up / entry - 1.0)
    if n == 0:
        return 0, 0.0
    c = float(closes[n - 1])
    return 0, (c / entry - 1.0) if entry > 0 else 0.0


def label_panel(panel: dict, horizon: int = 5, tp: float = 0.05,
                sl: float = 0.03) -> dict[str, np.ndarray]:
    """给整个面板打三重障碍标签。

    返回 {"label": 标签矩阵, "ret": 实现收益矩阵},形状同 close。
    第 t 行第 j 列 = 在第 t 天收盘后入场、看未来 horizon 天的结果。
    """
    close = panel.get("close")
    high = panel.get("high")
    low = panel.get("low")
    if close is None or close.empty:
        return {"label": np.zeros((0, 0)), "ret": np.zeros((0, 0))}

    c = close.to_numpy(dtype="float64")
    h = (high if high is not None else close).to_numpy(dtype="float64")
    lo = (low if low is not None else close).to_numpy(dtype="float64")
    rows, cols = c.shape
    labels = np.zeros((rows, cols), dtype="float64")
    rets = np.full((rows, cols), np.nan)

    for j in range(cols):
        for i in range(rows - 1):
            entry = c[i, j]
            if not np.isfinite(entry) or entry <= 0:
                continue
            end = min(rows, i + 1 + horizon)
            lab, ret = triple_barrier(h[i + 1:end, j], lo[i + 1:end, j],
                                      c[i + 1:end, j], entry, tp, sl, horizon)
            labels[i, j] = lab
            rets[i, j] = ret
    return {"label": labels, "ret": rets}
