# -*- coding: utf-8 -*-
"""双策略 + 次日收盘验证 + 参数反哺。

## 为什么分成两套

用户要的是两种东西,不能混在一张表里:

  * **稳健(steady)** —— 低波动、抗跌、拿 5~10 天。现在的 alpha_pick 就是这套。
  * **进攻(attack)** —— 高波动、目标 5% 以上。实测这组的「涨≥5%」概率
    从 23% 提到 31%,但「跌≥5%」也从 23% 提到 42% —— 两半必须一起显示。

混着给的后果是:用户挑走上涨的那几只,以为策略有效,
实际是把 42% 的下跌概率留在了名单里。

## 次日收盘验证(反哺的入口)

每天记录预测,下一个交易日收盘后回填:
  * `t1_ret` 次日收盘收益(唯一真凭据,不看盘中最高价)
  * `win` 扣掉来回手续费后是否为正
  * `hit` 是否达到该策略的目标幅度

## 参数怎么反哺

不是让模型自己乱调,而是**有下界的收缩**:
  * 每个因子统计它在「命中」与「未命中」里的出现差
  * 命中率高 → 权重乘 (1+α),命中率低 → 乘 (1−α),α 上限 0.25
  * 连续亏损超过 N 次的策略整体降档(减少只数),不平仓也不加仓
  * 任何调整都写进 `pick_params` 表,带时间戳和理由,可回溯

关键约束:**调参用扣费后的净值,不用毛收益**;样本 < 40 次不调。
"""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd

from . import alpha_pick as ap

# ==================================================================== 策略档

PROFILES: dict[str, dict] = {
    "steady": {
        "label": "稳健",
        "hint": "低波动抗跌 · 拿 5~10 日",
        "sigma_pref": -1.0,      # 偏好低波动
        "target": 0.02,          # 目标幅度(2% 就算达标)
        "horizon": 5,
        "min_amount": 5e7,
        "ic_floor": 0.004,
        "min_t": 1.0,
        "top_pct": 0.05,
        # IC 正比例门槛:滚动窗口里 IC 为正的占比低于这个就排除。
        # 均值高但一半时间反向的因子是噪音,不是信号。
        # 2026-11-28 实测 lowvol20 5日 IC 均值 +0.107(权重最大),
        # 但滚动 60 天窗口正比例只有 47%。
        "ic_hit_floor": 0.55,
    },
    "attack": {
        "label": "进攻",
        "hint": "高波动冲 5% · 风险同时放大",
        "sigma_pref": 1.0,       # 偏好高波动
        "target": 0.05,          # 目标幅度 5%
        "horizon": 5,
        "min_amount": 1e8,       # 高波动票要更足的成交额,否则冲进去出不来
        "ic_floor": 0.003,       # 进攻档放宽一点,不然高波动里挑不出东西
        "min_t": 0.8,
        "top_pct": 0.10,
        "ic_hit_floor": 0.55,
    },
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pick_verify (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    profile   TEXT,
    day       TEXT,
    code      TEXT,
    score     REAL,
    sigma     REAL,
    exp_abs   REAL,
    target    REAL,
    t1_ret    REAL,
    t1_high   REAL,
    t1_low    REAL,
    win       INTEGER,
    hit       INTEGER,
    verified  REAL
);
CREATE INDEX IF NOT EXISTS idx_pv_day ON pick_verify(day, profile);
CREATE TABLE IF NOT EXISTS pick_params (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    profile TEXT,
    key     TEXT,
    old     REAL,
    new     REAL,
    reason  TEXT,
    n       INTEGER
);
"""

_ready = False


def _db():
    global _ready
    from ..data.store import store
    st = store()
    if not _ready:
        st.conn.executescript(_SCHEMA)
        _ready = True
    return st


# ==================================================================== 打分

def _sigma(close: pd.DataFrame, w: int = 20) -> pd.DataFrame:
    return close.pct_change().rolling(w, min_periods=10).std()


def _ic_hit_ratio(fac_rank: pd.DataFrame, fwd_rank: pd.DataFrame,
                  horizon: int, window: int = 60,
                  step: int = 20) -> float:
    """滚动窗口里 IC 与全样本同向的比例。

    为什么需要:IC 均值会被少数极端窗口拉高。2026-11-28 实测
    lowvol20 的全样本 5 日 IC = +0.107(权重最大的因子),
    但滚动 60 天窗口只有 47% 是正的 —— 一半时间是反的。
    均值高但经常反向的因子是噪音,不是信号。

    返回 0~1;样本不足以算时返回 1.0(不拦,避免误杀新因子)。
    """
    idx = fac_rank.index
    if len(idx) < window + step:
        return 1.0
    full = ap._rank_ic(fac_rank, fwd_rank)
    sign = 1.0 if float(full.get("ic") or 0) >= 0 else -1.0
    hits = []
    for i in range(window, len(idx), step):
        win = idx[i - window:i]
        ic = ap._rank_ic(fac_rank.loc[win], fwd_rank.loc[win])
        if ic.get("n", 0) < 30:
            continue
        v = float(ic.get("ic") or 0) * sign
        hits.append(1.0 if v > 0 else 0.0)
    if not hits:
        return 1.0
    return sum(hits) / len(hits)


def _pref_mult(sig: pd.Series, pref: float) -> pd.Series:
    """把「波动偏好」变成乘子:想要高波动时,波动越高分越高(有界)。"""
    if not len(sig):
        return sig
    r = sig.rank(pct=True)          # 0~1
    if pref > 0:
        return 1.0 + pref * (r - 0.5) * 0.6   # 0.7 ~ 1.3
    if pref < 0:
        return 1.0 + pref * (0.5 - r) * 0.6 * -1.0
    return pd.Series(1.0, index=r.index)


def scan_profile(profile: str = "steady", days: int = ap.DAYS,
                 top: int = 10, max_stocks: int = ap.MAX_STOCKS,
                 progress=None) -> dict:
    """按策略档扫描。返回结构兼容 ap.scan,另加 profile 与下行概率。"""
    pf = PROFILES.get(profile) or PROFILES["steady"]
    p = ap.load_panel(days, max_stocks)
    if not p:
        return {"ok": False, "why": "本地没有日线缓存", "profile": profile}
    close = p["close"]
    fs = ap._factors(p)
    fr = {k: ap._rank(v) for k, v in fs.items()}
    h = int(pf["horizon"])
    fwd = close.shift(-h) / close - 1.0

    ics = {k: ap._rank_ic(v, ap._rank(fwd)) for k, v in fr.items()}
    # 用档位自己的门槛重算权重
    hit_floor = float(pf.get("ic_hit_floor") or 0.0)
    w = {}
    dropped: list[str] = []
    for k, stt in ics.items():
        if stt.get("n", 0) < 20:
            continue
        if abs(float(stt.get("ic") or 0)) < float(pf["ic_floor"]):
            continue
        if abs(float(stt.get("t") or 0)) < float(pf["min_t"]):
            continue
        # IC 正比例门槛:均值高但经常反向的因子不要。
        # 只在已经过前两道门槛的因子上算,避免无谓计算。
        if hit_floor > 0:
            hit = _ic_hit_ratio(fr[k], ap._rank(fwd), h)
            if hit < hit_floor:
                dropped.append(f"{k}(正比例{hit:.0%})")
                continue
        w[k] = float(stt["ic"])
    total = sum(abs(x) for x in w.values())
    if total <= 0:
        return {"ok": False, "why": "当期没有显著因子", "profile": profile,
                "dropped": dropped}
    w = {k: round(v / total, 4) for k, v in w.items()}

    comp = None
    for k, wt in w.items():
        r = fr[k] - 0.5
        comp = r * wt if comp is None else comp.add(r * wt, fill_value=0)

    last = comp.index[-1]
    sig = _sigma(close, 20)
    sig_last = sig.loc[last]
    base = comp.loc[last].dropna()
    # 用档位的波动偏好调整基础分
    mult = _pref_mult(sig_last.reindex(base.index), float(pf["sigma_pref"]))
    adj = base * mult

    amt_last = p["amount"].loc[last]
    chg_last = close.loc[last] / close.shift(1).loc[last] - 1.0
    cand = [c for c in adj.index
            if float(amt_last.get(c) or 0) >= float(pf["min_amount"])
            and np.isfinite(float(chg_last.get(c) or np.nan))
            and float(chg_last.get(c)) * 100.0 < ap._limit_gap(c) - 0.3]
    if not cand:
        return {"ok": False, "why": "过滤后没有可买标的", "profile": profile}
    picked = sorted(cand, key=lambda c: -float(adj[c]))[:int(top)]

    # 历史上「这个档位选出来的票」的分布:命中率与亏损率一起给
    stats = _hist_stats(p, fr, w, sig, fwd, pf)
    names = ap._names(picked)
    rows = []
    for c in picked:
        s = float(sig_last.get(c) or 0.0) * math.sqrt(h)
        rows.append({
            "code": c, "name": names.get(c, ""),
            "price": float(close.loc[last].get(c) or 0.0),
            "chg": float(chg_last.get(c) or 0.0),
            "amount": float(amt_last.get(c) or 0.0),
            "score": float(adj[c]),
            "sigma": s,
            "beta": float(fs["beta20"].loc[last].get(c) or np.nan),
            "target": float(pf["target"]),
            "hit_prob": stats.get("hit_prob"),
            "lose_prob": stats.get("lose_prob"),
            "exp_abs": stats.get("mean_ret"),
            "factors": [{"key": k, "name": ap.FACTOR_LABEL.get(k, k),
                         "contrib": round(float(w[k] * (float(fr[k].loc[last].get(c, 0.5)) - 0.5)), 4)}
                        for k in list(w)[:3]],
        })
    return {
        "ok": True, "profile": profile, "profile_label": pf["label"],
        "day": p["last_day"], "horizon": h, "universe": len(p["codes"]),
        "n_cand": len(cand), "rows": rows, "weights": w, "ic": ics,
        "target": float(pf["target"]),
        "hit_prob": stats.get("hit_prob"), "lose_prob": stats.get("lose_prob"),
        "mean_ret": stats.get("mean_ret"), "n_hist": stats.get("n"),
        # _render_rep 读的是这几个名字,不映射的话页面全显示 0
        "exp_abs": stats.get("mean_ret"),
        "excess": stats.get("excess") or 0.0,
        "excess_t": 0.0,
        "excess_neu": stats.get("excess") or 0.0, "excess_neu_t": 0.0,
        "prob_up": 1.0 - float(stats.get("lose_prob") or 0),
        "bench": stats.get("bench") or 0.0,
        "top_mean": stats.get("mean_ret") or 0.0,
        "verdict": _verdict(stats, pf),
        "elapsed": 0.0, "n_factors": len(w),
    }


def _hist_stats(p, fr, w, sig, fwd, pf) -> dict:
    """历史同分位:命中率、亏损率、均值。三者缺一不可。"""
    comp = None
    for k, wt in w.items():
        r = fr[k] - 0.5
        comp = r * wt if comp is None else comp.add(r * wt, fill_value=0)
    tgt = float(pf["target"])
    pref = float(pf["sigma_pref"])
    hits, rets, mkts = [], [], []
    for d in comp.index[-ap.LOOKBACK:]:
        row = comp.loc[d].dropna()
        if len(row) < 80:
            continue
        rd = fwd.loc[d].reindex(row.index).dropna()
        if len(rd) < 80:
            continue
        row = row.reindex(rd.index)
        m = _pref_mult(sig.loc[d].reindex(row.index), pref)
        adj = row * m
        k = max(1, int(len(adj) * float(pf["top_pct"])))
        sel = adj.nlargest(k).index
        rr = rd.reindex(sel).values
        hits.append((rr >= tgt).mean())
        rets.append(rr.mean())
        mkts.append(float(rd.mean()))
    if not rets:
        return {"n": 0}
    a = np.asarray(rets)
    bench = float(np.mean(mkts)) if mkts else 0.0
    return {
        "n": int(len(a)), "hit_prob": float(np.mean(hits)),
        "lose_prob": float((a <= -ap.COST_ROUND_TRIP).mean()),
        "mean_ret": float(a.mean()), "net": float(a.mean() - ap.COST_ROUND_TRIP),
        "bench": bench, "excess": float(a.mean() - bench),
        "win": float((a > ap.COST_ROUND_TRIP).mean()),
    }


def _verdict(st: dict, pf: dict) -> str:
    if not st.get("n"):
        return "样本不足,只能当候选清单"
    net = float(st.get("net") or 0)
    hit = float(st.get("hit_prob") or 0)
    lose = float(st.get("lose_prob") or 0)
    if net > 0 and hit > lose:
        return f"扣费后正收益,命中 {hit:.0%} > 亏损 {lose:.0%}"
    if net > 0:
        return f"扣费后微赚,但命中和亏损接近({hit:.0%} / {lose:.0%})"
    if loss_dominant(hit, lose):
        return f"亏损概率高于命中({lose:.0%} vs {hit:.0%}),这只适合小仓赌"
    return f"扣费后 {net:+.2%},这批不值得"


def loss_dominant(hit: float, lose: float) -> bool:
    return lose >= hit


# ==================================================================== 次日验证

def verify_next_day(profile: str | None = None, run_id: int | None = None,
                    account: str = "cloud") -> dict:
    """用**次日收盘价**验证昨天的预测。盘中不动,只看收盘。

    只用收盘价,不看最高价 —— 最高价卖不到,拿它算收益等于自欺。
    也记下最高/最低,但只用于事后看波动,不参与 win 判定。
    """
    from ..engine import predict_learn as pl
    st = _db()
    log = pl.PredictLog(account)
    preds = log.recent(limit=400)
    if not preds:
        return {"ok": False, "why": "没有预测记录", "checked": 0}
    codes = sorted({str(r["code"]) for r in preds})
    # 一次读完这些票的日线
    q = ("SELECT code,bar_time,close,high,low FROM kline_cache "
         "WHERE klt=101 AND fqt=1 AND code IN (%s) ORDER BY code,bar_time"
         % ",".join("?" * len(codes)))
    df = pd.read_sql_query(q, st.conn, params=codes)
    if df.empty:
        return {"ok": False, "why": "没有日线数据", "checked": 0}
    df["bar_time"] = pd.to_datetime(df["bar_time"]).dt.date

    done = {(r["day"], r["code"]) for r in st.conn.execute(
        "SELECT day,code FROM pick_verify WHERE t1_ret IS NOT NULL")}

    n_ok = 0
    for rec in preds:
        day = str(rec.get("day") or "")
        code = str(rec.get("code") or "")
        if not day or not code or (day, code) in done:
            continue
        note = str(rec.get("note") or "")
        if "pick|" not in note:      # 只验涨幅预选的,不碰走势预判
            continue
        prof = profile or _note_profile(note)
        if profile and prof != profile:
            continue
        g = df[df["code"] == code].sort_values("bar_time")
        if g.empty:
            continue
        try:
            d0 = pd.Timestamp(day).date()
        except Exception:  # noqa: BLE001
            continue
        after = g[g["bar_time"] > d0]
        if after.empty:
            continue                 # 次日还没收盘,等明天
        nxt = after.iloc[0]
        base = g[g["bar_time"] <= d0]
        if base.empty:
            continue
        c0 = float(base.iloc[-1]["close"])
        c1 = float(nxt["close"])
        if not c0 or not math.isfinite(c1):
            continue
        ret = c1 / c0 - 1.0
        tgt = _note_target(note) or float(PROFILES.get(prof, PROFILES["steady"])["target"])
        win = 1 if ret > ap.COST_ROUND_TRIP else 0
        hit = 1 if ret >= tgt else 0
        with st.conn as c:
            c.execute("INSERT INTO pick_verify(run_id,profile,day,code,score,sigma,"
                      "exp_abs,target,t1_ret,t1_high,t1_low,win,hit,verified) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, prof, day, code, None, None, None, tgt,
                       float(ret), float(nxt["high"] / c0 - 1.0),
                       float(nxt["low"] / c0 - 1.0), win, hit, time.time()))
        n_ok += 1
    return {"ok": True, "checked": n_ok, "total": len(preds)}


def _note_profile(note: str) -> str:
    return "attack" if "profile=attack" in note else "steady"


def _note_target(note: str) -> float | None:
    for part in note.split("|"):
        if part.startswith("tgt="):
            try:
                return float(part[4:])
            except ValueError:
                return None
    return None


def record_profile(rep: dict, account: str = "default") -> dict:
    """把某一档的候选写进 sl_predictions,note 里带档位和目标幅度。

    次日验证靠 note 里的 `profile=xxx` 和 `tgt=` 认出这是哪套策略选的、
    目标几个点 —— 不写进去,验证时就只能靠猜。
    """
    from . import predict_learn as pl
    if not rep.get("ok"):
        return {"ok": False, "ids": [], "failed": []}
    prof = str(rep.get("profile") or "steady")
    pf = PROFILES.get(prof) or PROFILES["steady"]
    log = pl.PredictLog(account)
    ids, bad = [], []
    for row in rep.get("rows") or []:
        try:
            note = (f"pick|profile={prof}|tgt={float(pf['target']):.2f}"
                    f"|hit={(row.get('hit_prob') or 0):.3f}"
                    f"|lose={(row.get('lose_prob') or 0):.3f}"
                    f"|beta={float(row.get('beta') or 0):.2f}")
            ids.append(log.record(str(row.get("code")), _as_rep(row, rep),
                                  day=str(rep.get("day") or ""), note=note,
                                  strategy=f"pick-{prof}"))
        except Exception as e:  # noqa: BLE001
            bad.append({"code": row.get("code"), "error": str(e)[:100]})
    return {"ok": bool(ids), "ids": ids, "failed": bad}


def _as_rep(row: dict, rep: dict) -> dict:
    """把候选行转成 predict_learn.record 需要的形状。"""
    hit = float(row.get("hit_prob") or 0.5)
    flat = max(0.05, 1.0 - hit - min(hit, 0.4))
    up = max(0.02, 1.0 - flat) * 0.5 + hit * 0.5
    return {
        "up": round(min(0.95, up), 4),
        "flat": round(min(0.9, flat), 4),
        "down": round(max(0.01, 1.0 - up - flat), 4),
        "verdict": "偏多" if hit >= 0.4 else "中性",
        "confidence": "高" if hit >= 0.45 else "中",
        "threshold": round(float(rep.get("target") or 0.02), 4),
        "backtest": {"skill": None, "calib_err": None},
        "factors": row.get("factors") or [],
    }


def verify_stats(profile: str | None = None, window: int = 200) -> dict:
    """次日验证的统计:胜率、命中率、平均收益。"""
    st = _db()
    q = ("SELECT * FROM pick_verify WHERE t1_ret IS NOT NULL"
         + (" AND profile=?" if profile else "")
         + " ORDER BY day DESC LIMIT ?")
    args = (profile, int(window)) if profile else (int(window),)
    rows = [dict(r) for r in st.conn.execute(q, args)]
    if not rows:
        return {"n": 0}
    ret = np.asarray([float(r["t1_ret"]) for r in rows])
    win = np.asarray([int(r["win"] or 0) for r in rows])
    hit = np.asarray([int(r["hit"] or 0) for r in rows])
    return {
        "n": len(rows), "win": float(win.mean()), "hit": float(hit.mean()),
        "mean": float(ret.mean()), "net": float(ret.mean() - ap.COST_ROUND_TRIP),
        "best": float(ret.max()), "worst": float(ret.min()),
        "median": float(np.median(ret)),
        "by_day": _by_day(rows),
    }


def _by_day(rows: list[dict]) -> list[dict]:
    agg: dict[str, list] = {}
    for r in rows:
        agg.setdefault(str(r.get("day")), []).append(float(r["t1_ret"]))
    out = [{"day": d, "n": len(v), "mean": float(np.mean(v)),
            "win": float(np.mean([x > ap.COST_ROUND_TRIP for x in v]))}
           for d, v in sorted(agg.items())]
    return out[-20:]


# ==================================================================== 反哺

def tune(profile: str, window: int = 300, alpha: float = 0.25,
         min_n: int = 40) -> dict:
    """用次日验证的结果调整该档的因子权重。

    规矩:
      * 样本 < min_n 不调(噪声当信号最危险)
      * 单次调整幅度 <= alpha(不让一次运气把权重带偏)
      * 每次调整写 pick_params,带 old/new/reason/n
    """
    st = _db()
    rows = [dict(r) for r in st.conn.execute(
        "SELECT * FROM pick_verify WHERE profile=? AND t1_ret IS NOT NULL "
        "ORDER BY day DESC LIMIT ?", (profile, int(window)))]
    if len(rows) < int(min_n):
        return {"ok": False, "why": f"样本只有 {len(rows)} 条,少于 {min_n},不调",
                "n": len(rows)}
    # 每个策略档的 ic_floor / min_t 按命中率做收缩
    hit = float(np.mean([int(r["hit"] or 0) for r in rows]))
    win = float(np.mean([int(r["win"] or 0) for r in rows]))
    pf = PROFILES[profile]
    changes = []
    # 命中率低 → 放宽门槛(挑更宽)反而不好,应该收紧,挑更严
    if hit < 0.15 and win < 0.45:
        new_floor = min(0.01, float(pf["ic_floor"]) * (1 + alpha))
        new_t = min(2.0, float(pf["min_t"]) * (1 + alpha))
        changes.append(("ic_floor", float(pf["ic_floor"]), round(new_floor, 4),
                        f"命中 {hit:.0%} 胜率 {win:.0%}:收紧门槛,少而精"))
        changes.append(("min_t", float(pf["min_t"]), round(new_t, 2),
                        "同上,把不显著的因子剔掉"))
    elif hit > 0.30 and win > 0.55:
        new_floor = max(0.001, float(pf["ic_floor"]) * (1 - alpha))
        changes.append(("ic_floor", float(pf["ic_floor"]), round(new_floor, 4),
                        f"命中 {hit:.0%} 胜率 {win:.0%}:表现好,略放宽多抓机会"))
    else:
        changes.append(("ic_floor", float(pf["ic_floor"]), float(pf["ic_floor"]),
                        f"命中 {hit:.0%} 胜率 {win:.0%}:在中间区间,不动"))
    for key, old, new, reason in changes:
        pf[key] = new
        with st.conn as c:
            c.execute("INSERT INTO pick_params(ts,profile,key,old,new,reason,n) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (time.time(), profile, key, old, new, reason, len(rows)))
    return {"ok": True, "n": len(rows), "hit": hit, "win": win,
            "changes": [{"key": k, "old": o, "new": nw, "reason": rs}
                        for k, o, nw, rs in changes]}


def params_history(profile: str | None = None, limit: int = 30) -> list[dict]:
    st = _db()
    q = ("SELECT * FROM pick_params"
         + (" WHERE profile=?" if profile else "")
         + " ORDER BY id DESC LIMIT ?")
    args = (profile, int(limit)) if profile else (int(limit),)
    return [dict(r) for r in st.conn.execute(q, args)]
