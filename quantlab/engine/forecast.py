# -*- coding: utf-8 -*-
"""走势预判:给概率,也给理由。

跟「大盘研判」的分工:研判给站位分(该进攻还是防守),这里给概率——
未来 N 日涨/平/跌各多少,而且这个概率能被检验。

为什么是「池化」而不是单只标的自己算:
  单只股票 250 根日线估出来的因子方向,标准误约 0.06,而真实信息系数只有
  0.05 上下 —— 等于抛硬币。所以权重和概率都拿一批标的(默认 60 只,约 3 万
  个样本)堆在一起估:统计上站得住,也不会出现「这只票顺势、那只票逆势」的
  随机翻转。标的身的历史只用来算它自己的因子值和相似状态。

四步:
  1. 因子:趋势 / 动量 / 超买超卖 / 量价 / 回撤位置 / 波动 / 相对强弱,
     全部因果(只用当根及之前的数据),值域 [-1, 1];
  2. 方向:每个因子跟未来 N 日收益的相关系数由数据算,符号就是方向
     (A 股个股常见「顺势之后回落」,指数才顺势),权重按 |相关| 归一;
  3. 概率:把样本按分数分桶,数「落在同一桶时之后涨/平/跌各多少」,
     实测频率 + 拉普拉斯平滑 + 保序回归,就是这个桶的概率;
  4. 检验:走查(walk-forward)——第 t 段只用 t 之前的数据定权重和概率,
     再和实际比,报命中率 / Brier / 校准误差,并跟「永远猜多数类」对照。

涨/平/跌按波动率归一:收益除以自身 σ√N,|值| < 0.5 算平,这样一个高频票和
一个低频票的「平」是同一个尺度。

修正项:政策消息、外围、资金流、估值没有足够长的历史序列,进不了统计校准,
只以 logit 平移修概率,幅度有硬上限,并单独列出改了多少。
"""
from __future__ import annotations

import datetime as dt
import math
import time

import numpy as np
import pandas as pd

from ..data import kline as kl
from ..data import newsdb
from ..utils import normalize_code
from . import indicators as ind

# ---------------------------------------------------------------- 参数

HORIZONS = (1, 3, 5, 10, 20)
DEFAULT_HORIZON = 5

PANEL_SIZE = 60           # 池子里多少只票
PANEL_DAYS = 800          # 每只取多少根日线
PANEL_TTL = 1800          # 池子缓存多久(秒)
MIN_FRAMES = 12           # 至少这么多只才算得出池化统计
MIN_BARS = 260            # 单只少于这么多根不参与
WARMUP = 100              # 因子预热
GRID_EVERY = 20           # 每 20 个交易日重估一次权重
GRID_START = 300          # 前 300 个交易日只当训练,不预报
IC_FLOOR = 0.005          # |相关| 低于这个当噪声,权重给 0
MIN_IC_SAMPLES = 200      # 因子少于此行数不定权重
MIN_FACTOR_DAYS = 60      # 因子覆盖不足这么多交易日不定权重
STABLE_MIN_HALF = 80      # 前后两半各自的最少样本数(行)
STABLE_MIN_HALF_DAYS = 40  # 前后两半各自至少要覆盖这么多独立交易日
# 方向稳定性过滤:按日期切两半,两半相关性符号相反就把因子作废。
# 实测(2026-11)两个池子都变差:个股技能 +0.82%→+0.54%,指数 -0.56%→-2.41%,
# 说明在当前样本上它砍掉的是真信号、留下的是噪声。默认关,留着开关供以后复核。
STABLE_FILTER = False
EDGE_MIN = 0.08           # 方向边际达到这个值才算「强信号」
N_BUCKETS = 5
SMOOTH = 1.0
MIN_BUCKET = 40
THR_SIGMA = 0.5           # |收益| < 0.5σ 算平
SIMILAR_BAND = 0.10
SIMILAR_TOP = 8

FACTORS = [
    ("trend", "趋势", "价格相对 20/60 日均线的位置 + 20 日线斜率"),
    ("momentum", "动量", "5 日 / 20 日涨幅 + RSI14"),
    ("oscillator", "超买超卖", "RSI / KDJ 的 J / 布林带位置"),
    ("volume", "量价配合", "量比 × 5 日涨幅方向"),
    ("position", "回撤位置", "距 60 日高点的距离"),
    ("volatility", "波动", "ATR 相对自身中位数"),
    ("relative", "相对强弱", "相对沪深300 的 20 日超额动量"),
    ("market", "大盘环境", "沪深300 当日趋势+动量状态(个股方向的主要外部驱动)"),
    ("news", "新闻情绪", "近 3 个交易日个股新闻的多空词得分(只算收盘前能看到的)"),
    ("notice", "公告事件", "近 45 天公告里的增减持/解禁/立案等硬事件净分"),
    ("money", "主力资金", "近 5 日主力净额占同期成交额的比例(新浪全历史,因果对齐)"),
    ("policy", "政策热度", "政策类新闻条数相对自身近 60 日的 z 分(档案够长才启用)"),
]
FACTOR_LABEL = {k: n for k, n, _d in FACTORS}
FACTOR_DESC = {k: d for k, _n, d in FACTORS}

INDEX_NAMES = {
    "000001": "上证指数", "000300": "沪深300", "399006": "创业板指",
    "399001": "深证成指", "000905": "中证500", "000016": "上证50",
    "000852": "中证1000", "000688": "科创50",
}
INDEX_LIST = [("000001", "上证指数"), ("000300", "沪深300"),
              ("399006", "创业板指"), ("399001", "深证成指")]
INDEX_PANEL = ["000001", "000300", "399001", "399006", "000905", "000016", "000852"]
BENCH_CODE = "000300"

# 取不到全市场快照时的兜底池子(大票,流动性够)
UNIVERSE_FALLBACK = [
    "600519", "601318", "600036", "000858", "002594", "300750", "600030", "002415",
    "601166", "600887", "000333", "600276", "601899", "002304", "600900", "601012",
    "000651", "600809", "603259", "601088", "600028", "601857", "600585", "000002",
]

NOTES = [
    "概率来自池化历史样本的实测频率,不是对点位的预测",
    "因子方向由数据定:个股常「顺势之后回落」,指数才顺势,所以别拿趋势当必然",
    "涨/平/跌按各自波动率归一(±0.5σ),不同活跃度的票可比",
    "新闻/公告是真因子:有历史、进权重、参与校准;取不到就记「无数据」,不当 0 算",
    "政策类新闻接口只能回溯十来天,档案攒够 30 天才启用,之前只做提示不改概率",
    "外围/资金/估值没有长历史,仍只做有上限的 logit 修正",
]

_PANELS: dict[str, list[dict]] = {}
_PANEL_TS: dict[str, float] = {}
_PIPES: dict[tuple[str, int], dict] = {}


# ---------------------------------------------------------------- 小工具

def _clip(x, lo: float = -1.0, hi: float = 1.0):
    return np.clip(x, lo, hi)


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1.0 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return max(0.0, (c - s) / d), min(1.0, (c + s) / d)


def _nanmedian(x) -> float:
    x = np.asarray(x, dtype="float64")
    x = x[np.isfinite(x)]
    return float(np.median(x)) if len(x) else 0.0


def _pava(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """保序回归(不减):分桶概率修成单调,避免「分数更高概率反而更低」。"""
    blocks: list[list[float]] = []
    for yi, wi in zip([float(v) for v in y], [float(v) for v in w]):
        blocks.append([yi, max(wi, 1e-9), 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            b1 = blocks.pop()
            b0 = blocks.pop()
            tw = b0[1] + b1[1]
            val = (b0[0] * b0[1] + b1[0] * b1[1]) / tw if tw > 0 else (b0[0] + b1[0]) / 2
            blocks.append([val, tw, b0[2] + b1[2]])
    out: list[float] = []
    for val, _w, n in blocks:
        out.extend([val] * int(n))
    return np.asarray(out, dtype="float64")


def _edges(s: np.ndarray, k: int = N_BUCKETS) -> np.ndarray:
    if len(s) < k * 2:
        return np.linspace(-1.0, 1.0, k + 1)
    qs = np.quantile(s, np.linspace(0, 1, k + 1))
    if len(np.unique(np.round(qs, 6))) < k + 1:
        return np.linspace(-1.0, 1.0, k + 1)
    qs[0], qs[-1] = min(qs[0], -1.0), max(qs[-1], 1.0)
    return qs


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 60:
        return 0.0
    a, b = x[m], y[m]
    sa, sb = a.std(), b.std()
    if sa <= 1e-12 or sb <= 1e-12:
        return 0.0
    return float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))


# ---------------------------------------------------------------- 因子

def _factor_frame(df: pd.DataFrame, bench: dict[str, float] | None = None) -> pd.DataFrame:
    """每根 K 线上的因子值,全部 [-1, 1],全部因果。"""
    c = pd.Series(np.asarray(df["close"], dtype="float64"))
    h = pd.Series(np.asarray(df["high"], dtype="float64"))
    low = pd.Series(np.asarray(df["low"], dtype="float64"))
    v = pd.Series(np.asarray(df["volume"], dtype="float64"))

    ma20, ma60 = ind.ma(c, 20), ind.ma(c, 60)
    _k, _d, j = ind.kdj(h, low, c)
    rsi14 = ind.rsi(c, 14)
    bu, _bm, bl = ind.boll(c)
    atr14 = ind.atr(h, low, c, 14)
    vol_ma20 = ind.ma(v, 20)

    ret5 = c / c.shift(5) - 1
    ret20 = c / c.shift(20) - 1
    boll_pos = ((c - bl) / (bu - bl).replace(0, np.nan)).clip(0, 1).fillna(0.5)
    vol_ratio = (v / vol_ma20.replace(0, np.nan)).clip(lower=1e-3)
    atr_pct = atr14 / c
    hhv60 = h.rolling(60, min_periods=20).max()
    dd = c / hhv60 - 1

    trend = (0.40 * _clip((c / ma20 - 1) / 0.06)
             + 0.35 * _clip((c / ma60 - 1) / 0.12)
             + 0.25 * _clip((ma20 / ma20.shift(5) - 1) / 0.03))
    momentum = (0.35 * _clip(ret5 / 0.06)
                + 0.35 * _clip(ret20 / 0.12)
                + 0.30 * _clip((rsi14 - 50) / 25))
    oscillator = (0.40 * _clip((50 - rsi14) / 25)
                  + 0.35 * _clip((50 - j) / 40)
                  + 0.25 * _clip((0.5 - boll_pos) * 2))
    volume = _clip(np.log(vol_ratio) / math.log(2.5)) * np.tanh(ret5 / 0.03)
    position = _clip(1.0 + dd / 0.10)
    med = atr_pct.rolling(120, min_periods=40).median()
    volatility = -_clip((atr_pct / med.replace(0, np.nan) - 1) / 0.6)

    cols: dict[str, pd.Series] = {
        "trend": trend, "momentum": momentum, "oscillator": oscillator,
        "volume": volume, "position": position, "volatility": volatility,
    }
    if bench:
        dates = [str(x)[:10] for x in df["datetime"]]
        bc = pd.Series([bench.get(d, np.nan) for d in dates], dtype="float64")
        rel = (c / c.shift(20) - 1) - (bc / bc.shift(20) - 1)
        cols["relative"] = _clip(rel / 0.10)

    out = pd.DataFrame(cols)
    out.index = df.index
    return out.fillna(0.0)


NEWS_WINDOW = 3          # 新闻情绪回看几个交易日
NOTICE_WINDOW = 45       # 公告事件回看多少自然日
POLICY_WINDOW = 5        # 政策热度回看几个交易日
POLICY_BASE = 60         # 政策热度的基准窗口
POLICY_MIN_DAYS = 30     # 档案少于这么多天才启用政策因子


def _day(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _news_cols(code: str, dates: list[str]) -> dict[str, np.ndarray]:
    """新闻情绪 / 公告事件两列,按 K 线日期对齐,缺失记 NaN。"""
    n = len(dates)
    news = np.full(n, np.nan)
    notice = np.zeros(n)
    try:
        dn = newsdb.daily("stock", code, days=n + 60)
    except Exception:
        dn = {}
    try:
        dq = newsdb.daily("notice", code, days=n + 60)
    except Exception:
        dq = {}

    if dn:
        # 每天拆成「收盘前」和「收盘后」两部分:收盘后的归下一个交易日
        pre: dict[dt.date, tuple[float, float]] = {}
        whole: dict[dt.date, tuple[float, float]] = {}
        for ds, v in dn.items():
            d = _day(ds)
            if d is None:
                continue
            pre[d] = (float(v["n"]), float(v["sent"]) * float(v["n"]))
            whole[d] = (float(v["n"]) + float(v["late_n"]),
                        float(v["sent"]) * float(v["n"]) + float(v["late_sent"]) * float(v["late_n"]))
        for i, ds in enumerate(dates):
            d = _day(ds)
            if d is None:
                continue
            tot_n = tot_s = 0.0
            for k in range(1, NEWS_WINDOW + 1):
                cd = d - dt.timedelta(days=k)
                if cd in whole:
                    tot_n += whole[cd][0]
                    tot_s += whole[cd][1]
            if d in pre:
                tot_n += pre[d][0]
                tot_s += pre[d][1]
            if tot_n > 0:
                news[i] = tot_s / tot_n

    if dq:
        ev: dict[dt.date, tuple[float, float]] = {}
        for ds, v in dq.items():
            d = _day(ds)
            if d is None:
                continue
            ev[d] = (float(v["n"]) + float(v["late_n"]),
                     float(v["sent"]) * (float(v["n"]) + float(v["late_n"])))
        for i, ds in enumerate(dates):
            d = _day(ds)
            if d is None:
                continue
            s = 0.0
            for k in range(0, NOTICE_WINDOW + 1):
                cd = d - dt.timedelta(days=k)
                if cd in ev:
                    s += ev[cd][1]
            notice[i] = math.tanh(s / 2.0)
    return {"news": news, "notice": notice}


def _policy_col(dates: list[str]) -> np.ndarray:
    """政策热度:近 5 个交易日的政策类新闻条数,相对近 60 日的 z 分。

    档案天数不够就整列 NaN —— 宁可没有,也不拿 10 天数据当统计依据。
    """
    n = len(dates)
    out = np.full(n, np.nan)
    try:
        dp = newsdb.daily("policy", "*", days=n + 90)
    except Exception:
        dp = {}
    if not dp:
        return out
    cnt: dict[dt.date, float] = {}
    for ds, v in dp.items():
        d = _day(ds)
        if d is not None:
            cnt[d] = float(v["n"]) + float(v["late_n"]) if v["n"] + v["late_n"] else 0.0
    days = [d for d in sorted(cnt) if cnt[d] > 0]
    if len(days) < POLICY_MIN_DAYS:
        return out
    series = []
    for i, ds in enumerate(dates):
        d = _day(ds)
        if d is None:
            series.append(np.nan)
            continue
        w = sum(cnt.get(d - dt.timedelta(days=k), 0.0) for k in range(1, POLICY_WINDOW + 1))
        # 基准要和被比较的量同口径:同样是「5 日窗口和」的历史分布
        base = []
        for j in range(POLICY_WINDOW, POLICY_BASE):
            base.append(sum(cnt.get(d - dt.timedelta(days=j + q), 0.0)
                            for q in range(POLICY_WINDOW)))
        mu = float(np.mean(base)) if base else 0.0
        sd = float(np.std(base)) if base else 0.0
        series.append((w - mu) / sd if sd > 1e-9 else np.nan)
    arr = np.asarray(series, dtype="float64")
    out = _clip(arr, -1.0, 1.0)
    return out


MONEY_WINDOW = 5         # 资金流回看几个交易日
_money_cache: dict[str, dict[str, float]] = {}


def _money_series(code: str) -> dict[str, float]:
    """主力净流入的日序列(元)。

    直接走新浪的全历史:东财 daykline 只回 120 天,而因子要按 800 根日线对齐,
    用东财会让资金流因子只有最近小半年有值 —— 那样估出来的权重没意义。
    """
    hit = _money_cache.get(code)
    if hit is not None:
        return hit
    rows: dict[str, float] = {}
    try:
        from ..data import sina as sina_mod
        bars = sina_mod.moneyflow_history(code, num=1400)
        for b in bars:
            d = str(b.get("date") or "")[:10]
            v = b.get("main")
            if d and v is not None:
                rows[d] = float(v)
    except Exception:
        rows = {}
    if len(rows) < 60:
        try:
            from ..data import moneyflow as mf
            for b in mf.daily(code, days=400):
                d = str(b.get("date") or "")[:10]
                if d and d not in rows and b.get("main") is not None:
                    rows[d] = float(b["main"])
        except Exception:
            pass
    if len(rows) >= 60:
        _money_cache[code] = rows        # 只缓存成功的结果
    return rows


def _money_prefetch(codes: list[str], workers: int = 8) -> None:
    """并发把池子的资金流抓进内存缓存。串行 60 只要一分半,并发十秒出头。"""
    from concurrent.futures import ThreadPoolExecutor

    todo = [c for c in codes if c not in _money_cache]
    if not todo:
        return
    try:
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            list(ex.map(_money_series, todo))
    except Exception:
        for c in todo:                     # 线程池出问题就老实串行
            try:
                _money_series(c)
            except Exception:
                pass


def _money_ratio(code: str, dates: list[str], amount: np.ndarray) -> np.ndarray:
    """近 5 日主力净额 / 近 5 日成交额(原始比例,不压缩)。"""
    n = len(dates)
    out = np.full(n, np.nan)
    ser = _money_series(code)
    if not ser:
        return out
    main = np.array([ser.get(d, np.nan) for d in dates], dtype="float64")
    if np.isfinite(main).sum() < 60:
        return out
    ms = pd.Series(main).rolling(MONEY_WINDOW, min_periods=2).sum().to_numpy()
    am = pd.Series(amount).rolling(MONEY_WINDOW, min_periods=2).sum().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(am > 0, ms / am, np.nan)
    return r


def _money_col(code: str, dates: list[str], amount: np.ndarray) -> np.ndarray:
    """主力资金强度 = 近 5 日主力净额 / 近 5 日成交额,压到 -1~1。

    用同期成交额做分母:大票和小票的净额绝对值没法直接比,
    占成交额的比例才是同一把尺子。
    """
    return _clip(np.tanh(_money_ratio(code, dates, amount) * 8.0), -1.0, 1.0)


def _fwd(close: np.ndarray, horizon: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) > horizon:
        out[:-horizon] = close[horizon:] / close[:-horizon] - 1
    return out


def _expanding_vol(close: np.ndarray) -> np.ndarray:
    """展开窗口的日波动率(只用过去),用来把收益归一到 σ 单位。"""
    r = np.zeros(len(close))
    if len(close) > 1:
        r[1:] = close[1:] / close[:-1] - 1
    vol = np.full(len(close), np.nan)
    c1 = np.cumsum(r * r)
    for i in range(len(close)):
        n = i + 1
        if n >= 60:
            vol[i] = math.sqrt(max(c1[i] / n, 1e-12))
    return vol


# ---------------------------------------------------------------- 权重与合成

def _ic_weights(X: np.ndarray, y: np.ndarray, di: np.ndarray | None = None) -> np.ndarray:
    """相关系数的符号就是因子方向,权重按 |相关| 归一。

    天数是真样本量:同一天几十只票的收益高度相关,不能当成独立样本。
    因子覆盖的交易日不够就给 0,不让十来天的数据定出 0.4 的权重。

    另外做一次方向稳定性检验:把样本按日期切成前后两半,两半里相关性
    符号相反就说明这个因子时而顺势时而逆势,那是噪声不是信号,权重归 0。
    """
    k = X.shape[1]
    ics = np.zeros(k)
    for j in range(k):
        m = np.isfinite(X[:, j]) & np.isfinite(y)
        if m.sum() < MIN_IC_SAMPLES:
            continue
        if di is not None and len(np.unique(di[m])) < MIN_FACTOR_DAYS:
            continue
        ic = _corr(X[:, j], y)
        if STABLE_FILTER and di is not None:
            dj = di[m]
            xj = X[m, j]
            yj = y[m]
            cut = float(np.median(dj))
            h1 = dj <= cut
            h2 = ~h1
            # 按「独立交易日」判定,不能按行数:同一天几十只票彼此高度相关,
            # 行数够了不代表样本够了(指数池就 7 只,一天就是 7 行)
            ok = (len(np.unique(dj[h1])) >= STABLE_MIN_HALF_DAYS
                  and len(np.unique(dj[h2])) >= STABLE_MIN_HALF_DAYS)
            if ok:
                a = _corr(xj[h1], yj[h1])
                b = _corr(xj[h2], yj[h2])
                if np.isfinite(a) and np.isfinite(b) and a * b < 0:
                    continue                     # 方向不稳,整项作废
        ics[j] = ic
    mag = np.where(np.abs(ics) > IC_FLOOR, ics, 0.0)
    tot = float(np.abs(mag).sum())
    return mag / tot if tot > 1e-12 else np.zeros(k)


def _composite(z: np.ndarray, W: np.ndarray) -> np.ndarray:
    """缺失的因子(没有新闻/没有政策档案)不进分子也不进分母,等于跳过。"""
    m = np.isfinite(z)
    z0 = np.where(m, z, 0.0)
    s = np.einsum("ij,ij->i", z0, W)
    tot = np.einsum("ij,ij->i", m.astype("float64"), np.abs(W))
    return np.where(tot > 1e-9, s / np.maximum(tot, 1e-9), 0.0)


# ---------------------------------------------------------------- 校准

def _counts(idx: np.ndarray, y: np.ndarray, thr: float, k: int) -> np.ndarray:
    cnt = np.zeros((k, 3), dtype="float64")
    for b in range(k):
        m = idx == b
        if not m.any():
            continue
        yb = y[m]
        cnt[b, 0] = float((yb > thr).sum())
        cnt[b, 2] = float((yb < -thr).sum())
        cnt[b, 1] = float(m.sum()) - cnt[b, 0] - cnt[b, 2]
    return cnt


def _pooled(cnt: np.ndarray, b: int) -> np.ndarray:
    b = int(max(0, min(len(cnt) - 1, b)))
    if cnt[b].sum() >= MIN_BUCKET:
        return cnt[b]
    return cnt[max(0, b - 1):min(len(cnt), b + 2)].sum(axis=0)


def _dir_from(cnt: np.ndarray) -> tuple[float, float]:
    up, flat, down = (float(x) for x in cnt)
    n = up + flat + down
    p_flat = (flat + SMOOTH) / (n + 2 * SMOOTH) if n > 0 else 0.34
    p_dir = (up + SMOOTH) / (up + down + 2 * SMOOTH) if (up + down) > 0 else 0.5
    return p_dir, min(max(p_flat, 0.02), 0.80)


def _calibrate(s: np.ndarray, y: np.ndarray, edges: np.ndarray | None = None,
               k: int = N_BUCKETS) -> dict:
    valid = np.isfinite(s) & np.isfinite(y)
    sv, yv = s[valid], y[valid]
    if len(sv) < 100:
        raise RuntimeError(f"有效样本只有 {len(sv)} 个,不够校准")
    edges = _edges(sv, k) if edges is None else edges
    real_k = len(edges) - 1
    idx = np.clip(np.digitize(sv, edges[1:-1]), 0, real_k - 1)
    cnt = _counts(idx, yv, THR_SIGMA, real_k)
    p_dir_raw = np.zeros(real_k)
    p_flat_raw = np.zeros(real_k)
    for b in range(real_k):
        p_dir_raw[b], p_flat_raw[b] = _dir_from(_pooled(cnt, b))
    wts = np.array([max(1.0, _pooled(cnt, b).sum()) for b in range(real_k)])
    return {"edges": np.asarray(edges), "centers": (np.asarray(edges[:-1]) + np.asarray(edges[1:])) / 2,
            "counts": cnt, "p_dir_raw": p_dir_raw, "p_dir": _pava(p_dir_raw, wts),
            # p_flat 也做保序平滑:分数越极端,平的概率应越低(单调减),
            # 取负再 PAVA 就得到单调减的平滑曲线。
            "p_flat": -_pava(-p_flat_raw, wts),
            "n": int(len(sv)), "k": real_k}


def _predict(cal: dict, s_now: float) -> tuple[float, float, float, int]:
    """当前分数 → (涨, 平, 跌)。桶内线性插值,别让分数挪一点概率跳一格。"""
    d = float(np.interp(s_now, cal["centers"], cal["p_dir"]))
    f = float(np.interp(s_now, cal["centers"], cal["p_flat"]))
    f = min(max(f, 0.02), 0.80)
    d = min(max(d, 0.0), 1.0)
    b = int(np.digitize(s_now, cal["edges"][1:-1]))
    return (1 - f) * d, f, (1 - f) * (1 - d), min(max(b, 0), cal["k"] - 1)


def _ece(rows: list[tuple[float, int]]) -> float:
    if not rows:
        return 0.0
    bins: dict[int, list[tuple[float, int]]] = {}
    for up, y in rows:
        bins.setdefault(min(4, int(up * 5)), []).append((up, y))
    err, n = 0.0, len(rows)
    for items in bins.values():
        err += abs(np.mean([x[0] for x in items]) - np.mean([x[1] for x in items])) * len(items) / n
    return float(err)


# ---------------------------------------------------------------- 池子

def _universe(n: int) -> list[tuple[str, str]]:
    """按成交额挑池子;快照取不到就用兜底名单。剔除 ST / 退市。"""
    out: list[tuple[str, str]] = []
    try:
        from . import screener
        df = screener.snapshot()
        if df is not None and not df.empty and "amount" in df.columns:
            sub = df.copy()
            sub = sub[~sub["name"].astype(str).str.contains("ST|退", regex=True, na=False)]
            sub = sub.sort_values("amount", ascending=False)
            for _, r in sub.head(n * 2).iterrows():
                out.append((str(r["code"]), str(r["name"])))
                if len(out) >= n:
                    break
    except Exception:
        out = []
    if len(out) < MIN_FRAMES:
        out = [(c, c) for c in UNIVERSE_FALLBACK][:n]
    return out[:n]


def _bench_map(days: int) -> dict[str, float]:
    try:
        bars, _src = kl.get_kline(BENCH_CODE, 101, count=int(days), is_index=True)
        return {str(b["datetime"])[:10]: float(b["close"]) for b in bars if b.get("close")}
    except Exception:
        return {}


def _market_state(days: int) -> dict[str, float]:
    """沪深300 当日的「趋势+动量」状态。个股的绝对方向很大程度由它驱动。"""
    try:
        bars, _src = kl.get_kline(BENCH_CODE, 101, count=int(days), is_index=True)
        if not bars:
            return {}
        df = ind.bars_to_df(bars).tail(int(days)).reset_index(drop=True)
        mat = _factor_frame(df, None)
        m = 0.6 * mat["trend"].to_numpy(dtype="float64") + \
            0.4 * mat["momentum"].to_numpy(dtype="float64")
        return {str(d)[:10]: float(v) for d, v in zip(df["datetime"], m)}
    except Exception:
        return {}


def _make_frame(code: str, name: str, days: int, is_index: bool,
                bench: dict[str, float] | None,
                market: dict[str, float] | None = None) -> dict | None:
    try:
        bars, src = kl.get_kline(code, 101, count=int(days), is_index=bool(is_index))
        if not bars:
            return None
        df = ind.bars_to_df(bars)
        if len(df) < MIN_BARS:
            return None
        df = df.tail(int(days)).reset_index(drop=True)
        mat = _factor_frame(df, bench if not is_index else None)
        ds = [str(x)[:10] for x in df["datetime"]]
        # 列集合必须每只都一致,否则会被当作「因子列不一致」剔掉;
        # 没数据的列留 NaN,合成时自动跳过
        if is_index:
            mat["policy"] = _policy_col(ds)
        else:
            extra = _news_cols(code, ds)
            mat["news"] = extra["news"]
            mat["notice"] = extra["notice"]
            # 缓存里的日线常常没存成交额,用 收盘×成交量(手) 兜底,
            # 只影响资金流的分母量纲
            try:
                amt = np.asarray(df["amount"], dtype="float64") if "amount" in df.columns \
                    else np.full(len(df), np.nan)
            except Exception:
                amt = np.full(len(df), np.nan)
            bad = ~np.isfinite(amt) | (amt <= 0)
            if bad.any():
                fb = (np.asarray(df["close"], dtype="float64")
                      * np.asarray(df["volume"], dtype="float64") * 100.0)
                amt = np.where(bad, fb, amt)
            mat["money"] = _money_col(code, ds, amt)
            df["_amt"] = amt
        if market and not is_index:
            mat["market"] = [market.get(d, 0.0) for d in ds]
        close = np.asarray(df["close"], dtype="float64")
        vol = _expanding_vol(close)
        st = _last_state(df, close)
        if "market" in mat.columns:
            st["market"] = float(mat["market"].iloc[-1])
        if "news" in mat.columns:
            nv = mat["news"].to_numpy(dtype="float64")
            fin = nv[np.isfinite(nv)]
            st["news_sent"] = float(nv[-1]) if np.isfinite(nv[-1]) else 0.0
            st["news_days"] = int(len(fin))          # 有新闻的交易日数
            st["news_n"] = 0
            try:
                _dn = newsdb.daily("stock", code, days=60)
                _bars = [str(x)[:10] for x in df["datetime"]][-NEWS_WINDOW:]
                _lo = min(_bars) if _bars else ""
                st["news_n"] = int(sum(v["n"] + v["late_n"] for d, v in _dn.items() if d >= _lo))
            except Exception:
                pass
        if "notice" in mat.columns:
            qv = mat["notice"].to_numpy(dtype="float64")
            st["notice_ev"] = float(qv[-1]) if np.isfinite(qv[-1]) else 0.0
            try:
                _cov = newsdb.coverage("notice", code)
                st["notice_n"] = int(_cov[0])
            except Exception:
                st["notice_n"] = 0
        if "money" in mat.columns:
            mv = mat["money"].to_numpy(dtype="float64")
            st["money"] = float(mv[-1]) if np.isfinite(mv[-1]) else 0.0
            st["money_has"] = bool(np.isfinite(mv[-1]))
            try:
                _r = _money_ratio(code, ds, np.asarray(df["_amt"], dtype="float64"))
                st["money_ratio"] = float(_r[-1]) if np.isfinite(_r[-1]) else 0.0
            except Exception:
                st["money_ratio"] = 0.0
        if "policy" in mat.columns:
            pv = mat["policy"].to_numpy(dtype="float64")
            st["policy"] = float(pv[-1]) if np.isfinite(pv[-1]) else 0.0
            try:
                _pc = newsdb.coverage("policy", "*")
                st["policy_days"] = int(_pc[0])
            except Exception:
                st["policy_days"] = 0
        if bench and len(close) > 21:                 # 相对强弱的人话要真算,别留 0
            d0 = str(df["datetime"].iloc[-1])[:10]
            d1 = str(df["datetime"].iloc[-21])[:10]
            b0, b1 = bench.get(d0), bench.get(d1)
            if b0 and b1:
                st["rel20"] = float((close[-1] / close[-21] - 1) - (b0 / b1 - 1))
        return {"code": code, "name": name, "dates": [str(x)[:10] for x in df["datetime"]],
                "z": mat.to_numpy(dtype="float64"), "keys": list(mat.columns),
                "close": close, "vol": vol, "src": src, "last": st}
    except Exception:
        return None


def _last_state(df: pd.DataFrame, close: np.ndarray) -> dict:
    """最后一根的状态,给因子文字用。"""
    c = pd.Series(close)
    h = pd.Series(np.asarray(df["high"], dtype="float64"))
    low = pd.Series(np.asarray(df["low"], dtype="float64"))
    v = pd.Series(np.asarray(df["volume"], dtype="float64"))
    ma20 = ind.ma(c, 20)
    bu, _bm, bl = ind.boll(c)
    denom = float(bu.iloc[-1] - bl.iloc[-1])
    vma = float(ind.ma(v, 20).iloc[-1])
    hhv = float(h.rolling(60, min_periods=20).max().iloc[-1])
    atr14 = ind.atr(h, low, c, 14)
    st = {
        "price": float(close[-1]),
        "above20": bool(close[-1] > float(ma20.iloc[-1])),
        "above60": bool(close[-1] > float(ind.ma(c, 60).iloc[-1])),
        "ma20_slope": float(ma20.iloc[-1] / ma20.iloc[-6] - 1) if len(ma20) > 6 else 0.0,
        "ret5": float(close[-1] / close[-6] - 1) if len(close) > 6 else 0.0,
        "ret20": float(close[-1] / close[-21] - 1) if len(close) > 21 else 0.0,
        "rsi14": float(ind.rsi(c, 14).iloc[-1]),
        "kdj_j": float(ind.kdj(h, low, c)[2].iloc[-1]),
        "boll_pos": min(max((close[-1] - float(bl.iloc[-1])) / denom, 0.0), 1.0) if denom > 0 else 0.5,
        "vol_ratio": float(v.iloc[-1] / vma) if vma > 0 else 1.0,
        "dd": float(close[-1] / hhv - 1) if hhv > 0 else 0.0,
        "atr_pct": float(atr14.iloc[-1] / close[-1]) if close[-1] else 0.0,
        "atr_rel": 1.0, "rel20": 0.0,
    }
    med = _nanmedian(np.asarray(atr14.iloc[-120:] / c.iloc[-120:], dtype="float64"))
    st["atr_rel"] = float(st["atr_pct"] / med) if med > 0 else 1.0
    return st


def build_panel(kind: str = "stock", n: int = PANEL_SIZE, days: int = PANEL_DAYS,
                progress=None, force: bool = False) -> list[dict]:
    """建池子。返回帧列表;结果缓存 PANEL_TTL 秒。"""
    kind = "index" if str(kind) == "index" else "stock"
    key = f"{kind}:{n}:{days}"
    if not force and key in _PANELS and time.time() - _PANEL_TS.get(key, 0) < PANEL_TTL:
        return _PANELS[key]

    if kind == "index":
        targets = [(c, INDEX_NAMES.get(c, c)) for c in INDEX_PANEL]
        bench = None
        market = None
        try:                                   # 政策档案:抓不到就空着,因子自动不启用
            newsdb.fetch_policy(pages=4)
        except Exception:
            pass
    else:
        targets = _universe(n)
        bench = _bench_map(days)
        market = _market_state(days)
        # 新闻/公告先并发抓到库里(6 小时缓存)。池子只抓最近一页够估权重,
        # 目标标的会在 forecast() 里单独抓深一点
        try:
            newsdb.prefetch([c for c, _nm in targets], pages=1, notices=40,
                            workers=6)
        except Exception:
            pass
        try:
            _money_prefetch([c for c, _nm in targets], workers=8)
        except Exception:
            pass

    frames: list[dict] = []
    for i, (code, name) in enumerate(targets):
        if progress:
            try:
                progress(i, len(targets))
            except Exception:
                pass
        f = _make_frame(code, name, days, kind == "index", bench, market)
        if f is not None:
            frames.append(f)

    if len(frames) < (4 if kind == "index" else MIN_FRAMES):
        raise RuntimeError(f"只取到 {len(frames)} 只标的的日线,不够做池化统计")
    keys = frames[0]["keys"]
    frames = [f for f in frames if f["keys"] == keys]     # 因子列不一致的剔掉
    _PANELS[key] = frames
    _PANEL_TS[key] = time.time()
    for k in [k for k in _PIPES if k[0] == key]:
        _PIPES.pop(k, None)
    return frames


def _panel_pipeline(pkey: str, horizon: int, frames: list[dict]) -> dict:
    """池化:学权重 → 合成分数 → 校准 → 走查。整段缓存。"""
    ck = (pkey, horizon)
    if ck in _PIPES:
        return _PIPES[ck]

    horizon = int(horizon)
    k = frames[0]["z"].shape[1]
    all_dates = sorted({d for f in frames for d in f["dates"]})
    di = {d: i for i, d in enumerate(all_dates)}
    n_dates = len(all_dates)

    for f in frames:
        f["_y"] = _fwd(f["close"], horizon) / (f["vol"] * math.sqrt(horizon))
        f["_di"] = np.array([di[d] for d in f["dates"]], dtype="int64")

    grid = list(range(GRID_START, n_dates, GRID_EVERY))
    if len(grid) < 3:
        raise RuntimeError(f"只有 {n_dates} 个交易日,不够做走查(至少 {GRID_START + GRID_EVERY * 3})")
    garr = np.asarray(grid)

    def gather(before: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        xs, ys, ds = [], [], []
        for f in frames:
            m = (f["_di"] < before) & np.isfinite(f["_y"])
            if m.any():
                xs.append(f["z"][m])
                ys.append(f["_y"][m])
                ds.append(f["_di"][m])
        if not xs:
            return np.zeros((0, k)), np.zeros(0), np.zeros(0, dtype="int64")
        return np.concatenate(xs), np.concatenate(ys), np.concatenate(ds)

    # 每个网格点的权重(只用该点之前的数据)
    Ws = np.zeros((len(grid), k))
    for gi, g in enumerate(grid):
        X, y, dd = gather(int(g))
        Ws[gi] = _ic_weights(X, y, dd) if len(y) > 200 else np.zeros(k)

    # 每帧分数:用日期 < row 的最后一个网格权重
    for f in frames:
        j = np.searchsorted(garr, f["_di"], side="right") - 1
        W_row = np.where(j[:, None] >= 0, Ws[np.maximum(j, 0)], 0.0)
        s = _composite(f["z"], W_row)
        s[f["_di"] < GRID_START] = np.nan
        f["_S"] = s

    S = np.concatenate([f["_S"] for f in frames])
    Y = np.concatenate([f["_y"] for f in frames])
    cal = _calibrate(S, Y)

    # 走查:第 j 段只用第 j 个网格点之前的数据定权重和概率
    hits = naive = total = 0
    dir_hits = dir_total = dir_up = 0
    st_hits = st_total = st_up = 0
    brier = brier_base = 0.0
    ece_rows: list[tuple[float, int]] = []
    for gi, g in enumerate(grid[:-1]):
        nxt = grid[gi + 1]
        bs = np.concatenate([f["_S"][f["_di"] < g] for f in frames])
        by = np.concatenate([f["_y"][f["_di"] < g] for f in frames])
        if np.isfinite(bs).sum() < 300:
            continue
        calj = _calibrate(bs, by)
        base = calj["counts"].sum(axis=0) / max(calj["counts"].sum(), 1e-9)
        for f in frames:
            m = (f["_di"] >= g) & (f["_di"] < nxt) & np.isfinite(f["_y"]) & np.isfinite(f["_S"])
            if not m.any():
                continue
            for sv, yv in zip(f["_S"][m], f["_y"][m]):
                up, flat, down, _b = _predict(calj, float(sv))
                actual = 0 if yv > THR_SIGMA else (2 if yv < -THR_SIGMA else 1)
                hits += int(int(np.argmax([up, flat, down])) == actual)
                naive += int(int(np.argmax(base)) == actual)
                if actual != 1:                    # 只看真涨真跌的,方向猜对没有
                    dir_total += 1
                    hit_dir = int((0 if up >= down else 2) == actual)
                    dir_hits += hit_dir
                    dir_up += int(actual == 0)
                    edge = abs(up / max(up + down, 1e-9) - 0.5)
                    if edge >= EDGE_MIN:           # 只在强信号子集里算成绩
                        st_total += 1
                        st_hits += hit_dir
                        st_up += int(actual == 0)
                brier += sum((pr - (1.0 if actual == c else 0.0)) ** 2
                             for c, pr in enumerate((up, flat, down)))
                brier_base += sum((base[c] - (1.0 if actual == c else 0.0)) ** 2 for c in range(3))
                ece_rows.append((up, 1 if actual == 0 else 0))
                total += 1

    bt = {}
    if total >= 200:
        bt = {"n": total, "hit": hits / total, "hit_naive": naive / total,
              "brier": brier / total, "brier_base": brier_base / total,
              "skill": (1 - brier / brier_base) if brier_base > 0 else 0.0,
              "calib_err": _ece(ece_rows),
              "dir_hit": (dir_hits / dir_total) if dir_total > 0 else 0.0,
              "dir_base": (dir_up / dir_total) if dir_total > 0 else 0.0,
              "dir_n": dir_total,
              "strong": {"n": st_total,
                         "hit": (st_hits / st_total) if st_total > 0 else 0.0,
                         "base": (st_up / st_total) if st_total > 0 else 0.0,
                         "edge": EDGE_MIN}}

    out = {"frames": frames, "keys": frames[0]["keys"], "cal": cal, "grid": grid,
           "date_index": di,
           "W": Ws, "bt": bt, "horizon": horizon, "n_dates": n_dates,
           "n_rows": int(np.isfinite(S).sum())}
    _PIPES[ck] = out
    return out


# ---------------------------------------------------------------- 修正项

def _overlay_index(rep_macro: dict | None) -> tuple[float, list[dict]]:
    if not rep_macro:
        return 0.0, []
    dims = [d for d in rep_macro.get("dims", []) if d.get("score") is not None]
    if not dims:
        return 0.0, []
    wsum = sum(float(d.get("weight") or 1) for d in dims) or 1.0
    out: list[dict] = []
    shift = 0.0
    for d in dims:
        x = (float(d["score"]) - 50.0) / 50.0
        delta = x * (float(d.get("weight") or 1) / wsum) * 1.4
        shift += delta
        out.append({"name": "研判·" + str(d["name"]), "signal": x, "shift": delta,
                    "reason": f"{d['name']} {float(d['score']):.0f} 分 · {str(d.get('detail', ''))[:60]}"})
    shift = max(-0.7, min(0.7, shift))
    out.append({"name": "研判合计", "signal": (float(rep_macro.get("score") or 50) - 50) / 50,
                "shift": shift,
                "reason": f"大盘研判 {rep_macro.get('score')} 分 · {rep_macro.get('stance')}"})
    return shift, out


def _overlay_stock(code: str, df: pd.DataFrame) -> tuple[float, list[dict]]:
    """个股的 logit 修正。资金流已经是真因子了,这里不再重复算,只留估值。"""
    out: list[dict] = []
    shift = 0.0
    try:
        from . import screener
        dfm = screener.snapshot()
        row = dfm[dfm["code"] == normalize_code(code)]
        if not row.empty:
            r0 = row.iloc[0]
            pe = float(r0["pe_ttm"]) if pd.notna(r0["pe_ttm"]) else None
            pb = float(r0["pb"]) if pd.notna(r0["pb"]) else None
            cap = float(r0["market_cap"]) if pd.notna(r0["market_cap"]) else None
            parts, xs = [], []
            if pe is not None and pe > 0:
                valid_pe = dfm["pe_ttm"] > 0
                pos = float((valid_pe & (dfm["pe_ttm"] < pe)).sum()) / max(float(valid_pe.sum()), 1)
                xs.append(1 - 2 * pos)
                parts.append(f"PE {pe:.1f}(全市场 {pos * 100:.0f}% 分位)")
            if pb is not None and pb > 0:
                pos = float((dfm["pb"] < pb).sum()) / max(float(dfm["pb"].notna().sum()), 1)
                xs.append(1 - 2 * pos)
                parts.append(f"PB {pb:.2f}({pos * 100:.0f}% 分位)")
            if xs:
                x = float(np.mean(xs))
                delta = x * 0.18
                shift += delta
                out.append({"name": "估值", "signal": x, "shift": delta,
                            "reason": "·".join(parts) + (",低估加分" if x > 0 else ",偏贵扣分")})
            if cap:
                out.append({"name": "市值", "signal": 0.0, "shift": 0.0,
                            "reason": f"总市值 {cap / 1e8:.0f} 亿(只作背景,不改概率)"})
    except Exception:
        pass
    return max(-0.8, min(0.8, shift)), out


def _apply_shift(up: float, flat: float, down: float, shift: float) -> tuple[float, float, float]:
    if not shift:
        return up, flat, down
    p_dir = up / max(up + down, 1e-9)
    p_dir2 = _sigmoid(_logit(p_dir) + shift)
    return (1 - flat) * p_dir2, flat, (1 - flat) * (1 - p_dir2)


# ---------------------------------------------------------------- 理由

def _factor_text(key: str, st: dict) -> str:
    if key == "trend":
        return (f"价格 {st['price']:.2f},{'站上' if st['above20'] else '跌破'} 20 日线,"
                f"{'站上' if st['above60'] else '跌破'} 60 日线,20 日线 5 日斜率 "
                f"{st['ma20_slope'] * 100:+.2f}%")
    if key == "momentum":
        return f"5 日 {st['ret5'] * 100:+.1f}%,20 日 {st['ret20'] * 100:+.1f}%,RSI14 {st['rsi14']:.0f}"
    if key == "oscillator":
        zone = "超买区" if st["rsi14"] > 70 else ("超卖区" if st["rsi14"] < 30 else "中性区")
        return (f"RSI14 {st['rsi14']:.0f} 在{zone},KDJ 的 J {st['kdj_j']:.0f},"
                f"布林带位置 {st['boll_pos'] * 100:.0f}%")
    if key == "volume":
        if st["vol_ratio"] >= 1.2:
            tone = "放量上涨" if st["ret5"] > 0 else "放量下跌"
        elif st["vol_ratio"] <= 0.8:
            tone = "缩量"
        else:
            tone = "量能平稳"
        return f"量比(20 日){st['vol_ratio']:.2f},{tone}"
    if key == "position":
        return f"距 60 日高点 {st['dd'] * 100:+.1f}%"
    if key == "volatility":
        return f"ATR 占价格 {st['atr_pct'] * 100:.2f}%,是自己近 120 日中位数的 {st['atr_rel']:.2f} 倍"
    if key == "relative":
        return (f"近 20 日{'跑赢' if st['rel20'] >= 0 else '跑输'}沪深300 "
                f"{abs(st['rel20']) * 100:.1f} 个百分点")
    if key == "market":
        v = st.get("market", 0.0)
        tone = "偏强" if v > 0.15 else ("偏弱" if v < -0.15 else "震荡")
        return f"沪深300 趋势+动量状态 {v:+.2f}({tone})"
    if key == "news":
        if not st.get("news_n"):
            return "近 3 日没抓到相关新闻,这一项不参与"
        tone = "偏多" if st["news_sent"] > 0.1 else ("偏空" if st["news_sent"] < -0.1 else "中性")
        return f"近 3 日 {st['news_n']} 条个股新闻,情绪 {st['news_sent']:+.2f}({tone})"
    if key == "notice":
        if not st.get("notice_n"):
            return "近 45 天没有公告,按中性处理"
        tone = "利空事件为主" if st["notice_ev"] < 0 else ("利好事件为主" if st["notice_ev"] > 0 else "无强信号")
        return f"近 45 天 {st['notice_n']} 份公告,事件分 {st['notice_ev']:+.2f}({tone})"
    if key == "money":
        if not st.get("money_has"):
            return "没有这只票的历史资金流,这一项不参与"
        v = st.get("money", 0.0)
        tone = "净流入" if v > 0.1 else ("净流出" if v < -0.1 else "进出平衡")
        return (f"近 5 日主力{tone},净额占成交额 {st.get('money_ratio', 0.0) * 100:+.1f}%"
                f"(强度 {v:+.2f})")
    if key == "policy":
        if st.get("policy_days", 0) < POLICY_MIN_DAYS:
            return f"政策档案只有 {st.get('policy_days', 0)} 天,不够统计,先不启用"
        v = st.get("policy", 0.0)
        tone = "政策密集" if v > 0.5 else ("政策清淡" if v < -0.5 else "政策节奏平常")
        return f"近 5 日政策类新闻 {v:+.2f} 个标准差({tone})"
    return ""


def _verdict(up: float, flat: float, down: float,
             skill: float = 0.0, consistency: float = 0.0) -> tuple[str, str]:
    """方向判断。技能分高时阈值收紧,更敢给方向;技能分低时保守。

    consistency: 多周期方向一致性(0~1)。一致时阈值再收紧 30%,
    不一致时保持原阈值 —— 短期和中期打架,不该硬给方向。
    """
    edge = up - down
    if skill >= 0.04:
        t1, t2 = 0.05, 0.15
    elif skill >= 0.01:
        t1, t2 = 0.07, 0.18
    else:
        t1, t2 = 0.08, 0.20
    mult = 1.0 - 0.3 * max(0.0, min(1.0, consistency))
    t1 *= mult
    t2 *= mult
    if edge >= t2:
        return "看多", "方向概率明显偏上,按计划持仓,别追高"
    if edge >= t1:
        return "偏多", "略偏多,控制单票仓位,等量能确认"
    if edge <= -t2:
        return "看空", "方向概率明显偏下,降仓为主,不接飞刀"
    if edge <= -t1:
        return "偏空", "略偏空,减少逆势加仓"
    return "中性", (f"多空差 {edge:+.1%},离方向判断还差 {t1 - abs(edge):.1%},"
                    "降低换手,等信号")


def _confidence(n: int, width: float, skill: float, ece: float = 1.0) -> str:
    """置信度看三件事:样本量、区间宽度、走查技能分。技能分不高就是低。"""
    if n >= 3000 and width <= 0.12 and skill >= 0.04 and ece <= 0.03:
        return "高"
    if n >= 1200 and width <= 0.20 and skill >= 0.01 and ece <= 0.05:
        return "中"
    return "低"


# ---------------------------------------------------------------- 主入口

def forecast(code: str, horizon: int = DEFAULT_HORIZON, days: int = PANEL_DAYS,
             kind: str = "stock", name: str = "", overlay: bool = True,
             macro_rep: dict | None = None, progress=None) -> dict:
    """给一只标的的方向概率 + 原因。"""
    code = normalize_code(code)
    is_index = str(kind) == "index"
    horizon = int(horizon if horizon in HORIZONS else DEFAULT_HORIZON)
    pk = "index" if is_index else "stock"
    if not is_index:
        # 目标标的自己抓深一点(约 300 条,覆盖 1~6 个月),池子里只抓一页
        try:
            newsdb.fetch_stock(code, pages=3)
            newsdb.fetch_notices(code, 100)
        except Exception:
            pass
    frames = build_panel(pk, PANEL_SIZE, days, progress)
    pkey = f"{pk}:{PANEL_SIZE}:{days}"
    pipe = _panel_pipeline(pkey, horizon, frames)

    bench = None if is_index else _bench_map(days)
    market = None if is_index else _market_state(days)
    me = _make_frame(code, name or INDEX_NAMES.get(code, code), days, is_index, bench, market)
    if me is None:
        # 池子里没有它也不影响:单独取一次,因子列对齐
        bars, src = kl.get_kline(code, 101, count=int(days), is_index=is_index)
        if not bars:
            raise RuntimeError(f"{code} 没有取到日线")
        df0 = ind.bars_to_df(bars).tail(int(days)).reset_index(drop=True)
        if len(df0) < MIN_BARS:
            raise RuntimeError(f"日线只有 {len(df0)} 根,不够预判(至少 {MIN_BARS} 根)")
        mat = _factor_frame(df0, bench if not is_index else None)
        if market and not is_index:
            ds0 = [str(x)[:10] for x in df0["datetime"]]
            mat["market"] = [market.get(d, 0.0) for d in ds0]
        close0 = np.asarray(df0["close"], dtype="float64")
        st0 = _last_state(df0, close0)
        me = {"code": code, "name": name or INDEX_NAMES.get(code, code),
              "dates": [str(x)[:10] for x in df0["datetime"]],
              "z": mat.to_numpy(dtype="float64"), "keys": list(mat.columns),
              "close": close0, "vol": _expanding_vol(close0), "src": src,
              "last": st0}
        df = df0
    else:
        bars, _src = kl.get_kline(code, 101, count=int(days), is_index=is_index)
        df = ind.bars_to_df(bars).tail(int(days)).reset_index(drop=True)

    keys = pipe["keys"]
    idx_map = {k: i for i, k in enumerate(me["keys"])}
    z_now = np.array([me["z"][-1, idx_map[k]] for k in keys])
    w = pipe["W"][-1]
    # 这只标的自己的分数序列:按日期取当时那一格的权重,不看未来
    garr = np.asarray(pipe["grid"])
    j = np.array([pipe["date_index"].get(d, -1) for d in me["dates"]], dtype="int64")
    j = np.searchsorted(garr, j, side="right") - 1
    W_row = np.where(j[:, None] >= 0, pipe["W"][np.maximum(j, 0)], 0.0)
    s_me = _composite(me["z"], W_row)
    s_me[j < 0] = np.nan
    s_now = float(s_me[-1]) if np.isfinite(s_me[-1]) else float(_composite(z_now[None, :], w[None, :])[0])
    up0, flat0, down0, bucket = _predict(pipe["cal"], s_now)

    sigma_now = float(me["vol"][-1]) if np.isfinite(me["vol"][-1]) else 0.02
    thr_now = THR_SIGMA * sigma_now * math.sqrt(horizon)

    # 修正项
    shift = 0.0
    overlay_rows: list[dict] = []
    missing: list[str] = []
    if overlay:
        if is_index:
            rep = macro_rep
            if rep is None:
                try:
                    from . import macro as macro_mod
                    rep = macro_mod.analyze()
                except Exception as e:  # noqa: BLE001
                    missing.append(f"大盘研判取数失败:{str(e)[:40]}")
                    rep = None
            shift, overlay_rows = _overlay_index(rep)
            macro_rep = rep
        else:
            shift, overlay_rows = _overlay_stock(code, df)
            try:
                from . import macro as macro_mod
                rep = macro_rep or macro_mod.analyze()
                macro_rep = rep
                # 个股已经有「大盘环境」因子了,这里只用广度/资金/外围/消息,
                # 避免趋势维度被算两遍
                dims = [d for d in (rep.get("dims") or [])
                        if d.get("score") is not None and d.get("name") != "趋势"]
                if dims:
                    wsum = sum(float(d.get("weight") or 1) for d in dims) or 1.0
                    sc = sum(float(d["score"]) * float(d.get("weight") or 1)
                             for d in dims) / wsum
                    x = (sc - 50.0) / 50.0
                    d = x * 0.15
                    shift += d
                    overlay_rows.append({"name": "大盘环境(非趋势)", "signal": x, "shift": d,
                                         "reason": f"广度/资金/外围/消息加权 {sc:.0f} 分"
                                                   f"(大盘总分 {rep.get('score')} · {rep.get('stance')})"})
            except Exception:
                pass
    shift = max(-0.9, min(0.9, shift))
    bt = pipe["bt"]
    up, flat, down = _apply_shift(up0, flat0, down0, shift)

    cn = _pooled(pipe["cal"]["counts"], bucket)
    ci = _wilson(float(cn[0]), float(cn.sum())) if cn.sum() > 0 else (0.0, 1.0)
    conf = _confidence(int(pipe["cal"]["n"]), ci[1] - ci[0],
                       float(bt.get("skill") or 0.0), float(bt.get("calib_err") or 1.0))

    y_me = _fwd(me["close"], horizon) / (me["vol"] * math.sqrt(horizon))

    # 相似历史状态(只看这只标的自己,比池子更贴近)
    similar: list[dict] = []
    for band in (SIMILAR_BAND, SIMILAR_BAND * 1.8, SIMILAR_BAND * 3):
        cand = np.where(np.isfinite(s_me) & np.isfinite(y_me)
                        & (np.abs(s_me - s_now) <= band))[0]
        if len(cand) >= 3:
            similar = [{"date": me["dates"][i], "score": float(s_me[i]),
                        "ret": float(y_me[i] * sigma_now * math.sqrt(horizon)) * 100}
                       for i in cand[-SIMILAR_TOP:]][::-1]
            break

    # 因子明细
    st = me["last"]
    factors: list[dict] = []
    totsum = float(np.abs(w).sum()) or 1.0
    for jj, key in enumerate(keys):
        raw = float(z_now[jj])
        wj = float(w[jj])
        has = bool(np.isfinite(raw))
        v = raw if has else 0.0
        factors.append({
            "key": key, "name": FACTOR_LABEL.get(key, key), "desc": FACTOR_DESC.get(key, ""),
            "value": v, "weight": wj, "contrib": (v * wj / totsum) if has else 0.0,
            "has": has,
            "follow": "顺势" if wj > 0 else ("逆势" if wj < 0 else "无效"),
            "detail": _factor_text(key, st),
        })

    buckets: list[dict] = []
    for b in range(pipe["cal"]["k"]):
        c2 = pipe["cal"]["counts"][b]
        if c2.sum() <= 0:
            continue
        buckets.append({
            "lo": float(pipe["cal"]["edges"][b]), "hi": float(pipe["cal"]["edges"][b + 1]),
            "n": int(c2.sum()), "up": int(c2[0]), "flat": int(c2[1]), "down": int(c2[2]),
            "p_dir": float(pipe["cal"]["p_dir"][b]), "cur": b == bucket,
        })

    # 多周期
    horizons: list[dict] = []
    for h in HORIZONS:
        try:
            ph = pipe if h == horizon else _panel_pipeline(pkey, h, frames)
            u, f2, d2, _b = _predict(ph["cal"], s_now)
            yy = np.concatenate([ff["_y"][np.isfinite(ff["_y"])] for ff in ph["frames"]])
            sc = sigma_now * math.sqrt(h)
            horizons.append({"h": h, "up": u, "flat": f2, "down": d2,
                             "thr": THR_SIGMA * sc, "n": int(ph["cal"]["n"]),
                             "mean": float(np.mean(yy)) * sc if len(yy) else 0.0,
                             "p10": float(np.quantile(yy, 0.10)) * sc if len(yy) else 0.0,
                             "p50": float(np.quantile(yy, 0.50)) * sc if len(yy) else 0.0,
                             "p90": float(np.quantile(yy, 0.90)) * sc if len(yy) else 0.0})
        except Exception:
            continue

    # 多周期一致性:5/10/20 日方向朝同一侧,信号更可信。
    # 不一致时放宽阈值,别把短期噪声当方向。
    _dirs = []
    for _hh in horizons:
        _e = _hh["up"] - _hh["down"]
        _dirs.append(1 if _e > 0.02 else (-1 if _e < -0.02 else 0))
    consistency = abs(sum(_dirs)) / len(_dirs) if _dirs else 0.0
    verdict, advice = _verdict(up, flat, down,
                               skill=float(bt.get("skill") or 0.0),
                               consistency=consistency)

    cond = y_me[np.isfinite(s_me) & np.isfinite(y_me) & (np.abs(s_me - s_now) <= SIMILAR_BAND)]
    if len(cond) < 10:
        cond = y_me[np.isfinite(y_me)]
    sc = sigma_now * math.sqrt(horizon)
    quant = {
        "p10": float(np.quantile(cond, 0.10)) * sc if len(cond) else 0.0,
        "p25": float(np.quantile(cond, 0.25)) * sc if len(cond) else 0.0,
        "p50": float(np.quantile(cond, 0.50)) * sc if len(cond) else 0.0,
        "p75": float(np.quantile(cond, 0.75)) * sc if len(cond) else 0.0,
        "p90": float(np.quantile(cond, 0.90)) * sc if len(cond) else 0.0,
        "mean": float(np.mean(cond)) * sc if len(cond) else 0.0,
        "n": int(len(cond)),
    }

    reasons_up: list[str] = []
    reasons_down: list[str] = []
    for f2 in sorted(factors, key=lambda x: x["contrib"], reverse=True):
        if abs(f2["contrib"]) < 0.02:
            continue
        tone = f"({f2['follow']}计)" if f2["follow"] != "顺势" else ""
        line = f"{f2['name']} {f2['contrib']:+.2f}{tone}:{f2['detail']}"
        (reasons_up if f2["contrib"] > 0 else reasons_down).append(line)
    for o in overlay_rows:
        if abs(o["shift"]) < 0.04:
            continue
        line = f"{o['name']} {o['shift']:+.2f}:{o['reason']}"
        (reasons_up if o["shift"] > 0 else reasons_down).append(line)
    if not reasons_up and not reasons_down:
        reasons_up.append("没有稳定信号:因子方向接近 0,概率按历史基准给")

    base_up = float(np.mean(np.concatenate([f3["_y"][np.isfinite(f3["_y"])]
                                            for f3 in pipe["frames"]]) > THR_SIGMA))
    return {
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "code": code,
        "name": name or me["name"] or INDEX_NAMES.get(code, code),
        "kind": "index" if is_index else "stock",
        "horizon": horizon,
        "price": float(me["close"][-1]),
        "date": me["dates"][-1],
        "bars": len(me["close"]),
        "source": me.get("src", ""),
        "score": s_now,
        "probs": {"up": up0, "flat": flat0, "down": down0},
        "probs_adj": {"up": up, "flat": flat, "down": down},
        "shift": shift,
        "verdict": verdict,
        "advice": advice,
        "ci": ci,
        "confidence": conf,
        "threshold": thr_now,
        "sigma": sigma_now,
        "samples": int(pipe["cal"]["n"]),
        "pool": len(pipe["frames"]),
        "bucket": bucket,
        "factors": factors,
        "overlay": overlay_rows,
        "buckets": buckets,
        "similar": similar,
        "horizons": horizons,
        "quantiles": quant,
        "backtest": bt,
        "base_up": base_up,
        "reasons_up": reasons_up[:6],
        "reasons_down": reasons_down[:6],
        "macro": macro_rep if is_index else None,
        "missing": missing,
        "notes": list(NOTES),
    }


def market_rows(horizon: int = DEFAULT_HORIZON) -> list[dict]:
    """四个主要指数的方向概率一览。"""
    out: list[dict] = []
    for code, name in INDEX_LIST:
        try:
            rep = forecast(code, horizon=horizon, kind="index", name=name, overlay=False)
            out.append({"code": code, "name": name, "price": rep["price"],
                        "up": rep["probs"]["up"], "flat": rep["probs"]["flat"],
                        "down": rep["probs"]["down"], "score": rep["score"],
                        "verdict": rep["verdict"], "samples": rep["samples"],
                        "skill": (rep.get("backtest") or {}).get("skill")})
        except Exception as e:  # noqa: BLE001
            out.append({"code": code, "name": name, "price": None, "up": None,
                        "flat": None, "down": None, "score": None,
                        "verdict": f"取数失败 {str(e)[:24]}", "samples": 0, "skill": None})
    return out


def batch(codes: list[str], horizon: int = DEFAULT_HORIZON, progress=None) -> list[dict]:
    """批量扫个股(纯统计概率,不叠加修正)。"""
    out: list[dict] = []
    for i, code in enumerate(codes):
        if progress:
            try:
                progress(i, len(codes))
            except Exception:
                pass
        try:
            rep = forecast(code, horizon=horizon, kind="stock", overlay=False)
            out.append({"code": rep["code"], "name": rep["name"], "price": rep["price"],
                        "up": rep["probs"]["up"], "flat": rep["probs"]["flat"],
                        "down": rep["probs"]["down"], "score": rep["score"],
                        "verdict": rep["verdict"], "samples": rep["samples"],
                        "skill": (rep.get("backtest") or {}).get("skill")})
        except Exception as e:  # noqa: BLE001
            out.append({"code": normalize_code(code), "name": normalize_code(code),
                        "price": None, "up": None, "flat": None, "down": None,
                        "score": None, "verdict": f"失败 {str(e)[:20]}", "samples": 0,
                        "skill": None})
    out.sort(key=lambda r: -(r["up"] if r["up"] is not None else -1.0))
    return out


def text(rep: dict) -> str:
    pa = rep["probs_adj"]
    lines = [
        f"{rep['name']}({rep['code']}) 走势预判 · {rep['asof']} · 未来 {rep['horizon']} 个交易日",
        f"现价 {rep['price']:.2f}({rep['date']}) · 分数 {rep['score']:+.2f} · 判定 {rep['verdict']}",
        f"概率:涨 {pa['up'] * 100:.1f}% / 平 {pa['flat'] * 100:.1f}% / 跌 {pa['down'] * 100:.1f}%"
        f" · 横盘带 ±{rep['threshold'] * 100:.1f}%",
        f"置信 {rep['confidence']} · 池化样本 {rep['samples']}({rep['pool']} 只)"
        f" · 区间 [{rep['ci'][0] * 100:.0f}%, {rep['ci'][1] * 100:.0f}%]"
        + (f" · 修正 {rep['shift']:+.2f} logit" if rep.get("shift") else ""),
    ]
    if rep.get("backtest"):
        bt = rep["backtest"]
        lines.append(f"走查 {bt['n']} 次:三分类命中 {bt['hit'] * 100:.1f}%"
                     f"(多数类 {bt['hit_naive'] * 100:.1f}%),"
                     f"方向命中 {bt['dir_hit'] * 100:.1f}%({bt['dir_n']} 次真涨跌),"
                     f"基准 {bt['dir_base'] * 100:.1f}%,"
                     f"技能分 {bt['skill'] * 100:+.1f}%")
        sm = bt.get("strong") or {}
        if sm.get("n"):
            lines.append(f"强信号子集(方向边际≥{sm['edge']:.2f}):{sm['n']} 次,"
                         f"方向命中 {sm['hit'] * 100:.1f}%,"
                         f"基率 {sm['base'] * 100:.1f}% —— "
                         + ("有超额" if sm["hit"] - sm["base"] >= 0.02 else "无超额"))
    if rep.get("reasons_up"):
        lines.append("看多:" + ";".join(rep["reasons_up"][:4]))
    if rep.get("reasons_down"):
        lines.append("看空:" + ";".join(rep["reasons_down"][:4]))
    q = rep.get("quantiles") or {}
    if q:
        lines.append(f"情景分布({rep['horizon']} 日):P10 {q['p10'] * 100:+.1f}% / 中位 "
                     f"{q['p50'] * 100:+.1f}% / P90 {q['p90'] * 100:+.1f}%")
    return "\n".join(lines)
