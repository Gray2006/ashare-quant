# -*- coding: utf-8 -*-
"""执行算法:TWAP / VWAP 拆单。

对标市面量化的执行层:
  TWAP —— 时间加权,均分成 N 笔
  VWAP —— 成交量加权,按历史成交量分布分配

只生成拆单计划,不实际执行。执行由调用方按计划逐笔下单。
"""
from __future__ import annotations


def twap(qty: int, slices: int = 5, lot: int = 100) -> list[int]:
    """时间加权拆单:均分成 slices 笔,余数给最后一笔。"""
    qty = int(qty // lot * lot)
    if qty <= 0 or slices <= 0:
        return []
    base = qty // slices // lot * lot
    out = [base] * slices
    out[-1] += qty - sum(out)
    return [x for x in out if x > 0]


def vwap(qty: int, volume_profile: list[float], lot: int = 100) -> list[int]:
    """成交量加权拆单:按 volume_profile 的比例分配。"""
    qty = int(qty // lot * lot)
    if qty <= 0 or not volume_profile:
        return []
    total = sum(float(v) for v in volume_profile)
    if total <= 0:
        return twap(qty, len(volume_profile), lot)
    out = []
    used = 0
    n = len(volume_profile)
    for i, v in enumerate(volume_profile):
        if i == n - 1:
            part = qty - used
        else:
            part = int(qty * float(v) / total // lot * lot)
        out.append(part)
        used += part
    return [x for x in out if x > 0]
