# -*- coding: utf-8 -*-
"""涨幅预选:全市场横截面快扫,给出「接下来一段时间涨幅可能略大」的几只票。

## 为什么和 forecast.py 分开

forecast() 每只票要建 60 只的池子、逐个走查 800 根,单只约一分钟 ——
拿它扫全市场不可行。这里的做法是**一次把整张横截面读进内存**
(本地 SQLite 缓存,不逐只联网),再用走查 RankIC 权重的向量化因子打分。
池子按成交额取前 `max_stocks` 只(默认 1200),几秒钟出结果。

## 边界(写在代码里,不是写在文档里)

memory/quantlab-alpha-verdict.md 的结论:**45 因子等权在样本外超额
+0.570%/期(t=2.23),beta 中性化后 −0.436%(t=−1.55)** —— 优势来自 beta。
所以这里做的是:

  1. 同时算「原始分」和「beta 中性化分」,历史同分位也两套都跑;
  2. 只有中性化后仍有正超额才算真信号,否则页面明说「这次排的是 beta」;
  3. 预期幅度是**历史同分位均值按波动缩放**的参考值,不是收益承诺;
  4. 每只票都落进 sl_predictions,到期由 predict_learn.review_pending 回填,
     判对判错、哪个因子在骗人,全都留痕 —— 这是「记录学习」那一半。

## 数据来源

  * 日线   data/quantlab.db 的 kline_cache(klt=101, fqt=1 前复权)
  * 名称   data/em.quotes 批量取(失败就退回代码,不编)
  * 资金   只读 moneyflow.peek 缓存,不触发请求
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import pandas as pd

from ..data.store import store

HORIZON = 5          # 默认看 5 个交易日
DAYS = 260           # 回看多少根日线(算 IC 和权重用)
MAX_STOCKS = 1200    # 池子上限:成交额前 N。再多就是给噪声算力
MIN_BARS = 120       # 少于这么多根不参与
MIN_AMOUNT = 5e7     # 最后一日均额下限,太小的票买不进也卖不出
TOP_N = 5            # 给几只
LOOKBACK = 120       # 同分位回看多少个交易日
MIN_T = 1.0          # |t| 低于这个的因子当期不给权重
IC_FLOOR = 0.004     # IC 太小的因子也没意义,权重清零
TOP_PCT = 0.05       # 同分位口径:前 5%
COST_ROUND_TRIP = 0.0016  # 一个来回的费(佣金+印花税+滑点),与 factor_analysis 同口径
HOLD_SCAN = (1, 2, 3, 5, 10)  # 「拿几天」必须逐个实测,不能凭感觉给

FACTOR_LABEL = {
    "mom20": "20日动量", "mom60": "60日动量", "rev5": "5日反转",
    "lowvol20": "低波动", "amtheat": "成交额热度", "matrend": "均线多头",
    "breakout20": "20日突破", "cmf20": "资金流CMF", "pos60": "价格位置",
    "beta20": "20日Beta", "resi20": "残差动量", "kmid": "K线实体",
    "vwapdev5": "VWAP偏离",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pick_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    day      TEXT NOT NULL,
    horizon  INTEGER NOT NULL,
    universe INTEGER,
    n_pick   INTEGER,
    weights  TEXT,
    ic       TEXT,
    top_mean REAL,
    bench    REAL,
    excess   REAL,
    excess_t REAL,
    excess_neu REAL,
    excess_neu_t REAL,
    prob_up  REAL,
    verdict  TEXT,
    elapsed  REAL,
    note     TEXT,
    rows     TEXT,
    exp_abs  REAL
);
CREATE INDEX IF NOT EXISTS idx_pick_day ON pick_runs(day);
"""

_ready = False
_PANEL_CACHE: dict = {}


def _db():
    global _ready
    st = store()
    if not _ready:
        with st.conn as c:
            c.executescript(_SCHEMA)
            # 老库没有新列:补上,别让整张表报废
            cols = {r["name"] for r in c.execute("PRAGMA table_info(pick_runs)")}
            for name, kind in (("excess_neu", "REAL"), ("excess_neu_t", "REAL"),
                               ("rows", "TEXT"), ("exp_abs", "REAL")):
                if name not in cols:
                    c.execute(f"ALTER TABLE pick_runs ADD COLUMN {name} {kind}")
        _ready = True
    return st


# ==================================================================== 面板

def load_panel(days: int = DAYS, max_stocks: int = MAX_STOCKS,
               min_bars: int = MIN_BARS, asof: str | None = None) -> dict:
    """一次读出日线横截面,只保留成交额前 max_stocks 只。

    asof 决定「按哪一天的成交额选票」:
      None               最近一个交易日 —— 选股/看盘用这个
      'YYYY-MM-DD'       指定日 —— **回测必须传样本起点**,
                         否则是拿今天的成交额去挑历史池子(幸存者偏差)
    """
    key = (int(days), int(max_stocks), int(min_bars), asof)
    hit = _PANEL_CACHE.get(key)
    if hit is not None:
        return hit
    st = _db()
    rows = st.conn.execute(
        "SELECT DISTINCT bar_time FROM kline_cache WHERE klt=101 AND fqt=1 "
        "ORDER BY bar_time DESC LIMIT ?", (int(days),)).fetchall()
    if not rows:
        return {}
    dts = [r["bar_time"] for r in rows]
    start, end = min(dts), max(dts)

    # 先在库里挑出「值得读」的票,再取数据。
    # 以前是全市场读进来(5000 多只 × 260 天 ≈ 130 万行)再截前 1200 只,光读库 40 秒。
    # 现在分两步:
    #   a) 拿最近一个交易日的成交额排序(只扫一天,快);
    #   b) 对这批票再按整段的均额复核(只在几千只上聚合,不碰全表)。
    cand_sql = (
        "SELECT code, COALESCE(NULLIF(amount,0), close*volume*100.0) AS av "
        "FROM kline_cache WHERE klt=101 AND fqt=1 AND bar_time=? "
        "AND length(code)=6 AND substr(code,1,1) IN ('0','3','6') "
        "AND close IS NOT NULL ORDER BY av DESC LIMIT ?")
    codes: list[str] = []
    # 回测传了 asof 就按那一天的成交额选票,不碰「最近几天」
    pick_days = ([asof] if asof else sorted(dts, reverse=True)[:6])
    for day in pick_days:
        got = st.conn.execute(cand_sql, (day, int(max_stocks) * 2)).fetchall()
        codes = [r["code"] for r in got if r["code"]]
        if len(codes) >= int(max_stocks):
            break
    if codes:
        marks = ",".join("?" * len(codes))
        q = ("SELECT code,bar_time,open,high,low,close,volume,amount FROM kline_cache "
             f"WHERE klt=101 AND fqt=1 AND bar_time>=? AND bar_time<=? AND code IN ({marks})")
        df = pd.read_sql_query(q, st.conn, params=[start, end] + codes)
    else:
        q = ("SELECT code,bar_time,open,high,low,close,volume,amount FROM kline_cache "
             "WHERE klt=101 AND fqt=1 AND bar_time>=? AND bar_time<=?")
        df = pd.read_sql_query(q, st.conn, params=(start, end))
    if df.empty:
        return {}
    df = df[df["code"].astype(str).str.len() == 6]
    df = df[df["code"].astype(str).str[0].isin(list("036"))]
    df["bar_time"] = pd.to_datetime(df["bar_time"])

    def wide(col):
        return df.pivot(index="bar_time", columns="code", values=col).sort_index()

    close = wide("close")
    amt = wide("amount")
    vol = wide("volume")
    # 云端 / 不同数据源的 amount 可能整列是 None 或字符串,不强制转成 float 的话
    # 后面 np.log1p 会撞 "argument 0 of type float has no callable log1p method"。
    close = close.apply(pd.to_numeric, errors="coerce").astype("float64")
    amt = amt.apply(pd.to_numeric, errors="coerce").astype("float64")
    vol = vol.apply(pd.to_numeric, errors="coerce").astype("float64")
    # 成交额缺失(腾讯/新浪源)用 close×volume×100 估,和 factors._amount 同口径
    amt = amt.where(amt.notna() & (amt > 0), close * vol * 100.0)

    # 只留够长、且最近还在交易的票。
    # 关键:判「最近」不能用整张表的最后一行 —— 盘中有票先落库,那一行只有
    # 几十只,其余全被当成「早就不交易了」剔掉(实测池子从 1200 掉到 57)。
    # 改成按每只票自己的最后一根,和全表最新交易日比。
    enough = close.notna().sum() >= int(min_bars)
    last_seen = close.apply(lambda s: s.last_valid_index())
    newest = close.index[-1]
    # 允许落后 3 个交易日(停牌/数据源慢),再多就当它退市或长期停牌
    lag_ok = last_seen >= newest - pd.Timedelta(days=5)
    keep = [c for c in close.columns
            if bool(enough.get(c)) and bool(lag_ok.get(c))]
    if len(keep) > int(max_stocks):
        rank = amt[keep].iloc[-20:].mean().sort_values(ascending=False)
        keep = list(rank.index[: int(max_stocks)])

    p = {"close": close[keep], "open": wide("open")[keep],
         "high": wide("high")[keep], "low": wide("low")[keep],
         "volume": vol[keep], "amount": amt[keep]}
    # 同上,OHLC 也要坐实成 float(新浪源个别字段是字符串)
    for _k2 in ("open", "high", "low"):
        p[_k2] = p[_k2].apply(pd.to_numeric, errors="coerce").astype("float64")

    # 砍掉末尾「半天」的截面。盘中/收盘后刚落库时最后一行往往只有几十只票,
    # 拿它打分等于只在那几十只里排序(实测池子 1200 → 57)。
    # 判据:该行覆盖数 < 前 60 行中位数的 40%,就退一天,直到正常。
    cov = p["close"].notna().sum(axis=1)
    for _ in range(10):
        if len(cov) < 80:
            break
        base = float(cov.iloc[-61:-1].median()) if len(cov) > 61 else float(cov.median())
        if base <= 0 or float(cov.iloc[-1]) >= base * 0.4:
            break
        for k in ("close", "open", "high", "low", "volume", "amount"):
            p[k] = p[k].iloc[:-1]
        cov = p["close"].notna().sum(axis=1)

    p["last_day"] = str(p["close"].index[-1].date())
    p["codes"] = keep
    _PANEL_CACHE.clear()
    _PANEL_CACHE[key] = p
    return p


# ==================================================================== 因子

def _market(close: pd.DataFrame) -> pd.Series:
    """横截面等权当日收益,当市场代理(与 factors.market_proxy 同口径)。"""
    return close.pct_change().mean(axis=1, skipna=True)


def _factors(p: dict) -> dict[str, pd.DataFrame]:
    c, o, h, l = p["close"], p["open"], p["high"], p["low"]
    v, a = p["volume"], p["amount"]
    ret = c.pct_change()
    m = _market(c)

    w = 20
    mp = max(5, w // 2)
    cov = (ret.mul(m, axis=0)).rolling(w, min_periods=mp).mean() - \
        ret.rolling(w, min_periods=mp).mean().mul(m.rolling(w, min_periods=mp).mean(), axis=0)
    beta = cov.div(m.rolling(w, min_periods=mp).var().replace(0, np.nan), axis=0)

    rng = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)).div(rng)
    cmf = (mfm * v).rolling(20, min_periods=10).sum() / \
        v.rolling(20, min_periods=10).sum().replace(0, np.nan)
    vwap = a / (v * 100.0 + 1e-12)

    la = np.log1p(a.clip(lower=0))
    ma5, ma20, ma60 = [c.rolling(n, min_periods=max(2, n // 2)).mean()
                       for n in (5, 20, 60)]
    lo60 = c.rolling(60, min_periods=20).min()
    hi60 = c.rolling(60, min_periods=20).max()

    return {
        "mom20": c.pct_change(20),
        "mom60": c.pct_change(60),
        "rev5": c.pct_change(5),
        "lowvol20": -ret.rolling(20, min_periods=10).std(),
        "amtheat": la.rolling(5, min_periods=2).mean() /
        la.rolling(60, min_periods=20).mean().replace(0, np.nan),
        "matrend": (ma5 > ma20).astype("float64") + (ma20 > ma60).astype("float64"),
        "breakout20": c / c.rolling(20, min_periods=10).max(),
        "cmf20": cmf,
        "pos60": (c - lo60) / (hi60 - lo60).replace(0, np.nan),
        "beta20": beta,
        "resi20": (ret - beta.mul(m, axis=0)).rolling(20, min_periods=10).sum(),
        "kmid": (c - o) / o.replace(0, np.nan),
        "vwapdev5": (vwap / c.replace(0, np.nan) - 1).rolling(5, min_periods=2).mean(),
    }


def _rank(p: pd.DataFrame) -> pd.DataFrame:
    """横截面百分位 rank。一次算好,IC 和打分都复用这一份,别重复排。"""
    return p.rank(axis=1, pct=True)


def _rank_ic(fr: pd.DataFrame, rr: pd.DataFrame,
             lookback: int = LOOKBACK) -> dict:
    """走查 RankIC:每个交易日横截面相关,只取最近 lookback 天。"""
    ic = fr.corrwith(rr, axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    ic = ic.tail(int(lookback))
    if len(ic) < 20:
        return {"n": int(len(ic)), "ic": 0.0, "t": 0.0}
    mean = float(ic.mean())
    std = float(ic.std(ddof=1)) or 1e-9
    return {"n": int(len(ic)), "ic": round(mean, 4),
            "t": round(mean / std * math.sqrt(len(ic)), 2)}


def _weights(ics: dict[str, dict]) -> dict[str, float]:
    """|IC| 加权,方向由 IC 符号定;|t| 不显著或 |IC| 太小的一律给 0。

    不给「勉强有效」的因子留一点权重 —— 那等于用噪声稀释真信号。
    """
    w = {}
    for k, st in ics.items():
        if st.get("n", 0) < 20 or abs(float(st.get("ic") or 0)) < IC_FLOOR:
            continue
        if abs(float(st.get("t") or 0)) < MIN_T:
            continue
        w[k] = float(st["ic"])
    total = sum(abs(x) for x in w.values())
    if total <= 0:
        return {}
    return {k: round(x / total, 4) for k, x in w.items()}


def _neutralize_row(y: pd.Series, x: pd.Series) -> pd.Series:
    """单行横截面对 beta 做一维回归取残差(同 factor_analysis.beta_neutralize)。"""
    x = x.reindex(y.index)
    m = y.notna() & x.notna()
    if int(m.sum()) < 20:
        return y
    cols = list(y.index[m])
    xv = np.asarray(x.loc[cols].values, dtype="float64").reshape(-1, 1)
    yv = np.asarray(y.loc[cols].values, dtype="float64")
    lo, hi = np.percentile(xv, [1, 99])
    ok = (xv.ravel() >= lo) & (xv.ravel() <= hi)
    if int(ok.sum()) < 15:
        return y
    coef = np.linalg.lstsq(xv[ok], yv[ok], rcond=None)[0]
    out = y.copy()
    out.loc[cols] = yv - (xv @ coef).ravel()
    return out


# ==================================================================== 主流程

def _limit_gap(code: str) -> float:
    try:
        from ..utils import limit_pct
        return float(limit_pct(code)) * 100.0
    except Exception:  # noqa: BLE001
        return 10.0


def _names(codes: list[str]) -> dict[str, str]:
    """批量取名称。东财挂了就退腾讯、再退新浪 —— 名称不影响打分,不该拖垮整页。"""
    if not codes:
        return {}
    try:
        from ..data import em
        q = em.quotes(list(codes), fallback=False, retries=1, timeout=6.0)
        got = {c: str((q.get(c) or {}).get("name") or "") for c in codes}
        if sum(1 for v in got.values() if v) >= len(codes) // 2:
            return got
    except Exception:  # noqa: BLE001
        pass
    for mod, kw in (("tx", {"timeout": 4.0, "retries": 1}), ("sina", {})):
        try:
            mod_ = __import__(f"quantlab.data.{mod}", fromlist=["quotes"])
            q = mod_.quotes(list(codes), **kw)
            got = {c: str((q.get(c) or {}).get("name") or "") for c in codes}
            if any(got.values()):
                return got
        except Exception:  # noqa: BLE001
            continue
    return {c: "" for c in codes}


def _moneyflow(codes: list[str]) -> dict[str, dict]:
    """只吃已有缓存,不联网 —— 页面刷新不该因为资金接口卡住。"""
    out = {}
    try:
        from ..data import moneyflow as mf
        for c in codes:
            rec = mf.peek(c)
            if rec:
                out[c] = mf._public(rec)
    except Exception:  # noqa: BLE001
        return {}
    return out


def scan(days: int = DAYS, horizon: int = HORIZON, top: int = TOP_N,
         min_amount: float = MIN_AMOUNT, max_stocks: int = MAX_STOCKS,
         only_up: bool = False, progress=None) -> dict:
    """扫一遍横截面,返回候选与评估。几秒钟,可以直接在后台线程里跑。

    only_up=True 时只留「当日还在涨」的票。默认 False 是有原因的:
    这套因子是横截面排序,排的是「谁比谁强」,不看大盘方向 ——
    市场普跌那天,分数最高的自然就是跌得少的那些。想只做上涨的,
    要么开这个开关,要么看每行的「预期涨跌」那一列。
    """
    t0 = time.time()
    p = load_panel(days, max_stocks)
    if not p:
        return {"ok": False, "why": "本地没有日线缓存(先打开行情页拉一批)"}
    if progress:
        progress(1, 5, "算因子")
    close = p["close"]
    fs = _factors(p)
    fr = {k: _rank(v) for k, v in fs.items()}
    fwd = _rank(close.shift(-int(horizon)) / close - 1.0)

    ics = {k: _rank_ic(v, fwd) for k, v in fr.items()}
    w = _weights(ics)
    if not w:
        return {"ok": False, "why": "当期没有显著因子(全部 |t|<1 或 |IC| 太小)"}

    if progress:
        progress(2, 5, "合成打分")
    comp = None
    for k, wt in w.items():
        r = fr[k] - 0.5
        comp = r * wt if comp is None else comp.add(r * wt, fill_value=0)
    last = comp.index[-1]
    raw = comp.loc[last].dropna()
    beta_last = fs["beta20"].loc[last]
    neu = _neutralize_row(raw, beta_last).dropna()

    amt_last = p["amount"].loc[last]
    chg_last = close.loc[last] / close.shift(1).loc[last] - 1.0
    cand = [c for c in neu.index
            if float(amt_last.get(c) or 0) >= float(min_amount)
            and np.isfinite(float(chg_last.get(c) or np.nan))
            and float(chg_last.get(c)) * 100.0 < _limit_gap(c) - 0.3
            and (not only_up or float(chg_last.get(c)) > 0)]
    if not cand:
        return {"ok": False,
                "why": ("今天没有既在涨、又过成交额门槛的票(市场普跌时很正常)"
                        if only_up else "过滤后没有可买标的(成交额/涨停)")}
    picked = sorted(cand, key=lambda c: -float(neu[c]))[:int(top)]

    # 历史同分位:两套口径都跑(beta 暴露 vs 中性化),这才是诚实的比较
    if progress:
        progress(3, 5, "回看同分位")
    dates = comp.index[-LOOKBACK:]
    fwd_ret = close.shift(-int(horizon)) / close - 1.0
    bt = {"raw": ([], []), "neu": ([], [])}
    for d in dates:
        row = comp.loc[d].dropna()
        if len(row) < 40:
            continue
        ret_d = fwd_ret.loc[d].reindex(row.index).dropna()
        if len(ret_d) < 40:
            continue
        row = row.reindex(ret_d.index)
        k = max(1, int(len(row) * TOP_PCT))
        nr = _neutralize_row(row, fs["beta20"].loc[d]).dropna()
        if len(nr) >= 40:
            sel = nr.nlargest(k).index
            bt["neu"][0].append(float(ret_d.reindex(sel).mean()))
            bt["neu"][1].append(float(ret_d.mean()))
        sel = row.nlargest(k).index
        bt["raw"][0].append(float(ret_d.reindex(sel).mean()))
        bt["raw"][1].append(float(ret_d.mean()))

    def _stat(a, b):
        if not a:
            return 0.0, 0.0, 0.0
        x, y = np.asarray(a), np.asarray(b)
        diff = x - y
        t = (float(diff.mean() / diff.std(ddof=1) * math.sqrt(len(diff)))
             if len(diff) > 2 and diff.std(ddof=1) > 0 else 0.0)
        return float(x.mean()), float(diff.mean()), round(t, 2)

    top_mean, excess, excess_t = _stat(*bt["raw"])
    neu_mean, excess_neu, excess_neu_t = _stat(*bt["neu"])
    bench = float(np.mean(bt["raw"][1])) if bt["raw"][1] else 0.0
    prob_up = float(np.mean([x > 0 for x in bt["raw"][0]])) if bt["raw"][0] else 0.0
    # 绝对预期涨跌 = 同分位选出来的票在 horizon 日的平均收益(未扣基准)。
    # 以前只给「超额」,用户看到全是跌的票会以为模型在做空 ——
    # 超额讲的永远是「比大盘好多少」,和「会不会涨」是两件事。两个都给。
    exp_abs = float(top_mean)
    if excess_neu > 0 and excess_neu_t >= 2:
        verdict = "中性化后仍有正超额"
    elif excess_t >= 2:
        verdict = "排的是 beta 暴露,不是 alpha"
    elif excess_t <= -2:
        verdict = "同分位跑输,这批别信"
    else:
        verdict = "无显著超额,只能当候选清单"

    if progress:
        progress(4, 5, "出名单")
    names = _names(picked)
    flows = _moneyflow(picked)
    sig = close.pct_change().rolling(20, min_periods=10).std().loc[last]
    med_sig = float(sig.reindex(cand).median() or 0.0) or 1.0

    rows = []
    for c in picked:
        s5 = float(sig.get(c) or 0.0) * math.sqrt(int(horizon))
        scale = (s5 / (med_sig * math.sqrt(int(horizon)))) if med_sig else 1.0
        contrib = {}
        for k in w:
            r = fr[k].loc[last].get(c, np.nan)
            contrib[k] = float(w[k] * (float(r) - 0.5)) if np.isfinite(r) else 0.0
        top_f = sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:3]
        fl = flows.get(c) or {}
        c5 = close.shift(5).loc[last].get(c, np.nan)
        rows.append({
            "code": c, "name": names.get(c, ""),
            "price": float(close.loc[last].get(c) or 0.0),
            "chg": float(chg_last.get(c) or 0.0),
            "ret5": (float(close.loc[last].get(c) / c5 - 1.0)
                     if np.isfinite(c5) and c5 else None),
            "amount": float(amt_last.get(c) or 0.0),
            "score": float(neu[c]),
            "score_raw": float(raw.get(c, np.nan)),
            "beta": float(beta_last.get(c) or np.nan),
            "sigma": s5,
            "exp": excess_neu * scale if excess_neu_t >= 2 else top_mean * scale,
            # 这只票的「同分位历史平均涨跌」(绝对口径,不扣基准)。
            # exp 是超额,exp_abs 是涨跌 —— 页面把两个分开显示,免得看成做空。
            "exp_abs": exp_abs,
            "prob_up": prob_up,
            "main_in": fl.get("main"),
            "factors": [{"key": k, "name": FACTOR_LABEL.get(k, k),
                         "contrib": round(v, 4)} for k, v in top_f],
        })

    if progress:
        progress(5, 5, "完成")
    return {
        "ok": True, "day": p["last_day"], "horizon": int(horizon),
        "universe": len(p["codes"]), "n_cand": len(cand),
        "rows": rows, "weights": w, "ic": ics,
        "top_mean": top_mean, "bench": bench, "excess": excess,
        "excess_t": excess_t, "excess_neu": excess_neu,
        "excess_neu_t": excess_neu_t, "prob_up": prob_up,
        "exp_abs": exp_abs,
        "verdict": verdict, "elapsed": round(time.time() - t0, 2),
        "n_factors": len(w),
    }


# ==================================================================== 落档与回传

def save_run(rep: dict, note: str = "") -> int:
    st = _db()
    with st.conn as c:
        cur = c.execute(
            "INSERT INTO pick_runs(ts,day,horizon,universe,n_pick,weights,ic,"
            "top_mean,bench,excess,excess_t,excess_neu,excess_neu_t,prob_up,"
            "verdict,elapsed,note,rows,exp_abs) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), str(rep.get("day") or ""), int(rep.get("horizon") or 5),
             int(rep.get("universe") or 0), len(rep.get("rows") or []),
             json.dumps(rep.get("weights") or {}, ensure_ascii=False),
             json.dumps(rep.get("ic") or {}, ensure_ascii=False),
             float(rep.get("top_mean") or 0), float(rep.get("bench") or 0),
             float(rep.get("excess") or 0), float(rep.get("excess_t") or 0),
             float(rep.get("excess_neu") or 0), float(rep.get("excess_neu_t") or 0),
             float(rep.get("prob_up") or 0), str(rep.get("verdict") or ""),
             float(rep.get("elapsed") or 0), note,
             json.dumps(rep.get("rows") or [], ensure_ascii=False, default=str),
             float(rep.get("exp_abs") or 0)))
        return int(cur.lastrowid or 0)


def recent_runs(limit: int = 20) -> list[dict]:
    st = _db()
    rows = st.conn.execute(
        "SELECT * FROM pick_runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def _as_rep(row: dict, rep: dict) -> dict:
    """把候选行装成 predict_learn.record 吃的那种结构。

    record 只认 probs_adj / horizon / verdict / confidence / threshold /
    backtest / factors —— 按那个口径造,回传与回填链路零改动。
    """
    pu = float(row.get("prob_up") or 0.5)
    sigma = float(row.get("sigma") or 0.0)
    thr = max(0.005, 0.5 * sigma)
    exp = float(row.get("exp") or 0.0)
    up = min(0.85, max(0.15, pu if exp >= 0 else 1.0 - pu))
    down = min(0.85, max(0.05, (1.0 - up) * 0.6))
    flat = max(0.02, 1.0 - up - down)
    return {
        "name": row.get("name") or "",
        "horizon": int(rep.get("horizon") or 5),
        "probs_adj": {"up": round(up, 4), "flat": round(flat, 4),
                      "down": round(down, 4)},
        "verdict": "偏多" if up >= 0.5 else "偏空",
        "confidence": "高" if abs(float(rep.get("excess_neu_t") or 0)) >= 2 else "中",
        "threshold": round(thr, 4),
        "backtest": {"skill": None, "calib_err": None},
        "factors": row.get("factors") or [],
    }


def record_picks(rep: dict, account: str = "default") -> dict:
    """把候选写进 sl_predictions 并回传云端。返回 {ok, ids, failed}。"""
    from . import predict_learn as pl
    log = pl.PredictLog(account)
    ids, bad = [], []
    for row in rep.get("rows") or []:
        try:
            note = (f"pick|{rep.get('verdict', '')}"
                    f"|exp={float(row.get('exp') or 0):.4f}"
                    f"|beta={float(row.get('beta') or 0):.2f}")
            ids.append(log.record(str(row.get("code")), _as_rep(row, rep),
                                  day=str(rep.get("day") or ""), note=note,
                                  strategy="pick"))
        except Exception as e:  # noqa: BLE001
            bad.append({"code": row.get("code"), "error": str(e)[:100]})
    return {"ok": bool(ids), "ids": ids, "failed": bad}


def hold_report(p: dict | None = None, days: int = DAYS,
                max_stocks: int = MAX_STOCKS) -> dict:
    """逐个持有天数实测:拿几天划算、次日卖能不能赚。

    用户真正要的是三件事 —— 买什么、持多久、次日能不能盈利卖出。
    前两个能算,第三个必须**扣手续费之后**再算,否则 1 天的涨幅
    还不够一次来回的费(实测毛均 +0.054%,扣费 −0.106%)。

    返回 {ok, table:[{hold, n, gross, net, win, win_net, excess, n_fac}],
          best, t1_ok, why}
    """
    p = p or load_panel(days, max_stocks)
    if not p:
        return {"ok": False, "why": "没有日线缓存", "table": []}
    close = p["close"]
    fs = _factors(p)
    fr = {k: _rank(v) for k, v in fs.items()}

    table = []
    for h in HOLD_SCAN:
        fwd = close.shift(-int(h)) / close - 1.0
        ics = {k: _rank_ic(v, _rank(fwd)) for k, v in fr.items()}
        w = _weights(ics)
        if not w:
            table.append({"hold": h, "n": 0, "gross": 0.0, "net": 0.0,
                          "win": 0.0, "win_net": 0.0, "excess": 0.0, "n_fac": 0})
            continue
        comp = None
        for k, wt in w.items():
            r = fr[k] - 0.5
            comp = r * wt if comp is None else comp.add(r * wt, fill_value=0)
        rets, mkt = [], []
        for d in comp.index[-LOOKBACK:]:
            row = comp.loc[d].dropna()
            if len(row) < 40:
                continue
            rd = fwd.loc[d].reindex(row.index).dropna()
            if len(rd) < 40:
                continue
            row = row.reindex(rd.index)
            k = max(1, int(len(row) * TOP_PCT))
            sel = row.nlargest(k).index
            rets.append(float(rd.reindex(sel).mean()))
            mkt.append(float(rd.mean()))
        if not rets:
            continue
        a = np.asarray(rets)
        net = a - COST_ROUND_TRIP
        table.append({
            "hold": int(h), "n": int(len(a)),
            "gross": float(a.mean()), "net": float(net.mean()),
            "win": float((a > 0).mean()), "win_net": float((net > 0).mean()),
            "excess": float(a.mean() - np.mean(mkt)) if mkt else 0.0,
            "n_fac": len(w),
        })
    if not table:
        return {"ok": False, "why": "没有可测的持有期", "table": []}

    # 最优 = 扣费后平均收益最高,但样本太少的不算(少于 60 次不可信)
    ok_rows = [r for r in table if r["n"] >= 60]
    best = max(ok_rows, key=lambda r: r["net"]) if ok_rows else None
    t1 = next((r for r in table if r["hold"] == 1), None)
    t1_ok = bool(t1 and t1["net"] > 0 and t1["win_net"] >= 0.5)
    if t1_ok:
        why = (f"次日卖出扣费后 {t1['net']:+.2%},盈利概率 {t1['win_net']:.0%}"
               f" —— 可以隔天走")
    elif t1:
        why = (f"次日卖出扣费后 {t1['net']:+.2%},盈利概率仅 {t1['win_net']:.0%}"
               f" —— 涨幅盖不住手续费,别隔天就卖")
    else:
        why = "次日样本不足"
    return {"ok": True, "table": table, "best": best, "t1": t1,
            "t1_ok": t1_ok, "why": why, "cost": COST_ROUND_TRIP}


def review(account: str = "default", limit: int = 200) -> dict:
    """回填到期预测(和走势预判共用一张表、一套回填)。"""
    from . import predict_learn as pl
    return pl.review_pending(account=account, limit=limit)


def learn_stats(account: str = "default") -> dict:
    from . import predict_learn as pl
    log = pl.PredictLog(account)
    return {"stats": log.stats(), "factors": log.factor_report()[:8],
            "recent": log.recent(limit=15)}
