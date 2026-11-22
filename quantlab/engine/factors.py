# -*- coding: utf-8 -*-
"""横截面因子:输入单只股票的日线 DataFrame,输出「每根 K 线一个分数」的 Series。

所有因子只用到当根及之前的数据(因果),回测引擎再统一 shift(1) 做决策延迟。
分数越高 = 越看多。

因子分四族(对标 qlib Alpha158 + 幻方系公开实践里的「多周期、多类型低相关因子」):

  形态族   K 线几何(涨跌实体、上下影、位置、KSFT 前收偏移),与量纲无关
  时序族   动量/反转/波动/偏度/峰度/极值位置/计数,同一只票自己的历史比较
  量价族   CMF 资金流、VWAP 偏离、成交额热度、量价相关、量能稳定
  回归族   对「市场代理收益」的滚动 beta / R² / 残差(beta20/rsqr20/resi20 等)
           —— 需要市场序列,由调用方传 market(横截面等权收益),拿不到时返回 NaN

几个刻意的设计:

* 单票内不做标准化(横截面 z-score 由引擎统一做),这里只做截尾与 inf 清洗;
* 回归族要求 market:没有市场序列时**返回全 NaN 而不是填 0**。
  填 0 会让这些因子在横截面上等价于「中性票」,排序时混进组合,这是自欺;
* 滚动极值位置(IMAX/IMIN)用 numpy 循环算,1200 根 × 窗口 20 是毫秒级,
  比 rolling.apply 快,也不引入额外依赖。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind


def _z(s: pd.Series) -> pd.Series:
    """横截面标准化由引擎统一做,这里只做单票内的稳健处理(截尾)。"""
    return s.replace([np.inf, -np.inf], np.nan)


def _f(x) -> np.ndarray:
    return np.asarray(x, dtype="float64")


def _series(v: np.ndarray, df: pd.DataFrame) -> pd.Series:
    idx = pd.DatetimeIndex(df["datetime"]) if "datetime" in df.columns else df.index
    return pd.Series(v, index=idx)


def _close(df: pd.DataFrame) -> pd.Series:
    return pd.Series(_f(df["close"]), index=pd.DatetimeIndex(df["datetime"]))


def _amount(df: pd.DataFrame) -> pd.Series:
    """成交额。腾讯/新浪日线可能没有成交额,退化为 close × volume × 100(volume 单位=手)。"""
    a = df.get("amount")
    if a is None:
        a = _f(df["close"]) * _f(df["volume"]) * 100.0
    else:
        a = _f(a)
        bad = ~np.isfinite(a) | (a <= 0)
        if bad.mean() > 0.5:      # 缺失过半就当没有
            a = _f(df["close"]) * _f(df["volume"]) * 100.0
        else:
            a = np.where(bad, _f(df["close"]) * _f(df["volume"]) * 100.0, a)
    return pd.Series(a, index=pd.DatetimeIndex(df["datetime"]))


# ==================================================================== 形态族

def kbar(df: pd.DataFrame, kind: str) -> pd.Series:
    """Alpha158 的 9 个 K 线形态特征,输出与量纲无关的相对值。

    kmid  实体相对开盘:(close-open)/open
    klen  上下影总幅度 / 开盘
    kmid2 实体占比:(close-open)/(high-low)
    kup   上影 / 开盘
    kup2  上影占比:(high-max(open,close))/(high-low)
    klow  下影 / 开盘
    klow2 下影占比:(min(open,close)-low)/(high-low)
    ksft  收盘相对前收的偏移:(2*close-high-low)/open
    ksft2 (2*close-high-low)/(high-low)
    """
    o, h, l, c = (_f(df[k]) for k in ("open", "high", "low", "close"))
    rng = np.where((h - l) == 0, np.nan, h - l)
    o_safe = np.where(o == 0, np.nan, o)
    up = h - np.maximum(o, c)
    dn = np.minimum(o, c) - l
    table = {
        "kmid": (c - o) / o_safe,
        "klen": (h - l) / o_safe,
        "kmid2": (c - o) / rng,
        "kup": up / o_safe,
        "kup2": up / rng,
        "klow": dn / o_safe,
        "klow2": dn / rng,
        "ksft": (2 * c - h - l) / o_safe,
        "ksft2": (2 * c - h - l) / rng,
    }
    if kind not in table:
        raise KeyError(f"未知形态 {kind}")
    return _series(_f(table[kind]), df)


def price_pos(df: pd.DataFrame, kind: str) -> pd.Series:
    """价格快照比率(P0 类):开盘/最高/最低/VWAP 相对收盘的位置。"""
    o, h, l, c = (_f(df[k]) for k in ("open", "high", "low", "close"))
    c_safe = np.where(c == 0, np.nan, c)
    if kind == "open0":
        v = o / c_safe
    elif kind == "high0":
        v = h / c_safe
    elif kind == "low0":
        v = l / c_safe
    elif kind == "vwap0":
        amt, vol = _f(_amount(df)), _f(df["volume"])
        vwap = np.where(vol > 0, amt / (vol * 100.0 + 1e-12), c)
        v = vwap / c_safe
    else:
        raise KeyError(f"未知价格位置 {kind}")
    return _series(v, df)


# ==================================================================== 时序族

def momentum(df: pd.DataFrame, n: int = 20, skip: int = 1) -> pd.Series:
    """n 日动量(跳过最近 skip 天,避开短期反转)。"""
    c = df["close"]
    return _z(c.shift(skip) / c.shift(n) - 1)


def reversal(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """n 日反转:短期跌得多的分高。"""
    c = df["close"]
    return _z(-(c / c.shift(n) - 1))


def roc(df: pd.DataFrame, n: int = 10) -> pd.Series:
    """纯 n 日收益率(Alpha158 的 ROC,不跳最近)。"""
    c = _close(df)
    return _z(c / c.shift(n) - 1)


def low_vol(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """低波动优先。"""
    r = df["close"].pct_change()
    return _z(-r.rolling(n, min_periods=max(5, n // 2)).std(ddof=0))


def raw_std(df: pd.DataFrame, n: int = 10) -> pd.Series:
    """原始波动率(不取负):与 lowvol 反向的一组,用来观察波动溢价的方向。"""
    r = df["close"].pct_change()
    return _z(r.rolling(n, min_periods=max(5, n // 2)).std(ddof=0))


def breakout(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """距 n 日高点的位置(越接近新高分越高)。"""
    c, h = df["close"], df["high"]
    hh = h.rolling(n, min_periods=n // 2).max()
    return _z(c / hh)


def volume_price(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """量价配合:近 n 日涨幅 × 量比。"""
    c, v = df["close"], df["volume"]
    r = c / c.shift(n) - 1
    vr = v / v.rolling(20, min_periods=5).mean()
    return _z(r * vr.clip(upper=3))


def ma_trend(df: pd.DataFrame) -> pd.Series:
    """均线多头排列强度。"""
    c = df["close"]
    m5, m10, m20 = ind.ma(c, 5), ind.ma(c, 10), ind.ma(c, 20)
    return _z((c / m20 - 1) + (m5 / m20 - 1) + (m10 / m20 - 1))


def rsi_reversal(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """RSI 超卖反转:RSI 越低分越高,但只在 RSI<50 时给分。"""
    r = ind.rsi(df["close"], n)
    return _z((50 - r).clip(lower=0))


def turnover_trend(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """换手放大(量能):近 n 日成交量 / 20 日均量。"""
    v = df["volume"]
    return _z(v.rolling(n, min_periods=1).mean() / v.rolling(20, min_periods=5).mean())


def skewness(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日收益偏度:正偏(右尾)分高。"""
    r = df["close"].pct_change()
    return _z(r.rolling(n, min_periods=max(5, n // 2)).skew())


def kurtosis(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日收益峰度:低峰度(少极端)分高。"""
    r = df["close"].pct_change()
    return _z(-r.rolling(n, min_periods=max(5, n // 2)).kurt())


def vol_change(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """波动率变化:近期波动 / 长期波动,波动收敛分高。"""
    r = df["close"].pct_change()
    short = r.rolling(n, min_periods=5).std(ddof=0)
    long = r.rolling(n * 3, min_periods=n).std(ddof=0)
    return _z(-(short / long))


def price_position(df: pd.DataFrame, n: int = 60) -> pd.Series:
    """价格位置:当前价在 n 日区间的位置(0=低,1=高)。"""
    c = df["close"]
    lo = df["low"].rolling(n, min_periods=n // 2).min()
    hi = df["high"].rolling(n, min_periods=n // 2).max()
    rng = (hi - lo).replace(0, np.nan)
    return _z((c - lo) / rng)


def volume_volatility(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """成交量波动:量比的标准差,越稳分越高。"""
    v = df["volume"]
    vr = v / v.rolling(20, min_periods=5).mean()
    return _z(-vr.rolling(n, min_periods=5).std(ddof=0))


def drawdown_factor(df: pd.DataFrame, n: int = 60) -> pd.Series:
    """回撤深度:距 n 日高点的回撤,回撤越浅分越高。"""
    c = df["close"]
    hh = c.rolling(n, min_periods=n // 2).max()
    return _z(c / hh)


def extreme_pos(df: pd.DataFrame, n: int, kind: str) -> pd.Series:
    """滚动窗口内的极值位置与分位数。

    imax  最大值在窗口中的相对位置(0=最近,1=最早)
    imin  最小值在窗口中的相对位置
    qtlu  close / 窗口 80% 分位
    qtld  close / 窗口 20% 分位
    """
    c = _f(df["close"])
    t = len(c)
    out = np.full(t, np.nan)
    if kind in ("imax", "imin"):
        for i in range(n - 1, t):
            w = c[i - n + 1:i + 1]
            if not np.all(np.isfinite(w)):
                continue
            pos = int(np.argmax(w) if kind == "imax" else np.argmin(w))
            out[i] = pos / (n - 1.0)
        return _series(out, df)
    s = pd.Series(c)
    q = s.rolling(n, min_periods=max(3, n // 3))
    if kind == "qtlu":
        out = (s / q.quantile(0.8).replace(0, np.nan)).to_numpy()
    elif kind == "qtld":
        out = (s / q.quantile(0.2).replace(0, np.nan)).to_numpy()
    else:
        raise KeyError(f"未知极值 {kind}")
    return _series(out, df)


def count_pos(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日里正收益天数占比。"""
    r = _close(df).pct_change()
    up = (r > 0).astype("float64")
    return _z(up.rolling(n, min_periods=max(3, n // 2)).mean())


def count_neg(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日里负收益天数占比。"""
    r = _close(df).pct_change()
    dn = (r < 0).astype("float64")
    return _z(dn.rolling(n, min_periods=max(3, n // 2)).mean())


def slope(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """n 日收盘价对时间的 OLS 斜率,除以均价 → 日均相对斜率(趋势的「速度」)。"""
    y = _f(df["close"])
    t = len(y)
    out = np.full(t, np.nan)
    x = np.arange(n, dtype="float64")
    xc = x - x.mean()
    sxx = float((xc ** 2).sum())
    if sxx <= 0:
        return _series(out, df)
    for i in range(n - 1, t):
        w = y[i - n + 1:i + 1]
        if not np.all(np.isfinite(w)):
            continue
        b = float((xc * (w - w.mean())).sum() / sxx)
        out[i] = b / (abs(w.mean()) + 1e-12)
    return _series(out, df)


# ==================================================================== 量价族

def cmf(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Chaikin Money Flow:((C-L)-(H-C))/(H-L) 按成交额加权,窗口内净额 / 窗口总额。

    不用流通市值,所以只要 OHLC + 成交额就能算,全历史可得、可回测。
    """
    h, l, c = (_f(df[k]) for k in ("high", "low", "close"))
    amt = _f(_amount(df))
    rng = h - l
    mfm = np.where(rng > 0, ((c - l) - (h - c)) / np.where(rng > 0, rng, 1.0), 0.0)
    mfv = mfm * amt
    s = pd.Series(mfv)
    a = pd.Series(amt)
    den = a.rolling(n, min_periods=max(3, n // 2)).sum()
    num = s.rolling(n, min_periods=max(3, n // 2)).sum()
    return _z(num / den.replace(0, np.nan))


def vwap_dev(df: pd.DataFrame, n: int = 5) -> pd.Series:
    """收盘相对 n 日 VWAP 的偏离。低于 VWAP 分高(买得比市场平均便宜)。"""
    c = _f(df["close"])
    amt, vol = _f(_amount(df)), _f(df["volume"])
    vwap = amt / (vol * 100.0 + 1e-12)
    vw = pd.Series(vwap).rolling(n, min_periods=max(2, n // 2)).mean()
    return _z(vw / np.where(c == 0, np.nan, c) - 1)


def amount_heat(df: pd.DataFrame, n: int = 5, window: int = 60) -> pd.Series:
    """成交额热度:近 n 日均额 / 近 window 日均额(资金关注度)。"""
    a = _amount(df)
    short = a.rolling(n, min_periods=1).mean()
    long = a.rolling(window, min_periods=max(5, window // 4)).mean()
    return _z(short / long.replace(0, np.nan))


def corr_pv(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """量价相关:收益与成交量变化的相关。负相关(跌时不放量)分高。"""
    r = _close(df).pct_change()
    vol = pd.Series(_f(df["volume"]), index=r.index)
    dv = vol.pct_change().replace([np.inf, -np.inf], np.nan)
    return _z(-r.rolling(n, min_periods=max(5, n // 2)).corr(dv))


# ==================================================================== 回归族

def _market_aligned(df: pd.DataFrame, market) -> pd.Series | None:
    if market is None:
        return None
    if not isinstance(market, pd.Series):
        market = pd.Series(_f(market))
    try:
        m = market.reindex(pd.DatetimeIndex(df["datetime"]))
    except Exception:  # noqa: BLE001
        return None
    if m.notna().sum() < 30:
        return None
    return m.astype("float64")


def rolling_reg(df: pd.DataFrame, kind: str, n: int = 20,
                market: pd.Series | None = None) -> pd.Series:
    """个股收益对市场代理收益的滚动回归。

    beta  对市场的敏感度(系统性风险敞口)
    rsqr  拟合优度(多少是市场解释的)
    resi  残差收益,窗口内累加(剔除市场后的个体 alpha —— 残差动量)
    """
    m = _market_aligned(df, market)
    if m is None:
        return _series(np.full(len(df), np.nan), df)
    r = _close(df).pct_change()
    mm = m.reindex(r.index)
    cov = r.rolling(n, min_periods=max(5, n // 2)).cov(mm)
    var = mm.rolling(n, min_periods=max(5, n // 2)).var()
    beta = cov / var.replace(0, np.nan)
    if kind == "beta":
        return _z(beta)
    if kind == "rsqr":
        return _z(r.rolling(n, min_periods=max(5, n // 2))
                  .corr(mm).pow(2))
    if kind == "resi":
        resid = r - beta * mm
        return _z(resid.rolling(n, min_periods=max(5, n // 2)).sum())
    raise KeyError(f"未知回归因子 {kind}")


# ==================================================================== 注册表

FACTORS: dict[str, tuple[str, any]] = {
    # 形态族(Alpha158 K 线 9 + 价格快照 4)
    "kmid": ("K线实体", lambda df, m=None: kbar(df, "kmid")),
    "klen": ("K线振幅", lambda df, m=None: kbar(df, "klen")),
    "kmid2": ("实体占比", lambda df, m=None: kbar(df, "kmid2")),
    "kup": ("上影线", lambda df, m=None: kbar(df, "kup")),
    "kup2": ("上影占比", lambda df, m=None: kbar(df, "kup2")),
    "klow": ("下影线", lambda df, m=None: kbar(df, "klow")),
    "klow2": ("下影占比", lambda df, m=None: kbar(df, "klow2")),
    "ksft": ("收盘偏移", lambda df, m=None: kbar(df, "ksft")),
    "ksft2": ("偏移占比", lambda df, m=None: kbar(df, "ksft2")),
    "open0": ("开盘/收盘", lambda df, m=None: price_pos(df, "open0")),
    "high0": ("最高/收盘", lambda df, m=None: price_pos(df, "high0")),
    "low0": ("最低/收盘", lambda df, m=None: price_pos(df, "low0")),
    "vwap0": ("VWAP/收盘", lambda df, m=None: price_pos(df, "vwap0")),
    # 时序族
    "momentum20": ("20日动量", lambda df, m=None: momentum(df, 20)),
    "momentum60": ("60日动量", lambda df, m=None: momentum(df, 60)),
    "reversal5": ("5日反转", lambda df, m=None: reversal(df, 5)),
    "roc10": ("10日涨幅", lambda df, m=None: roc(df, 10)),
    "lowvol20": ("低波动", lambda df, m=None: low_vol(df, 20)),
    "std10": ("10日波动", lambda df, m=None: raw_std(df, 10)),
    "breakout20": ("20日突破", lambda df, m=None: breakout(df, 20)),
    "volprice5": ("量价配合", lambda df, m=None: volume_price(df, 5)),
    "matrend": ("均线多头", lambda df, m=None: ma_trend(df)),
    "rsirev": ("RSI超卖", lambda df, m=None: rsi_reversal(df)),
    "voltrend": ("量能放大", lambda df, m=None: turnover_trend(df)),
    "skew20": ("20日偏度", lambda df, m=None: skewness(df, 20)),
    "kurt20": ("20日峰度", lambda df, m=None: kurtosis(df, 20)),
    "volchg": ("波动收敛", lambda df, m=None: vol_change(df)),
    "pos60": ("价格位置", lambda df, m=None: price_position(df)),
    "volstab": ("量能稳定", lambda df, m=None: volume_volatility(df)),
    "dd60": ("回撤深度", lambda df, m=None: drawdown_factor(df)),
    "cntp20": ("上涨天数", lambda df, m=None: count_pos(df, 20)),
    "cntn20": ("下跌天数", lambda df, m=None: count_neg(df, 20)),
    "imax20": ("高点位置", lambda df, m=None: extreme_pos(df, 20, "imax")),
    "imin20": ("低点位置", lambda df, m=None: extreme_pos(df, 20, "imin")),
    "qtlu20": ("距80分位", lambda df, m=None: extreme_pos(df, 20, "qtlu")),
    "qtld20": ("距20分位", lambda df, m=None: extreme_pos(df, 20, "qtld")),
    "slope20": ("20日斜率", lambda df, m=None: slope(df, 20)),
    # 量价族
    "cmf20": ("资金流CMF", lambda df, m=None: cmf(df, 20)),
    "vwapdev5": ("VWAP偏离", lambda df, m=None: vwap_dev(df, 5)),
    "amtheat": ("成交额热度", lambda df, m=None: amount_heat(df)),
    "corrpv20": ("量价相关", lambda df, m=None: corr_pv(df, 20)),
    # 回归族(需要 market,没有就 NaN)
    "beta20": ("20日Beta", lambda df, m=None: rolling_reg(df, "beta", 20, m)),
    "beta60": ("60日Beta", lambda df, m=None: rolling_reg(df, "beta", 60, m)),
    "rsqr20": ("20日拟合度", lambda df, m=None: rolling_reg(df, "rsqr", 20, m)),
    "resi20": ("残差动量", lambda df, m=None: rolling_reg(df, "resi", 20, m)),
}

# 多因子策略的默认池:与云端 full 包一致,少两路回归族(需要市场序列)
DEFAULT_KEYS = ("momentum20", "reversal5", "lowvol20", "volprice5", "matrend")

# 前端分组展示用
FACTOR_GROUPS = {
    "形态": ("kmid", "klen", "kmid2", "kup", "kup2", "klow", "klow2", "ksft", "ksft2",
             "open0", "high0", "low0", "vwap0"),
    "时序": ("momentum20", "momentum60", "reversal5", "roc10", "lowvol20", "std10",
             "breakout20", "matrend", "rsirev", "voltrend", "skew20", "kurt20",
             "volchg", "pos60", "volstab", "dd60", "cntp20", "cntn20", "imax20",
             "imin20", "qtlu20", "qtld20", "slope20"),
    "量价": ("volprice5", "cmf20", "vwapdev5", "amtheat", "corrpv20"),
    "回归": ("beta20", "beta60", "rsqr20", "resi20"),
}


def market_proxy(data: dict[str, pd.DataFrame]) -> pd.Series | None:
    """横截面等权收益,当「市场代理」喂给回归族因子。

    每一项只用当天及之前的信息(等权当日收益),所以是因果的。
    """
    cols = {}
    for code, df in (data or {}).items():
        if df is None or len(df) < 3 or "datetime" not in df.columns:
            continue
        try:
            s = _close(df)
        except Exception:  # noqa: BLE001
            continue
        cols[code] = s.pct_change()
    if len(cols) < 3:
        return None
    panel = pd.DataFrame(cols).sort_index()
    return panel.mean(axis=1, skipna=True).dropna()


def factor_series(df: pd.DataFrame, key: str,
                  market: pd.Series | None = None) -> pd.Series:
    """取一个因子。key 不在表里就抛 KeyError(带可选列表)。"""
    if key not in FACTORS:
        raise KeyError(f"未知因子 {key},可选:{list(FACTORS)}")
    return FACTORS[key][1](df, market)


def factor_label(key: str) -> str:
    return FACTORS.get(key, (key, None))[0]


def cross_rank(panel: pd.DataFrame) -> pd.DataFrame:
    """横截面百分位排名(每行内 0..1),已是因果因子,直接排。"""
    return panel.rank(axis=1, pct=True)


def cs_zscore(panel: pd.DataFrame, winsor: float | None = 0.01) -> pd.DataFrame:
    """横截面去极值 + z-score。

    缺失值保持 NaN,不填 0 —— 填 0 等于把「没数据」当成「中性票」,
    排序时会被当成有效信号选进组合。
    """
    x = panel.replace([np.inf, -np.inf], np.nan)
    if winsor:
        lo = x.quantile(winsor, axis=1)
        hi = x.quantile(1 - winsor, axis=1)
        x = x.clip(lower=lo, upper=hi, axis=0)
    mu = x.mean(axis=1)
    sd = x.std(axis=1)
    return x.sub(mu, axis=0).div(sd.where(sd > 1e-12), axis=0)


def cs_rank(panel: pd.DataFrame) -> pd.DataFrame:
    """横截面排名后再 z-score:比原始 z 更抗尖峰,和 rank IC 口径一致。"""
    return cs_zscore(panel.rank(axis=1, pct=True), winsor=None)


def neutralize(panel: pd.DataFrame, group_of) -> pd.DataFrame:
    """按分组(行业/板块)做组内去均值:剔除「整组一起涨」的暴露。

    group_of: 可调用(列名 → 组名)或 dict。
    """
    fn = group_of if callable(group_of) else (lambda c: group_of.get(c, "其他"))
    out = panel.copy()
    for col in panel.columns:
        out[col] = panel[col]
    labels = pd.Series({c: fn(c) for c in panel.columns})
    for day in out.index:
        row = out.loc[day]
        for _grp, idx in labels.groupby(labels).groups.items():
            vals = row.reindex(idx).dropna()
            if len(vals) < 2:
                continue
            out.loc[day, vals.index] = vals - vals.mean()
    return out


def list_factors(group: str | None = None) -> list[dict]:
    """给界面用的因子清单。"""
    keys = FACTOR_GROUPS.get(group, tuple(FACTORS)) if group else tuple(FACTORS)
    return [{"key": k, "name": factor_label(k),
             "group": next((g for g, ks in FACTOR_GROUPS.items() if k in ks), "其他")}
            for k in keys if k in FACTORS]
