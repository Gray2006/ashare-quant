# -*- coding: utf-8 -*-
"""多策略融合:幻方「多策略」的日线简化版。

单策略会失效,融合几个低相关的策略信号,比押一个稳。
每个策略的分数先横截面 rank,再按权重相加。

权重有两种:
  固定 weights —— 手工指定
  dynamic=True —— 按各因子近期 IC 分配,IC 为负的不给权重

用法:
    st = build("blend", names=["momentum20", "lowvol20"], weights=[0.6, 0.4])
    st = build("blend", dynamic=True)
"""
from __future__ import annotations

import pandas as pd


DEFAULT_NAMES = ("momentum20", "reversal5", "lowvol20", "volprice5", "matrend")


def dynamic_weights(data: dict[str, pd.DataFrame], names: list[str],
                    horizon: int = 5) -> list[float]:
    """按近期因子 IC 分配权重。IC 为负的不给权重。"""
    from . import factor_analysis as fa
    ics = []
    for n in names:
        try:
            st = fa.ic_stats(data, n, horizon=horizon)
            ics.append(max(0.0, float(st.get("ic") or 0)))
        except Exception:  # noqa: BLE001
            ics.append(0.0)
    s = sum(ics)
    if s <= 0:
        return [1.0 / len(names)] * len(names)
    return [x / s for x in ics]


class BlendStrategy:
    name = "blend"
    desc = "多策略融合:动量/反转/低波/量价/均线按 IC 动态加权"

    def __init__(self, names: list[str] | None = None,
                 weights: list[float] | None = None,
                 dynamic: bool = True, **params):
        self.params = {**self.default_params(), **params}
        self.names = list(names or DEFAULT_NAMES)
        self.weights = list(weights or [1.0] * len(self.names))
        self.dynamic = bool(dynamic)

    @classmethod
    def default_params(cls) -> dict:
        return {"n_long": 10, "exit_buffer": 0.15}

    def _build(self):
        from . import strategy as st
        return [st.build(n, n_long=self.params.get("n_long", 10))
                for n in self.names]

    def factor_scores(self, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        weights = (dynamic_weights(data, self.names)
                   if self.dynamic else self.weights)
        total = None
        for s, w in zip(self._build(), weights):
            try:
                p = s.factor_scores(data)
            except Exception:  # noqa: BLE001
                continue
            if p is None or p.empty:
                continue
            r = p.rank(axis=1, pct=True) * w
            total = r if total is None else total.add(r, fill_value=0)
        return total if total is not None else pd.DataFrame()

    def select(self, scores: pd.DataFrame) -> pd.DataFrame:
        from .strategy import CrossSectionalStrategy
        return CrossSectionalStrategy.select(self, scores)
