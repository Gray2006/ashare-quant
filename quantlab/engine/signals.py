# -*- coding: utf-8 -*-
"""热点 / 资金流 / 外围 / 消息 信号层。

从一份独立的 signals.py 原型吸收进来的东西,落成这个工程能直接调用的口径:

可回测(有历史数据,进入 IC 评估):
  industry   行业热点:行业动量、行业内相对强弱、行业成交额热度
  moneyflow  CMF 由 OHLC + 成交额算,全历史可得 —— 见 factors.cmf
  external   外围联动:个股对「前一日外围涨跌」的滞后 beta × 最新外围涨跌
  overlay    外围风险偏好:VIX 高位/急升 → 降仓系数

仅实盘叠加(无历史存档,无法回测,权重给得保守):
  新闻词典情绪、事件否决(立案/退市等)、真实主力资金流、概念热点榜

跟原型相比改掉的东西(都是实测出来的问题):
  * 行业分组用「代码 → 行业」静态表时会有幸存者偏差和历史变更偏差,
    所以这里显式接 groups(可以是新浪行业榜、东财 f100,也可以是截面分组),
    拿不到就返回 None 让上层跳过,而不是编一个中性值蒙混过去;
  * 外围 tilt 的滚动协方差用 pandas 的 rolling.cov(另一种对象),
    原型里 .div(...) 那一步是错的,这里直接按列对齐算,不依赖那个语义;
  * 新闻情绪只做「按半衰期衰减的加权和」,并且**新增否决词命中即返回 veto**,
    上层必须先剔除再打分,不能把负面事件当成负分继续参与排序。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..utils import normalize_code
from . import factors as fct


# ==================================================================== 涨跌幅口径

def limit_ratio(code: str, is_st: bool = False, is_new: bool = False) -> float:
    """涨跌幅限制。新股前 5 日无限制返回 inf。

    统一走 engine/exchange.py,别在信号层再抄一份板块表。
    """
    from . import exchange
    return exchange.limit_pct(code, name="ST" if is_st else None,
                              is_st_flag=bool(is_st), is_new=bool(is_new))


def limit_hit(panel: pd.DataFrame, codes: list[str] | None = None,
              is_st: dict[str, bool] | None = None) -> pd.DataFrame:
    """逐日逐票判断当日涨跌幅是否接近涨跌停(±0.3 个百分点以内)。

    返回与 panel 同形状的布尔表(True = 一字/近涨停跌停,当天很难成交)。
    用收盘价环比判断,不含未来信息。
    """
    pct = panel.pct_change() * 100.0
    codes = list(codes or panel.columns)
    is_st = is_st or {}
    # limit_ratio 返回比例(0.10),这里统一换成百分点
    lim = pd.Series({c: limit_ratio(c, bool(is_st.get(c))) * 100.0 for c in codes},
                    dtype="float64")
    lim = lim.reindex(panel.columns).fillna(10.0)
    return pct.abs().ge(lim - 0.3, axis=1)


# ==================================================================== 行业因子

def group_returns(close: pd.DataFrame, groups: pd.Series) -> pd.DataFrame:
    """每日各行业的等权收益。groups: 代码 → 行业名。"""
    gid = groups.reindex(close.columns)
    if gid.isna().all():
        return pd.DataFrame()
    ret = close.pct_change()
    return ret.T.groupby(gid.values, dropna=True).mean().T


def industry_factors(close: pd.DataFrame, amount: pd.DataFrame, groups: pd.Series,
                     w_mom: int = 20, w_heat: tuple[int, int] = (5, 60),
                     log_amount: bool = True) -> dict[str, pd.DataFrame]:
    """行业热点三件套,全部换算回个股层面(同行业的票共享行业分数)。

    返回 {ind_mom, ind_rel, ind_heat},每个都是 日期 × 代码 的横截面 z-score。
      ind_mom   行业动量:行业指数 20 日涨幅
      ind_rel   行业内相对强弱:个股 20 日涨幅 − 行业 20 日涨幅
                (热点轮动里「买龙一」和「买补涨」是两套逻辑,这两个因子分开)
      ind_heat  行业成交额热度:近 5 日均额 / 近 60 日均额

    成交额取对数再算热度:行业内成交额从 20 亿到 2000 亿的头部公司,
    直接相加会被巨无霸行业主导,log 之后才是「相对放大几倍」。
    """
    gid = groups.reindex(close.columns)
    if gid.isna().all():
        return {}
    gret = group_returns(close, groups)
    if gret.empty:
        return {}
    gpx = (1 + gret.fillna(0)).cumprod()
    gmom = gpx / gpx.shift(w_mom) - 1

    a = amount.reindex(columns=close.columns)
    if log_amount:
        a = np.log1p(a.clip(lower=0))
    gamt = a.T.groupby(gid.values, dropna=True).sum().T
    short, long = max(1, int(w_heat[0])), max(2, int(w_heat[1]))
    heat = gamt.rolling(short, min_periods=max(1, short // 2)).mean() / \
        gamt.rolling(long, min_periods=max(2, long // 4)).mean()

    def to_stock(g: pd.DataFrame) -> pd.DataFrame:
        return g.reindex(columns=gid.values).set_axis(close.columns, axis=1)

    ind_mom = to_stock(gmom)
    ind_heat = to_stock(heat)
    stock_mom = close.pct_change(w_mom)
    ind_rel = stock_mom - ind_mom
    return {"ind_mom": fct.cs_zscore(ind_mom),
            "ind_rel": fct.cs_zscore(ind_rel),
            "ind_heat": fct.cs_zscore(ind_heat)}


def theme_heat(close: pd.DataFrame, groups: pd.Series, top: int = 15) -> pd.Series:
    """当日最热概念 → 个股热度(0~1,所属最热板块的名次得分)。

    只用于实盘叠加:概念榜没有历史存档,不能进回测,权重必须给得很保守。
    """
    gret = group_returns(close, groups)
    if gret.empty:
        return pd.Series(dtype="float64")
    today = gret.iloc[-1].dropna().sort_values(ascending=False).head(top)
    if today.empty:
        return pd.Series(dtype="float64")
    score = {name: 1.0 - i / max(len(today), 1) for i, name in enumerate(today.index)}
    gid = groups.reindex(close.columns)
    out = {c: float(score.get(g, 0.0)) for c, g in gid.items() if pd.notna(g)}
    return pd.Series(out, dtype="float64")


# ==================================================================== 外围联动

def external_tilt(close: pd.DataFrame, ext_ret: pd.DataFrame,
                  window: int = 60) -> pd.DataFrame:
    """外围前一日涨跌 → 个股次日相对表现的滞后 beta × 最新外围涨跌。

    beta_t = Cov(r_t, ext_{t-1}) / Var(ext_{t-1}),只用 t 及之前的数据。
    最后乘的是**最新一期** ext_t,即「今晚外围跌了,明天该给多少折价」。

    原型里 `beta.mul(e, axis=0)` 配合 rolling.cov 的另一种对象语义容易错,
    这里逐列算,列对齐明确。
    """
    if ext_ret is None or ext_ret.empty or close is None or close.empty:
        return pd.DataFrame()
    ret = close.pct_change()
    E = ext_ret.reindex(close.index).fillna(0.0)
    total = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for col in E.columns:
        e = E[col]
        lag = e.shift(1)
        var = lag.rolling(window, min_periods=max(10, window // 3)).var()
        # 逐列协方差:ret[code] 与 lag
        for code in close.columns:
            cov = ret[code].rolling(window, min_periods=max(10, window // 3)).cov(lag)
            beta = cov / var.replace(0, np.nan)
            total[code] = total[code] + (beta * e).fillna(0.0)
    return fct.cs_zscore(total)


def risk_overlay(vix: pd.Series | None, index: pd.DatetimeIndex,
                 q_hi: float = 0.85, spike_window: int = 3,
                 spike_thr: float = 0.25) -> pd.Series:
    """外围风险偏好 overlay:VIX 高位或急升 → 降仓系数(1.0 正常 / 0.7 / 0.6)。

    给的是**仓位系数**,不是买卖信号。副作用小、解释成本低。
    """
    idx = pd.DatetimeIndex(index)
    o = pd.Series(1.0, index=idx)
    if vix is None or len(vix) == 0:
        return o
    v = vix.reindex(idx).ffill()
    if v.notna().sum() < 30:
        return o
    q = v.rolling(250, min_periods=60).quantile(q_hi)
    high = v > q
    o[high.fillna(False)] = 0.7
    spike = v.pct_change(spike_window) > spike_thr
    o[spike.fillna(False)] = np.minimum(o[spike.fillna(False)], 0.6)
    return o


# ==================================================================== 新闻情绪

POS = {"预增": 2, "扭亏": 2, "中标": 1.5, "回购": 1.5, "增持": 1.5, "突破": 1, "获批": 1.5,
       "订单": 1, "分红": 1, "创新高": 1, "超预期": 2, "涨价": 1, "战略合作": 1,
       "业绩增长": 1.5, "签订": 0.8}
NEG = {"立案": -3, "调查": -2, "处罚": -2.5, "退市": -4, "减持": -1.5, "亏损": -2, "预亏": -2.5,
       "违规": -2.5, "诉讼": -1.5, "质押": -1, "爆雷": -3, "下修": -1.5, "终止": -1,
       "警示": -2, "商誉减值": -2.5, "被查": -3}
# 命中即否决:这几类事件不是「负面打分」,是当天就不该买
VETO = ("立案", "退市", "被查", "财务造假", "重大违法")


def lexicon_score(title: str) -> float:
    """标题词典分,压到 -1~1。"""
    s = sum(w for k, w in {**POS, **NEG}.items() if k in (title or ""))
    return float(np.tanh(s / 3.0))


def news_vetoed(title: str) -> bool:
    return any(v in (title or "") for v in VETO)


def score_news(items: list[tuple[str, object]], today, half_life: float = 3.0) -> tuple[float, bool]:
    """items: [(标题, 日期)] → (按半衰期衰减的加权情绪, 是否触发否决词)。

    否决是**或**逻辑:七天内出现过一条立案,不管其他新闻多正面,这只票今天不买。
    """
    s = 0.0
    veto = False
    t0 = pd.Timestamp(today)
    for title, d in items:
        if not title:
            continue
        try:
            age = max((t0 - pd.Timestamp(d)).days, 0)
        except Exception:  # noqa: BLE001
            age = 30
        s += lexicon_score(title) * 0.5 ** (age / max(half_life, 0.5))
        veto = veto or news_vetoed(title)
    return float(np.tanh(s / 2.0)), bool(veto)


def score_news_rows(rows: list[dict], today, half_life: float = 3.0) -> tuple[float, bool]:
    """同上,但吃 world.news_cached 那种 [{title, time, ...}] 结构。"""
    items = [(r.get("title") or "", r.get("time") or today) for r in (rows or [])]
    return score_news(items, today, half_life)


# ==================================================================== 组合装订

def compute(close: pd.DataFrame, amount: pd.DataFrame, groups: pd.Series | None = None,
            ext_ret: pd.DataFrame | None = None, vix: pd.Series | None = None,
            weights: dict | None = None) -> dict:
    """把可回测的四件信号一次算出来。

    返回 {"ind_mom","ind_rel","ind_heat","ext","overlay","weights"}。
    任何一块数据缺失就跳过那一块(不在返回里),上层用 key 存在与否判断。
    """
    w = {"ind_mom": 0.25, "ind_rel": 0.15, "ind_heat": 0.10, "ext": 0.20}
    w.update(weights or {})
    out: dict = {"weights": w}
    if groups is not None and not groups.empty:
        ind = industry_factors(close, amount, groups)
        out.update(ind)
    if ext_ret is not None and not ext_ret.empty:
        ext = external_tilt(close, ext_ret)
        if not ext.empty:
            out["ext"] = ext
    if vix is not None and len(vix):
        out["overlay"] = risk_overlay(vix, close.index)
    return out


def blend(alpha: pd.DataFrame, signals: dict, weights: dict | None = None,
          base: float = 1.0, floor: float | None = None) -> pd.DataFrame:
    """alpha 与叠加信号合成。信号各自已横截面标准化,直接加权相加再标准化。

    floor:合成后低于该分位的票直接置 NaN(不参与选股)。
    用来把「行业/资金/外围全都在底部」的票剔除,而不是让它们加权后混进中游。
    """
    w = dict(weights or (signals or {}).get("weights") or {})
    total = alpha.astype("float64")
    for k in ("ind_mom", "ind_rel", "ind_heat", "ext"):
        s = signals.get(k) if signals else None
        if s is None or not isinstance(s, pd.DataFrame) or s.empty:
            continue
        s = s.reindex(index=total.index, columns=total.columns)
        total = total + float(w.get(k, 0.0)) * s
    out = fct.cs_zscore(total)
    if floor is not None:
        r = out.rank(axis=1, pct=True)
        out = out.where(r >= float(floor))
    return out


def overlay_scale(overlay: pd.Series | None, index) -> pd.Series:
    """把 overlay 系数对齐到目标日期索引,缺失按 1.0。"""
    idx = pd.DatetimeIndex(index)
    if overlay is None or len(overlay) == 0:
        return pd.Series(1.0, index=idx)
    return overlay.reindex(idx).ffill().fillna(1.0)


# ==================================================================== 代码池工具

def groups_from_rows(rows: list[dict], key: str = "industry") -> pd.Series:
    """把 [{code, industry}] 转成 Series(代码 → 行业),顺便统一代码格式。"""
    out = {}
    for r in rows or []:
        c = normalize_code(r.get("code") or r.get("symbol") or "")
        g = r.get(key)
        if c and g:
            out[c] = str(g)
    return pd.Series(out, dtype="object")


_GROUP_CACHE: dict[str, tuple[float, str]] = {}
_GROUP_TTL = 6 * 3600.0     # 行业归属变化很慢,6 小时够用


def groups_from_sector(codes: list[str], force: bool = False) -> pd.Series:
    """从板块数据层取「代码 → 一级行业」,只用于实盘叠加(有网络依赖,不能进回测)。

    取不到就返回空 Series:上层看到空就跳过行业因子,
    不要用「全部归到其他」蒙一个分组 —— 那会让 ind_rel 退化成纯个股动量。
    """
    import time

    from ..data import sector

    out: dict[str, str] = {}
    now = time.time()
    for raw in codes or []:
        code = normalize_code(raw)
        if not code:
            continue
        hit = _GROUP_CACHE.get(code)
        if hit and not force and now - hit[0] < _GROUP_TTL:
            if hit[1]:
                out[code] = hit[1]
            continue
        name = ""
        try:
            rows = sector.of_stock(code)
            inds = [r for r in rows if r.get("type") == "行业"]
            # 优先一级行业(level==2),没有就退到任意行业名
            lv = [r for r in inds if int(r.get("level") or 0) == 2]
            pick = (lv or inds or [None])[0]
            name = str((pick or {}).get("name") or "")
        except Exception:  # noqa: BLE001
            name = ""
        _GROUP_CACHE[code] = (now, name)
        if name:
            out[code] = name
    return pd.Series(out, dtype="object")
