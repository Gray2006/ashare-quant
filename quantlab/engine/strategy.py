# -*- coding: utf-8 -*-
"""策略框架。

设计要点(对齐 ai-trading-videos / EP007 那套做法):
  * 策略只负责「在 t 日收盘后,根据 t 日及之前的数据给出目标持仓」——因果,不许看未来。
  * 回测引擎统一把信号延后一天、在 t+1 开盘成交,并强制 shift(1) 语义。
  * 换手控制交给 exit_buffer / min_hold / rebalance_days —— 手续费是真金白银。

自定义策略:继承 BaseStrategy,实现 factor_scores()。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import factors as F


class BaseStrategy:
    name = "base"
    desc = ""

    def __init__(self, **params):
        self.params = {**self.default_params(), **params}

    @classmethod
    def default_params(cls) -> dict:
        return {}

    def factor_scores(self, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """返回 index=日期, columns=代码 的分数矩阵(越高越看多)。必须因果。"""
        raise NotImplementedError

    def select(self, scores: pd.DataFrame) -> pd.DataFrame:
        """分数矩阵 → 目标持仓(0/1 矩阵)。默认:每日横截面取前 n_long。"""
        n = int(self.params.get("n_long", 10))
        ranks = scores.rank(axis=1, pct=True)
        thr = 1.0 - min(n, max(len(scores.columns), 1)) / max(len(scores.columns), 1)
        return (ranks >= thr).astype(int)


@dataclass
class XSConfig:
    """横截面策略通用参数。"""
    n_long: int = 10
    exit_buffer: float = 0.15      # 掉出「阈值 - buffer」才卖,降低换手
    min_hold: int = 3              # 最短持仓天数
    rebalance: int = 1             # 每 N 天调仓一次


class CrossSectionalStrategy(BaseStrategy):
    """单因子(或多因子加权)横截面选股。"""

    name = "xs"
    desc = "横截面因子选股"
    factor_keys: tuple[str, ...] = ("momentum20",)

    @classmethod
    def default_params(cls) -> dict:
        return {"n_long": 10, "exit_buffer": 0.15, "min_hold": 3, "rebalance": 1,
                "weights": None, "min_amount": 5e7}

    def factor_scores(self, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        keys = self.factor_keys
        weights = self.params.get("weights") or [1.0] * len(keys)
        # 每个因子单独算,横截面 rank 后再加权 —— 不同因子量纲不同
        # (动量是百分比、低波是标准差),直接相加会被大值因子主导。
        # 这是 qlib Alpha158 的标准化做法。
        # 回归族因子(beta/rsqr/resi)要市场代理:横截面等权当日收益,因果。
        market = F.market_proxy(data)
        panels: dict[str, pd.DataFrame] = {}
        for k in keys:
            cols = {}
            for code, df in data.items():
                if df is None or len(df) < 30:
                    continue
                try:
                    v = F.factor_series(df, k, market)
                except Exception:  # noqa: BLE001
                    continue
                cols[code] = pd.Series(np.asarray(v, dtype="float64"),
                                       index=pd.DatetimeIndex(df["datetime"]))
            if cols:
                panels[k] = pd.DataFrame(cols).sort_index().rank(axis=1, pct=True)
        if not panels:
            return pd.DataFrame()
        total = None
        for (k, p), w in zip(panels.items(), weights):
            total = p * w if total is None else total.add(p * w, fill_value=0)
        panel = total
        # 流动性过滤:成交额太小的票不参与排名
        # 注意:腾讯/新浪日线没有成交额字段,用 close*volume*100 估算(volume 单位是手)
        min_amt = float(self.params.get("min_amount", 0) or 0)
        if min_amt > 0:
            amt = {}
            for code, df in data.items():
                if code not in panel.columns:
                    continue
                a = pd.Series(np.asarray(df["amount"], dtype="float64"),
                              index=pd.DatetimeIndex(df["datetime"]))
                if a.notna().sum() < len(a) * 0.5:
                    a = pd.Series(np.asarray(df["close"] * df["volume"] * 100.0, dtype="float64"),
                                  index=pd.DatetimeIndex(df["datetime"]))
                amt[code] = a
            if amt:
                amt_panel = pd.DataFrame(amt).reindex(panel.index)
                panel = panel.where(amt_panel >= min_amt)
        return panel

    def select(self, scores: pd.DataFrame) -> pd.DataFrame:
        n = int(self.params.get("n_long", 10))
        buf = float(self.params.get("exit_buffer", 0.0))
        k = len(scores.columns)
        if k == 0:
            return pd.DataFrame()
        ranks = scores.rank(axis=1, pct=True)
        thr = 1.0 - min(n, k) / k
        raw = (ranks >= thr).astype(int)
        # 滞后带:已持有且排名还在 thr-buffer 之上就继续持有
        hold = raw.copy()
        kept = np.zeros(len(ranks), dtype=bool)
        out = np.zeros(raw.shape, dtype=int)
        for i in range(len(raw)):
            prev_in = out[i - 1] == 1 if i > 0 else np.zeros(k, dtype=bool)
            r = ranks.iloc[i].to_numpy()
            enter = raw.iloc[i].to_numpy() == 1
            stay = prev_in & (r >= (thr - buf))
            out[i] = (enter | stay).astype(int)
        return pd.DataFrame(out, index=raw.index, columns=raw.columns)


# ---------------------------------------------------------------- 内置策略

class Momentum20(CrossSectionalStrategy):
    name = "momentum20"
    desc = "20 日动量:横截面买入最强 10 只,滞后带降换手"
    factor_keys = ("momentum20",)


class Momentum60(CrossSectionalStrategy):
    name = "momentum60"
    desc = "60 日动量(中长期趋势)"
    factor_keys = ("momentum60",)


class Reversal5(CrossSectionalStrategy):
    name = "reversal5"
    desc = "5 日反转:买入短期超跌"
    factor_keys = ("reversal5",)


class LowVol(CrossSectionalStrategy):
    name = "lowvol20"
    desc = "低波动:波动率最低的一档"
    factor_keys = ("lowvol20",)


class Breakout(CrossSectionalStrategy):
    name = "breakout20"
    desc = "20 日突破:靠近新高 + 量能"
    factor_keys = ("breakout20", "voltrend")
    @classmethod
    def default_params(cls):
        d = super().default_params()
        d.update({"weights": [0.7, 0.3]})
        return d


class MultiFactor(CrossSectionalStrategy):
    name = "multifactor"
    desc = "多因子:动量 + 反转 + 低波 + 量价 + 均线,按 IC 动态加权"
    factor_keys = ("momentum20", "reversal5", "lowvol20", "volprice5", "matrend")

    @classmethod
    def default_params(cls):
        d = super().default_params()
        d.update({"weights": [0.3, 0.15, 0.2, 0.15, 0.2], "n_long": 10,
                  "dynamic": True})
        return d

    def factor_scores(self, data):
        # 因子池来自云端模型包;拿不到时用保守默认(少两路)
        keys = list(self.factor_keys)
        try:
            from .. import model as M
            got = M.factors("a", "multifactor")
            if got:
                keys = [k for k in got if k in F.FACTORS] or keys
        except Exception:  # noqa: BLE001
            pass
        if self.params.get("dynamic", True):
            from .blend import dynamic_weights
            try:
                w = dynamic_weights(data, keys)
                old = self.params.get("weights")
                old_keys = self.factor_keys
                self.params["weights"] = w
                self.factor_keys = tuple(keys)
                try:
                    return super().factor_scores(data)
                finally:
                    self.params["weights"] = old
                    self.factor_keys = old_keys
            except Exception:  # noqa: BLE001
                pass
        return super().factor_scores(data)


class Alpha158(CrossSectionalStrategy):
    """横截面 alpha:从 Alpha158 / 幻方系公开实践里挑的一组低相关因子。

    不是把 158 个因子全塞进来 —— 那玩意儿在两三百只票的池子上,
    相关性 0.9 的因子组会互相抵消,权重变成了「重复投票」。
    这里按四族各取代表,并且默认权重按「稳健优先」给:
      形态(ksft2) 短期反转的几何表达
      时序(slope20, lowvol20) 趋势速度 + 波动溢价
      量价(cmf20) 资金流向的 OHLC 代理
      回归(resi20) 剔除市场后的个体 alpha
    真要用全套,把 factor_keys 换掉就行,评测走 factor_analysis.report。
    """

    name = "alpha158"
    desc = "多族低相关 alpha:形态/趋势速度/低波/资金流/残差动量,等权 rank"
    factor_keys = ("ksft2", "slope20", "lowvol20", "cmf20", "resi20")

    @classmethod
    def default_params(cls):
        d = super().default_params()
        d.update({"weights": [0.2] * 5, "n_long": 10, "exit_buffer": 0.2})
        return d


class WalkForwardMulti(CrossSectionalStrategy):
    """方向自适应的多因子:每个因子的多空方向由历史数据逐期判定,不写死。

    实测依据(沪深300 / 2024-01~2025-06 / 未来 5 日):
      momentum60  RankIC -0.105(t=-5.05)
      momentum20  RankIC -0.060(t=-2.92)
      cmf20       RankIC -0.048(t=-2.81)
    这段行情里动量是**反向**有效的,而 momentum20 / multifactor 把「分高=看多」
    写死,方向直接反了。反过来 2019-2021 动量是正向的 —— 没有永久成立的方向。

    所以这里每天只问一个问题:「到昨天为止,这个因子跟未来 5 日收益是同向还是反向?」
    用 walk-forward 判定(严格因果:第 d 天的 IC 要 d+horizon 才实现),
    |t| < min_t 的因子当期直接不参与(权重 0),而不是给个噪声权重。
    """

    name = "wfmulti"
    desc = "方向自适应多因子:IC 方向逐期判定,不写死「分高=看多」"
    factor_keys = ("momentum20", "momentum60", "reversal5", "lowvol20", "cmf20",
                   "matrend", "slope20", "resi20")

    @classmethod
    def default_params(cls):
        d = super().default_params()
        d.update({"n_long": 10, "exit_buffer": 0.2, "min_t": 2.0,
                  "refit": 20, "horizon": 5, "min_n": 40})
        return d

    def factor_scores(self, data):
        from . import factor_analysis as fa

        keys = [k for k in self.factor_keys if k in F.FACTORS]
        market = F.market_proxy(data)
        panels = {}
        for k in keys:
            cols = {}
            for code, df in data.items():
                if df is None or len(df) < 40:
                    continue
                try:
                    v = F.factor_series(df, k, market)
                except Exception:  # noqa: BLE001
                    continue
                cols[code] = pd.Series(np.asarray(v, dtype="float64"),
                                       index=pd.DatetimeIndex(df["datetime"]))
            if cols:
                panels[k] = pd.DataFrame(cols).sort_index().rank(axis=1, pct=True)
        if not panels:
            return pd.DataFrame()
        horizon = int(self.params.get("horizon", 5))
        dirs = fa.walkforward_directions(
            data, list(panels), horizon=horizon,
            refit=int(self.params.get("refit", 20)),
            min_t=float(self.params.get("min_t", 2.0)),
            min_n=int(self.params.get("min_n", 40)))
        total = None
        for k, p in panels.items():
            if dirs is None or dirs.empty or k not in dirs.columns:
                sign = pd.Series(1.0, index=p.index)
            else:
                sign = dirs[k].reindex(p.index).ffill().fillna(0).astype("float64")
            contrib = p.mul(sign, axis=0)
            total = contrib if total is None else total.add(contrib, fill_value=0)
        if total is None:
            return pd.DataFrame()
        # 全部因子当期都被判为 0 时,别把整张表变成 0(那会选出随机的第一只)
        if float(total.abs().sum().sum()) <= 0:
            return pd.DataFrame()
        return total


REGISTRY: dict[str, type[BaseStrategy]] = {
    c.name: c for c in (Momentum20, Momentum60, Reversal5, LowVol, Breakout,
                        MultiFactor, Alpha158, WalkForwardMulti)
}


def build(name: str, **params) -> BaseStrategy:
    # 自定义规则策略:rule:策略名 —— 回测/自动交易都用同一条路
    if isinstance(name, str) and name.startswith("rule:"):
        from . import rules as rules_mod
        rname = name[5:].strip()
        rule = rules_mod.get_rule(rname)
        if not rule:
            raise KeyError(f"没有名为 {rname} 的自定义策略")
        return rules_mod.RuleStrategy(rule=rule, **params)
    if name in ("blend", "融合"):
        from . import blend
        return blend.BlendStrategy(**params)
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"未知策略 {name},可选:{list(REGISTRY)}")
    if name == "multifactor":
        # 参数来自云端模型包;显式传入的优先
        try:
            from .. import model as M
            for k, v in M.params("a", "multifactor").items():
                if k in ("n_long", "exit_buffer", "max_positions"):
                    params.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass
    return cls(**params)


def list_strategies() -> list[dict]:
    out = [{"name": c.name, "desc": c.desc, "params": c.default_params()}
           for c in REGISTRY.values()]
    out.append({"name": "blend", "desc": "多策略融合:动量/反转/低波/多因子",
                "params": {"n_long": 10}})
    return out