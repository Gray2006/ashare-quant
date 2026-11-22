# -*- coding: utf-8 -*-
"""MAP-Elites 因子权重搜索。

用进化算法找多因子权重,比手工定权重更不容易过拟合。
fitness 用因子组合的横截面收益(不跑完整回测,快)。

参考神经网络工作区的 evolve.py,但独立实现,不依赖 CNN。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import factors as F
from .audit import _norm_cdf, _norm_ppf


def _rank_panel(data: dict[str, pd.DataFrame], key: str,
                market: pd.Series | None = None) -> pd.DataFrame | None:
    cols = {}
    for code, df in data.items():
        if df is None or len(df) < 30:
            continue
        try:
            v = F.factor_series(df, key, market)
        except Exception:  # noqa: BLE001
            continue
        cols[code] = pd.Series(np.asarray(v, dtype="float64"),
                               index=pd.DatetimeIndex(df["datetime"]))
    if not cols:
        return None
    return pd.DataFrame(cols).sort_index().rank(axis=1, pct=True)


def _ret_panel(data: dict[str, pd.DataFrame], horizon: int = 5) -> pd.DataFrame:
    cols = {}
    for code, df in data.items():
        if df is None or len(df) < horizon + 2:
            continue
        s = pd.Series(np.asarray(df["close"], dtype="float64"),
                      index=pd.DatetimeIndex(df["datetime"]))
        cols[code] = s.pct_change(horizon).shift(-horizon)
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()


def _fitness(panels: list[pd.DataFrame], ret: pd.DataFrame,
             weights: np.ndarray, n_long: int = 10) -> float:
    """按权重合成因子分数,每天取前 n_long 只,算 horizon 日收益的夏普。"""
    total = None
    for p, w in zip(panels, weights):
        total = p * w if total is None else total.add(p * w, fill_value=0)
    if total is None or total.empty:
        return 0.0
    daily = []
    for day in total.index:
        sc = total.loc[day].dropna()
        if len(sc) < 2:
            continue
        top = sc.nlargest(min(n_long, len(sc))).index
        rv = ret.loc[day].reindex(top).dropna() if day in ret.index else pd.Series(dtype=float)
        if len(rv):
            daily.append(float(rv.mean()))
    if len(daily) < 10:
        return 0.0
    arr = np.array(daily)
    return float(arr.mean() / (arr.std() + 1e-9))


def _cell(weights: np.ndarray, sharpe: float) -> tuple:
    """MAP-Elites 分箱:夏普粗分箱 + 权重集中度分箱。"""
    return (round(sharpe, 1), round(float(np.max(weights)), 1))


def _mutate(weights: np.ndarray, rng: np.random.Generator,
            sigma: float = 0.2) -> np.ndarray:
    w = np.maximum(0.0, weights + rng.normal(0, sigma, len(weights)))
    s = w.sum()
    return w / s if s > 0 else np.ones(len(weights)) / len(weights)


def search(data: dict[str, pd.DataFrame], keys: list[str],
           n_long: int = 10, horizon: int = 5,
           generations: int = 40, pop: int = 24, seed: int = 42,
           n_trials: int | None = None) -> dict:
    """MAP-Elites 搜索因子权重。返回最优权重 + 档案。

    n_trials:fitness 在样本内挑出来的,试了这么多次就该按 DSR 打折。
             默认 = 代数 × 种群(即实际评估过的候选数),调用方可以覆盖。
    """
    rng = np.random.default_rng(seed)
    market = F.market_proxy(data)
    panels = []
    used_keys = []
    for k in keys:
        p = _rank_panel(data, k, market)
        if p is not None:
            panels.append(p)
            used_keys.append(k)
    if len(panels) < 2:
        return {"weights": [1.0] * len(keys), "fitness": 0.0,
                "archive": [], "keys": keys, "n_trials": 0, "dsr": None}
    ret = _ret_panel(data, horizon)
    if ret.empty:
        return {"weights": [1.0] * len(keys), "fitness": 0.0,
                "archive": [], "keys": keys, "n_trials": 0, "dsr": None}

    archive: dict[tuple, dict] = {}
    best = None

    def evaluate(w: np.ndarray) -> float:
        return _fitness(panels, ret, w, n_long)

    # 初始种群:均匀权重 + 随机权重
    pop_list = [np.ones(len(panels)) / len(panels)]
    for _ in range(pop - 1):
        w = rng.random(len(panels))
        pop_list.append(w / w.sum())

    for _gen in range(generations):
        for w in pop_list:
            fit = evaluate(w)
            cell = _cell(w, fit)
            if cell not in archive or fit > archive[cell]["fitness"]:
                archive[cell] = {"weights": w.copy(), "fitness": fit,
                                 "cell": cell}
            if best is None or fit > best["fitness"]:
                best = {"weights": w.copy(), "fitness": fit}
        # 下一代:从档案里采样,变异
        if not archive:
            break
        items = list(archive.values())
        pop_list = []
        for _ in range(pop):
            parent = items[int(rng.integers(len(items)))]
            pop_list.append(_mutate(parent["weights"], rng))

    out_weights = best["weights"] if best else np.ones(len(panels)) / len(panels)
    trials = int(n_trials) if n_trials else int(generations * pop)
    # 样本内搜出来的最好一组,按「试了多少组」折算可信度。
    # 这里没有回测净值,用 fitness(≈ 日频夏普比)× 有效天数近似的 DSR,
    # 只有排序意义:同一份数据里试得越多,drs 越低。真实结论以回测的 DSR 为准。
    ns = int((ret.notna().sum(axis=1) > 0).sum())
    dsr = None
    if ns > 30 and trials >= 2:
        gamma = 0.5772156649
        sr = float(best["fitness"]) if best else 0.0
        sr0 = math.sqrt(1.0 / max(ns - 1, 1)) * (
            (1 - gamma) * _norm_ppf(1 - 1.0 / trials)
            + gamma * _norm_ppf(1 - 1.0 / (trials * math.e)))
        dsr = round(float(_norm_cdf((sr - sr0) * math.sqrt(ns - 1))), 4)
    return {
        "keys": used_keys,
        "weights": [round(float(x), 4) for x in out_weights],
        "fitness": round(float(best["fitness"]) if best else 0.0, 4),
        "n_trials": trials,
        "dsr": dsr,
        "archive": [{"cell": v["cell"], "fitness": round(v["fitness"], 4),
                     "weights": [round(float(x), 4) for x in v["weights"]]}
                    for v in archive.values()],
    }
