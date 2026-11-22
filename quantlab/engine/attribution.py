# -*- coding: utf-8 -*-
"""绩效归因:卡玛 / 信息比率 / Brinson。

对标市面量化的绩效层:
  卡玛比率   —— 年化收益 / 最大回撤
  信息比率   —— 超额收益 / 跟踪误差
  Brinson    —— 配置效应 + 选股效应 + 交互效应

只做计算,不改策略。
"""
from __future__ import annotations

import pandas as pd


def calmar(equity: pd.Series, periods_per_year: int = 252) -> float:
    """卡玛比率:年化收益 / 最大回撤。"""
    if equity is None or len(equity) < 2:
        return 0.0
    ret = equity.pct_change().dropna()
    ann = float(ret.mean() * periods_per_year)
    mdd = float((equity / equity.cummax() - 1).min())
    return round(ann / abs(mdd), 3) if mdd < 0 else 0.0


def information_ratio(port_ret: pd.Series, bench_ret: pd.Series,
                      periods_per_year: int = 252) -> float:
    """信息比率:超额收益均值 / 跟踪误差。"""
    if port_ret is None or bench_ret is None:
        return 0.0
    diff = (port_ret - bench_ret).dropna()
    if len(diff) < 2:
        return 0.0
    te = float(diff.std())
    if te <= 0:
        return 0.0
    return round(float(diff.mean()) / te * (periods_per_year ** 0.5), 3)


def brinson(port_w: dict[str, float], bench_w: dict[str, float],
            port_r: dict[str, float], bench_r: dict[str, float],
            industry_map: dict[str, str]) -> dict:
    """Brinson 归因:配置效应 + 选股效应 + 交互效应。"""
    inds = set()
    for code in list(port_w) + list(bench_w):
        inds.add(industry_map.get(code, "其他"))
    by_ind = {}
    for ind in inds:
        pw = sum(w for c, w in port_w.items()
                 if industry_map.get(c, "其他") == ind)
        bw = sum(w for c, w in bench_w.items()
                 if industry_map.get(c, "其他") == ind)
        pr = (sum(w * port_r.get(c, 0.0) for c, w in port_w.items()
                  if industry_map.get(c, "其他") == ind) / pw) if pw > 0 else 0.0
        br = (sum(w * bench_r.get(c, 0.0) for c, w in bench_w.items()
                  if industry_map.get(c, "其他") == ind) / bw) if bw > 0 else 0.0
        by_ind[ind] = {"port_w": round(pw, 4), "bench_w": round(bw, 4),
                       "port_r": round(pr, 4), "bench_r": round(br, 4)}
    pr_total = sum(w * port_r.get(c, 0.0) for c, w in port_w.items())
    br_total = sum(w * bench_r.get(c, 0.0) for c, w in bench_w.items())
    alloc = sel = inter = 0.0
    for _ind, d in by_ind.items():
        alloc += (d["port_w"] - d["bench_w"]) * (d["bench_r"] - br_total)
        sel += d["bench_w"] * (d["port_r"] - d["bench_r"])
        inter += (d["port_w"] - d["bench_w"]) * (d["port_r"] - d["bench_r"])
    return {"allocation": round(alloc, 4), "selection": round(sel, 4),
            "interaction": round(inter, 4),
            "port_total": round(pr_total, 4), "bench_total": round(br_total, 4),
            "by_industry": by_ind}
