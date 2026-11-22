# -*- coding: utf-8 -*-
"""因子分析:IC / IR / 正交化 / 行业中性 / IC 衰减 / 分层回测 / 冗余诊断。

对标 qlib 的因子评价:
  IC  = 因子值与未来收益的横截面相关系数
  RankIC = 先横截面 rank 再算相关(对极端值不敏感,qt 里更常用这一个)
  IR  = IC 均值 / IC 标准差
  正交化 = 剔除因子间共线性
  行业中性 = 剔除行业暴露

再做三件市面量化必做、但很多自研框架省掉的事:
  衰减   —— 同一个因子对未来 1/2/3/5/10 日收益的 IC,衰减快说明是短周期信号
  分层   —— 按因子分 5 组,看多空收益差(单调性)而不是只看相关系数
  冗余   —— 因子两两相关矩阵,超过 0.8 说明是同一个东西,别当多路用

只做分析,不改因子权重。权重怎么用由人决定。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import factors as fct


def _panel_frames(panel: dict[str, pd.DataFrame], factor_key: str,
                  horizon: int = 5,
                  market: pd.Series | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """构造因子矩阵和未来收益矩阵,索引=日期,列=代码。"""
    fvals, rets = {}, {}
    if market is None:
        market = fct.market_proxy(panel)
    for code, df in panel.items():
        if df is None or len(df) < horizon + 2:
            continue
        d = df.set_index("datetime")
        try:
            f = fct.factor_series(df, factor_key, market)
        except Exception:  # noqa: BLE001
            continue
        f.index = d.index
        r = d["close"].pct_change(horizon).shift(-horizon)
        fvals[code] = f
        rets[code] = r
    if not fvals:
        return pd.DataFrame(), pd.DataFrame()
    return pd.DataFrame(fvals), pd.DataFrame(rets)


def ic_series(panel: dict[str, pd.DataFrame], factor_key: str,
              horizon: int = 5, rank: bool = False) -> pd.Series:
    """逐日 IC:因子值与未来 horizon 日收益的横截面相关系数。

    rank=True 走 Rank IC(先横截面排名再算 Pearson,等价 Spearman 但不需要 scipy)。
    """
    f, r = _panel_frames(panel, factor_key, horizon)
    if f.empty or r.empty:
        return pd.Series(dtype=float)
    if rank:
        f = f.rank(axis=1, pct=True)
        r = r.rank(axis=1, pct=True)
    out = {}
    for day in f.index:
        fv = f.loc[day].dropna()
        rv = r.loc[day].dropna()
        common = fv.index.intersection(rv.index)
        if len(common) < 5:
            continue
        a = np.asarray(fv[common], dtype="float64")
        b = np.asarray(rv[common], dtype="float64")
        if a.std() < 1e-12 or b.std() < 1e-12:
            continue
        out[day] = float(np.corrcoef(a, b)[0, 1])
    return pd.Series(out).sort_index()


def ic_stats(panel: dict[str, pd.DataFrame], factor_key: str,
             horizon: int = 5, rank: bool = False) -> dict:
    """IC 均值、标准差、IR、胜率。rank=True 时 ic 字段是 Rank IC。"""
    ic = ic_series(panel, factor_key, horizon, rank=rank)
    if ic.empty:
        return {"n": 0, "ic": 0.0, "ir": 0.0, "win": 0.0, "t": 0.0}
    mean = float(ic.mean())
    std = float(ic.std()) or 1e-9
    return {"n": len(ic), "ic": round(mean, 4),
            "ir": round(mean / std, 3),
            "win": round(float((ic > 0).mean()), 3),
            # t 值:IC 均值的显著性(均值 / 标准误),|t|>2 才算有点东西
            "t": round(mean / std * float(np.sqrt(len(ic))), 2)}


def ic_decay(panel: dict[str, pd.DataFrame], factor_key: str,
             horizons: tuple[int, ...] = (1, 2, 3, 5, 10, 20),
             rank: bool = True) -> dict:
    """IC 衰减曲线:同一因子对不同持有期的 IC。

    返回 {"horizons": [...], "ic": [...], "ir": [...], "半衰期": k}。
    半衰期 = IC 掉到 H=1 时一半的第一个持有期,没有就记 None。
    """
    ics, irs = [], []
    for h in horizons:
        st = ic_stats(panel, factor_key, horizon=h, rank=rank)
        ics.append(float(st.get("ic") or 0.0))
        irs.append(float(st.get("ir") or 0.0))
    half = None
    if ics and abs(ics[0]) > 1e-9:
        thr = abs(ics[0]) * 0.5
        for h, v in zip(horizons, ics):
            if abs(v) <= thr:
                half = int(h)
                break
    return {"horizons": list(horizons), "ic": ics, "ir": irs, "half_life": half}


def layered(panel: dict[str, pd.DataFrame], factor_key: str, horizon: int = 5,
            groups: int = 5, rank: bool = True) -> dict:
    """分层回测:按因子值分 groups 组,看各组的平均未来收益。

    单调递增 + 多空差显著 = 因子能排序;中间乱跳说明只是两头的几个极端票在动。
    股票数少于 2×组数时自动降组数 —— 8 只票硬切 5 组只会切出空组。
    """
    f, r = _panel_frames(panel, factor_key, horizon)
    if f.empty or r.empty:
        return {"groups": [], "spread": 0.0, "monotonic": False, "n": 0}
    if rank:
        f = f.rank(axis=1, pct=True)
    # 每日有效股票数中位数决定实际能分几组
    per_day = f.notna().sum(axis=1)
    typical = int(per_day.median()) if len(per_day) else 0
    k = max(2, min(int(groups), typical // 2))
    if typical < 4:
        return {"groups": [], "spread": 0.0, "monotonic": False, "n": 0}
    acc: dict[int, list[float]] = {i: [] for i in range(k)}
    for day in f.index:
        fv = f.loc[day].dropna()
        if len(fv) < 4 or day not in r.index:
            continue
        rv = r.loc[day].reindex(fv.index).dropna()
        fv = fv.reindex(rv.index)
        if len(fv) < 4:
            continue
        # 用排名切组:第一组 = 因子最低
        try:
            q = pd.qcut(fv.rank(method="first"), k, labels=False, duplicates="drop")
        except Exception:  # noqa: BLE001
            continue
        for g, v in zip(np.asarray(q, dtype="float64"), np.asarray(rv, dtype="float64")):
            if np.isfinite(g) and np.isfinite(v):
                acc[int(g)].append(float(v))
    rows = []
    for g in range(k):
        vals = acc[g]
        rows.append({"group": g + 1,
                     "mean": round(float(np.mean(vals)), 6) if vals else None,
                     "n": len(vals)})
    means = [r0["mean"] for r0 in rows]
    spread = 0.0
    if means and means[0] is not None and means[-1] is not None:
        spread = round(float(means[-1] - means[0]), 6)
    mono = False
    if all(m is not None for m in means) and len(means) >= 3:
        diffs = np.diff(np.asarray(means, dtype="float64"))
        mono = bool((diffs > 0).all() or (diffs < 0).all())
    return {"groups": rows, "spread": spread,
            "monotonic": mono, "n": int(sum(r0["n"] for r0 in rows))}


def correlation(panel: dict[str, pd.DataFrame], keys: list[str],
                horizon: int = 5) -> pd.DataFrame:
    """因子两两相关矩阵(按横截面 rank 后,逐日求相关再平均)。

    同一族因子(比如 momentum20 与 roc10)相关性常在 0.9 以上,
    一起塞进多因子等于把权重重复计给同一个信号。
    """
    ranks: dict[str, pd.DataFrame] = {}
    for k in keys:
        f, _ = _panel_frames(panel, k, horizon)
        if not f.empty:
            ranks[k] = f.rank(axis=1, pct=True)
    if len(ranks) < 2:
        return pd.DataFrame()

    codes = list(ranks)
    acc = np.zeros((len(codes), len(codes)), dtype="float64")
    cnt = np.zeros((len(codes), len(codes)), dtype="float64")
    days = sorted(set().union(*[set(v.index) for v in ranks.values()]))
    for day in days:
        rows = {}
        for k, v in ranks.items():
            if day in v.index:
                rows[k] = v.loc[day]
        if len(rows) < 2:
            continue
        cor = pd.DataFrame(rows).corr(min_periods=5)
        for i, a in enumerate(codes):
            for j, b in enumerate(codes):
                if i == j or a not in cor.index or b not in cor.columns:
                    continue
                val = cor.at[a, b]
                if pd.notna(val):
                    acc[i, j] += float(val)
                    cnt[i, j] += 1.0
    mat = pd.DataFrame(np.eye(len(codes)), index=codes, columns=codes, dtype="float64")
    for i, a in enumerate(codes):
        for j, b in enumerate(codes):
            if i == j or cnt[i, j] <= 0:
                continue
            mat.at[a, b] = round(acc[i, j] / cnt[i, j], 4)
    return mat


def redundant_pairs(corr: pd.DataFrame, threshold: float = 0.8) -> list[dict]:
    """从相关矩阵里挑出冗余对(上三角,超过阈值)。"""
    if corr is None or corr.empty:
        return []
    out = []
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = corr.at[a, b]
            if pd.notna(v) and abs(float(v)) >= threshold:
                out.append({"a": a, "b": b, "corr": round(float(v), 4)})
    out.sort(key=lambda r: -abs(r["corr"]))
    return out


def orthogonalize(panel: dict[str, pd.DataFrame], keys: list[str],
                  horizon: int = 5) -> dict[str, pd.DataFrame]:
    """施密特正交化:按顺序剔除前面因子的线性影响。"""
    frames = {}
    for k in keys:
        f, _ = _panel_frames(panel, k, horizon)
        if not f.empty:
            frames[k] = f
    if not frames:
        return {}
    out = {}
    prev = None
    for k, f in frames.items():
        if prev is None:
            out[k] = f
            prev = f
            continue
        resid = f.copy()
        for day in f.index:
            y = f.loc[day].dropna()
            if len(y) < 5:
                continue
            X = prev.loc[day].reindex(y.index)
            mask = X.notna()
            if mask.sum() < 5:
                continue
            Xv = X[mask].values.reshape(-1, 1)
            yv = y[mask].values
            beta = np.linalg.lstsq(Xv, yv, rcond=None)[0]
            resid.loc[day, mask] = yv - Xv @ beta
        out[k] = resid
        prev = resid
    return out


def industry_neutral(panel: dict[str, pd.DataFrame], factor_key: str,
                     industry_map: dict[str, str],
                     horizon: int = 5) -> pd.DataFrame:
    """行业中性化:每个行业内做 z-score。

    不做这件事的后果:因子成了「买行业」的伪装。行业一旦反转,IC 立刻翻脸。
    """
    f, _ = _panel_frames(panel, factor_key, horizon)
    if f.empty:
        return f
    out = f.copy()
    for day in f.index:
        row = f.loc[day].dropna()
        if len(row) < 5:
            continue
        groups: dict[str, list[tuple[str, float]]] = {}
        for code, v in row.items():
            ind = industry_map.get(code, "其他")
            groups.setdefault(ind, []).append((code, float(v)))
        for _ind, items in groups.items():
            if len(items) < 2:
                continue
            vals = np.array([v for _, v in items])
            mu, sd = float(vals.mean()), float(vals.std()) or 1e-9
            for code, v in items:
                out.loc[day, code] = (v - mu) / sd
    return out


def walkforward_directions(panel: dict[str, pd.DataFrame], keys: list[str] | None = None,
                           horizon: int = 5, refit: int = 20,
                           min_t: float = 2.0, min_n: int = 40) -> pd.DataFrame:
    """逐期判定每个因子的方向(+1 顺势 / -1 反向 / 0 不用),严格因果。

    为什么必须这么做:实测沪深300(2024-01~2025-06)上 momentum60 的 Rank IC
    是 **-0.105(t=-5.05)**,momentum20 是 -0.060(t=-2.92)。也就是说这段行情里
    「过去涨得多的接下来跌得多」,而策略把「分高 = 看多」写死,方向直接反了。

    行业里没有哪个方向永久成立,A 股尤其如此:2019-2021 动量有效,2022-2025 反转有效。
    所以方向必须由数据决定,而且**只能用当时已经实现的那部分数据决定**:
    ic_series 在 d 日用 d 到 d+horizon 的收益算 IC,所以 d 日的 IC 要到 d+horizon
    才算"已知",t 时刻能用的最新 IC 日 = t - horizon。这条比多数开源实现都严格。

    返回 index=日期, columns=因子, 值 ∈ {-1, 0, +1}。0 表示 |t| < min_t 或样本不够,
    这类因子当期不给权重 —— 让它进组合只会稀释真正有效的信号。
    """
    keys = list(keys or fct.DEFAULT_KEYS)
    ics: dict[str, pd.Series] = {}
    for k in keys:
        try:
            s = ic_series(panel, k, horizon, rank=True)
        except Exception:  # noqa: BLE001
            s = pd.Series(dtype="float64")
        if not s.empty:
            ics[k] = s
    if not ics:
        return pd.DataFrame()
    dates = sorted(set().union(*[set(s.index) for s in ics.values()]))
    cols = list(ics)
    out = pd.DataFrame(0, index=pd.DatetimeIndex(dates), columns=cols, dtype="int8")
    step = max(int(refit), 1)
    for i, day in enumerate(dates):
        if i % step == 0:
            for k in cols:
                s = ics[k]
                # 位置裁:第 j 个 IC 用到了 j+horizon 的收益,所以只有 j <= pos-horizon 的已实现
                pos = int(s.index.searchsorted(day, side="right"))
                usable = pos - int(horizon)
                if usable < min_n:
                    continue
                vals = s.iloc[:usable]
                n = len(vals)
                mean = float(vals.mean())
                std = float(vals.std()) or 1e-9
                tstat = mean / std * float(np.sqrt(n))
                if abs(tstat) < float(min_t):
                    continue
                out.at[day, k] = 1 if mean > 0 else -1
            ref = out.iloc[i].values
        else:
            ref = out.iloc[i - 1].values
        out.iloc[i] = ref
    return out.astype("int8")


def report(panel: dict[str, pd.DataFrame], keys: list[str] | None = None,
           horizon: int = 5, rank: bool = True) -> dict:
    """一次把「谁有用、能撑多久、是不是重复的」全算出来。

    返回 {"horizon", "factors":[{key,name,ic,ir,win,t,n,half_life,spread,monotonic}],
          "redundant":[{a,b,corr}], "corr": DataFrame, "n_codes"}。
    界面和文档都直接用这份,免得各处口径不一。
    """
    keys = list(keys or [k for k in fct.DEFAULT_KEYS])
    rows = []
    for k in keys:
        st = ic_stats(panel, k, horizon=horizon, rank=rank)
        dec = ic_decay(panel, k, (1, 3, 5, 10, 20), rank=rank)
        lay = layered(panel, k, horizon=horizon, rank=rank)
        rows.append({"key": k, "name": fct.factor_label(k),
                     "ic": st["ic"], "ir": st["ir"], "win": st["win"],
                     "t": st["t"], "n": st["n"],
                     "half_life": dec.get("half_life"),
                     "spread": lay.get("spread") or 0.0,
                     "monotonic": bool(lay.get("monotonic"))})
    rows.sort(key=lambda r: -abs(float(r.get("ic") or 0.0)))
    corr = correlation(panel, [r["key"] for r in rows], horizon=horizon)
    return {"horizon": horizon, "rank": rank, "factors": rows,
            "corr": corr, "redundant": redundant_pairs(corr),
            "n_codes": len(panel)}


# ==================================================================== beta 中性化

def beta_neutralize(scores: pd.DataFrame, panel: dict[str, pd.DataFrame],
                    window: int = 20) -> pd.DataFrame:
    """把打分对 beta 做横截面回归,取残差。

    **为什么这是必做项**:实测 45 因子等权 rank 在 2024-08~2026-11 跑出
    +0.570%/期超额(t=2.23),beta 中性化后变成 -0.436%(t=-1.55)。
    同期高 beta 前 20% 比低 beta 后 20% 高 +1.274%/期 —— 那段是单边牛市,
    所谓 alpha 全部来自「挑了高 beta 的票」。

    不做这一步,任何回测在牛市里都会显示"能赚钱",而那不是选股能力。

    实现:每天用个股对市场代理的滚动 beta(取 factors.beta,窗口 window),
    做单变量横截面 OLS,返回残差(保留原 index/columns,缺失保持 NaN)。
    """
    from . import factors as _f

    if scores is None or scores.empty:
        return pd.DataFrame()
    betas = {}
    for code, df in panel.items():
        try:
            b = _f.rolling_reg(df, "beta", window, _f.market_proxy(panel))
        except Exception:  # noqa: BLE001
            continue
        s = pd.Series(np.asarray(b, dtype="float64"), index=pd.DatetimeIndex(df["datetime"]))
        betas[code] = s
    if not betas:
        return scores.copy()
    B = pd.DataFrame(betas).reindex(index=scores.index, columns=scores.columns)
    out = scores.copy()
    for d in scores.index:
        y = scores.loc[d]
        x = B.loc[d] if d in B.index else None
        if x is None:
            continue
        m = y.notna() & x.notna()
        if int(m.sum()) < 20:
            continue
        cols = list(y.index[m])
        xv = np.asarray(x.loc[cols].values, dtype="float64").reshape(-1, 1)
        yv = np.asarray(y.loc[cols].values, dtype="float64")
        # 去掉 beta 里极端的值(杠杆型异常),否则一行就能把斜率拉歪
        lo, hi = np.percentile(xv, [1, 99])
        ok = (xv.ravel() >= lo) & (xv.ravel() <= hi)
        if ok.sum() < 15:
            continue
        try:
            coef = np.linalg.lstsq(xv[ok], yv[ok], rcond=None)[0]
        except np.linalg.LinAlgError:
            continue
        out.loc[d, cols] = yv - (xv @ coef).ravel()
    return out


# ==================================================================== 扣费收益评估

COST_ROUND_TRIP = 0.0016     # 双边:佣金 + 印花税 + 过户费 + 滑点


def _fwd_ret(panel: dict[str, pd.DataFrame], horizon: int) -> pd.DataFrame:
    cols = {}
    for code, df in panel.items():
        if df is None or len(df) < horizon + 2:
            continue
        s = df.set_index("datetime")["close"]
        cols[code] = s.shift(-horizon) / s - 1.0
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()


def net_return(panel: dict[str, pd.DataFrame], scores: pd.DataFrame, horizon: int = 5,
               top_pct: float = 0.2, cost: float = COST_ROUND_TRIP,
               step: int | None = None) -> dict:
    """**按扣费后的真实收益**评估一个打分矩阵,而不是按 IC。

    为什么必须这么评:实测 250 只池子 / 2020-2026 / 5 日持有,LightGBM 的 RankIC
    +0.033(t=5.3)、Top20% 有 51% 落进真实前 20%(随机 20%),**排序能力是真的**;
    但 Top20% 的扣费后收益比全池等权低 0.21%/期(t=-3.5)。十分位是驼峰形,
    Q1 的高均值全部来自不足 5% 的极端赢家 —— 模型正确识别了彩票票,
    而市场恰恰奖励彩票票。IC 是统计量,钱是钱。

    参数:
      scores   日期 × 代码 的打分(大 = 看好),不用 rank,内部自己排
      top_pct  买前多少比例
      step     每隔几天调仓一次;None = 每个交易日都算(会有重叠,偏乐观)
               真实调仓应按 horizon 设,否则成本被低估

    返回:策略与全池等权两条链路各自的单期收益、年化、波动、夏普、超额及 t 值。
    """
    fwd = _fwd_ret(panel, horizon)
    if fwd.empty or scores is None or scores.empty:
        return {}
    sc = scores.reindex(columns=fwd.columns)
    idx = list(sc.index)
    if step and step > 1:
        idx = idx[::int(step)]
    strat, bench = [], []
    for d in idx:
        if d not in fwd.index:
            continue
        s = sc.loc[d].dropna()
        if len(s) < 20:
            continue
        r = fwd.loc[d].reindex(s.index).dropna()
        s = s.reindex(r.index)
        if len(s) < 20:
            continue
        k = max(1, int(len(s) * float(top_pct)))
        top = s.nlargest(k).index
        rt = fwd.loc[d].reindex(top).dropna()
        if rt.empty:
            continue
        strat.append(float(rt.mean()))
        bench.append(float(r.mean()))
    if len(strat) < 10:
        return {"n": len(strat), "note": "样本不足 10 次调仓"}
    a, b = np.array(strat), np.array(bench)
    freq = 252.0 / max(int(step or horizon), 1)
    net = a - float(cost)
    ann = (1 + net.mean()) ** freq - 1
    vol = float(net.std(ddof=1)) * np.sqrt(freq) if len(net) > 1 else 0.0
    diff = a - b
    bnet = b - float(cost)
    bann = (1 + bnet.mean()) ** freq - 1
    bvol = float(bnet.std(ddof=1)) * np.sqrt(freq) if len(bnet) > 1 else 0.0
    return {
        "n": len(a), "freq": round(freq, 1), "horizon": horizon, "top_pct": top_pct,
        "strategy": {"mean": round(float(a.mean()), 6),
                     "net_mean": round(float(net.mean()), 6),
                     "annual": round(float(ann), 4),
                     "vol": round(float(vol), 4),
                     "sharpe": round(float(ann / vol), 3) if vol > 0 else None,
                     "win": round(float((a > 0).mean()), 3)},
        "benchmark": {"mean": round(float(b.mean()), 6),
                      "net_mean": round(float(bnet.mean()), 6),
                      "annual": round(float(bann), 4),
                      "vol": round(float(bvol), 4),
                      "sharpe": round(float(bann / bvol), 3) if bvol > 0 else None,
                      "win": round(float((b > 0).mean()), 3)},
        "excess_mean": round(float(diff.mean()), 6),
        "excess_t": round(float(diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff))), 2)
        if len(diff) > 1 and diff.std(ddof=1) > 0 else None,
        "verdict": ("能赚钱" if diff.mean() > 0 and
                    diff.mean() / max(diff.std(ddof=1), 1e-12) * np.sqrt(len(diff)) >= 2
                    else "跑不赢等权"),
    }


def audit_scores(panel: dict[str, pd.DataFrame], scores: pd.DataFrame,
                 horizon: int = 5, top_pct: float = 0.2,
                 cost: float = COST_ROUND_TRIP) -> dict:
    """一次给出「原始 vs beta 中性化」两份扣费收益结果,外加 IC。

    这是评测打分器的标准入口。只有**两份都跑赢**才算真 alpha ——
    实测 45 因子等权 rank:原始 +0.570%/期(t=2.23),beta 中性化后 -0.436%(t=-1.55),
    前者能赚钱、后者有害,说明那点超额就是 beta。

    返回 {"raw": net_return(...), "neutral": net_return(...),
          "rank_ic": 均值, "verdict": 结论句}。
    """
    if scores is None or scores.empty:
        return {"verdict": "无打分"}
    fwd = _fwd_ret(panel, horizon)
    ic = []
    for d in scores.index:
        if d not in fwd.index:
            continue
        s = scores.loc[d].dropna()
        y = fwd.loc[d].reindex(s.index).dropna()
        s = s.reindex(y.index)
        if len(s) < 30:
            continue
        a = np.asarray(s, dtype="float64")
        b = np.asarray(y, dtype="float64")
        if a.std() > 1e-12 and b.std() > 1e-12:
            ic.append(float(np.corrcoef(a, b)[0, 1]))
    rank_ic = round(float(np.mean(ic)), 4) if ic else 0.0

    raw = net_return(panel, scores, horizon=horizon, top_pct=top_pct,
                     cost=cost, step=horizon)
    neut_scores = beta_neutralize(scores, panel)
    neutral = net_return(panel, neut_scores, horizon=horizon, top_pct=top_pct,
                         cost=cost, step=horizon) if not neut_scores.empty else {}

    def _ok(r):
        return bool(r) and r.get("excess_t") is not None and r["excess_t"] >= 2

    if _ok(raw) and _ok(neutral):
        verdict = "真 alpha(中性化后仍显著)"
    elif _ok(raw) and not _ok(neutral):
        verdict = "beta 伪装成 alpha(中性化后消失)"
    elif _ok(neutral) and not _ok(raw):
        verdict = "只在剔除 beta 后有效(异常,建议复查)"
    else:
        verdict = "跑不赢等权"
    return {"raw": raw, "neutral": neutral, "rank_ic": rank_ic,
            "verdict": verdict, "horizon": horizon, "cost": cost}
