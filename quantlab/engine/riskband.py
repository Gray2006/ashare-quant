# -*- coding: utf-8 -*-
"""风险档位:在横截面上判断「这只票的波动风险相对有多高」。

先说清楚为什么这么做 —— 这是实测修正过的结论:

- 单票时间序列上预测「未来波动会变大还是变小」几乎没用:
  个股自身走查超额 -12.8pp ~ +1.7pp,波动会均值回归,高点之后往往回落;
- 但横截面上「同一天谁的波动高」非常有信息:
  Spearman IC 约 +0.6,当前波动前 20% 的票,未来 5 日波动均值是后 20% 的 3 倍。

所以这个模块回答的是相对位置:今天这些票里,谁的波动风险处在高位,
历史同样位置的样本接下来波动多大、该给多少仓位上限。

对外:panel_risk(codes=None, horizon=5, days=800) -> dict
      risk_band(code, ...) -> dict        单只(在池子里查它的位置)
      text(rep) -> str
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..utils import normalize_code
from . import forecast as fc

HORIZON = 5
VOL_WINDOW = 20
MIN_PAIRS = 200
BANDS = ("低", "中", "高", "极高")
BAND_CUTS = (0.50, 0.80, 0.95)                 # 横截面分位切点
POSITION_CAP = (0.80, 0.60, 0.40, 0.20)
BAND_COLOR = ("#2ebd85", "#4c8dff", "#f0b90b", "#f6465d")

NOTES = [
    "这是横截面相对位置,不是「这只票未来波动会变大」",
    "分档看的是同一天里所有票的波动排名,所以可以直接横向比",
    "预测值和区间来自历史同档样本的实测中位数与 P25~P75",
    "走查命中率一定和「永远猜高」的基准并排看",
    "单票时间序列上猜波动升降不可靠(实测超额为负),别那样用",
]


def _vols(close: np.ndarray, h: int) -> tuple[np.ndarray, np.ndarray]:
    r = np.zeros_like(close)
    r[1:] = close[1:] / close[:-1] - 1.0
    s = pd.Series(r)
    cur = s.rolling(VOL_WINDOW, min_periods=10).std().to_numpy()
    fut = s.rolling(h, min_periods=max(2, h - 2)).std().shift(-h).to_numpy()
    return cur, fut


def _build_matrix(frames: list[dict], h: int) -> pd.DataFrame:
    """把池子拼成 (date, code, cur, fut) 长表。"""
    rows = []
    for f in frames:
        cur, fut = _vols(np.asarray(f["close"], dtype="float64"), h)
        rows.append(pd.DataFrame({"code": f["code"], "name": f["name"],
                                  "date": f["dates"], "cur": cur, "fut": fut}))
    d = pd.concat(rows, ignore_index=True)
    return d[np.isfinite(d["cur"]) & np.isfinite(d["fut"]) & (d["cur"] > 0)].reset_index(drop=True)


def _band_of(rank: float) -> int:
    for i, cut in enumerate(BAND_CUTS):
        if rank <= cut:
            return i
    return len(BANDS) - 1


def panel_risk(codes: list[str] | None = None, horizon: int = HORIZON,
               days: int = 800, progress=None) -> dict:
    """整个池子的横截面风险档位。"""
    h = int(horizon) if int(horizon) > 0 else HORIZON
    frames = fc.build_panel("stock", fc.PANEL_SIZE, days, progress)
    if len(frames) < 10:
        raise RuntimeError("池子标的太少,做不了横截面比较")
    d = _build_matrix(frames, h)
    if codes:
        want = {normalize_code(c) for c in codes}
    else:
        want = None

    # 每个交易日内部做横截面分位
    d["cur_r"] = d.groupby("date")["cur"].rank(pct=True)
    d["fut_r"] = d.groupby("date")["fut"].rank(pct=True)

    dates = sorted(d["date"].unique())
    last = dates[-1]
    now = d[d["date"] == last].copy()
    hist = d[d["date"] < last]

    # 走查:滚动用「过去横截面样本」预测当天各票的未来波动排名
    picks = dates[-60:]
    ics: list[float] = []
    hit = base = tot = 0
    for dt in picks:
        past = hist[hist["date"] < dt]
        if len(past) < MIN_PAIRS:
            continue
        today = d[d["date"] == dt]
        if len(today) < 5:
            continue
        # 过去:cur 排名 → fut 排名 的映射(分 5 段取中位数)
        pb = pd.cut(past["cur_r"], [0, 0.2, 0.4, 0.6, 0.8, 1.0],
                    labels=False, include_lowest=True)
        med = past.groupby(pb)["fut_r"].median()
        tb = pd.cut(today["cur_r"], [0, 0.2, 0.4, 0.6, 0.8, 1.0],
                    labels=False, include_lowest=True)
        pred = tb.map(med)
        ok = pred.notna().to_numpy() & today["fut_r"].notna().to_numpy()
        if ok.sum() >= 5:
            # 两边都是分位数(秩),直接算皮尔逊就等价于 Spearman(环境里没有 scipy)
            a = np.asarray(pred.to_numpy()[ok], dtype="float64")
            bq = np.asarray(today["fut_r"].to_numpy()[ok], dtype="float64")
            if a.std() > 1e-12 and bq.std() > 1e-12:
                ics.append(float(np.corrcoef(a, bq)[0, 1]))
            hi_pred = (pred.to_numpy()[ok] > 0.5).astype(int)
            hi_act = (today["fut_r"].to_numpy()[ok] > 0.5).astype(int)
            hit += int((hi_pred == hi_act).sum())
            base += int(hi_act.sum())
            tot += int(ok.sum())

    bt: dict = {"n": tot}
    if tot >= 100:
        bt["hit"] = hit / tot
        bt["base"] = base / tot
        bt["excess"] = bt["hit"] - bt["base"]
        bt["ic"] = float(np.nanmean(ics)) if ics else None

    # 当前时点:按横截面排名分档,查历史同档的未来波动分布
    now["band_idx"] = now["cur_r"].apply(_band_of)
    q_cur = np.quantile(hist["cur"], [0, 0.5, 0.8, 0.95, 1.0])
    levels = []
    for i, lab in enumerate(BANDS):
        lo, hi = float(q_cur[i]), float(q_cur[i + 1])
        sub = hist[(hist["cur"] >= lo) & (hist["cur"] <= hi)]["fut"]
        levels.append({"band": lab, "idx": i, "lo": lo, "hi": hi, "n": int(len(sub)),
                       "fut_med": float(sub.median()) if len(sub) else None,
                       "fut_p25": float(sub.quantile(0.25)) if len(sub) else None,
                       "fut_p75": float(sub.quantile(0.75)) if len(sub) else None,
                       "pos_cap": POSITION_CAP[i]})

    items = []
    for _, r0 in now.iterrows():
        if want is not None and r0["code"] not in want:
            continue
        b = int(r0["band_idx"])
        lv = levels[b]
        band = hist[hist["date"] < last]
        sub = band[(band["cur"] >= lv["lo"]) & (band["cur"] <= lv["hi"])]["fut"]
        items.append({
            "code": r0["code"], "name": r0["name"],
            "cur_vol": float(r0["cur"]), "cur_vol_pct": float(r0["cur"]) * 100.0,
            "rank": float(r0["cur_r"]),
            "band": BANDS[b], "band_idx": b, "band_color": BAND_COLOR[b],
            "pred_vol": float(sub.median()) if len(sub) else None,
            "pred_vol_pct": float(sub.median()) * 100.0 if len(sub) else None,
            "vol_lo_pct": float(sub.quantile(0.25)) * 100.0 if len(sub) else None,
            "vol_hi_pct": float(sub.quantile(0.75)) * 100.0 if len(sub) else None,
            "pos_cap": POSITION_CAP[b],
            "advice": _advice(b, float(r0["cur_r"])),
        })
    items.sort(key=lambda x: -x["rank"])

    conf = "低"
    if bt.get("n", 0) >= 200 and (bt.get("excess") or 0) >= 0.03:
        conf = "中"
    if bt.get("n", 0) >= 300 and (bt.get("excess") or 0) >= 0.06:
        conf = "高"

    return {"asof": last, "horizon": h, "pool": len(frames), "n_rows": int(len(d)),
            "levels": levels, "items": items, "backtest": bt,
            "confidence": conf, "notes": NOTES}


def _advice(b: int, rank: float) -> str:
    if b == 0:
        return f"波动排名在池子后 {rank * 100:.0f}% 分位,行情安静,可正常持仓"
    if b == 1:
        return f"波动排名 {rank * 100:.0f}% 分位,正常水平,按常规仓位做"
    if b == 2:
        return f"波动排名 {rank * 100:.0f}% 分位,偏活跃,仓位压到四成、止损放宽一档"
    return f"波动排名 {rank * 100:.0f}% 分位,极高,容易大阳大阴,仓位两成以内或观望"


def risk_band(code: str, horizon: int = HORIZON, days: int = 800,
              kind: str = "stock", name: str = "") -> dict:
    """单只标的在池子里的风险档位。"""
    code = normalize_code(code)
    rep = panel_risk([code], horizon=horizon, days=days)
    hit = [it for it in rep["items"] if it["code"] == code]
    if not hit:
        raise RuntimeError("这只票不在池子里(需要有效的日线数据)")
    it = hit[0]
    out = dict(it)
    out.update({"asof": rep["asof"], "horizon": rep["horizon"],
                "levels": rep["levels"], "backtest": rep["backtest"],
                "confidence": rep["confidence"], "pool": rep["pool"],
                "notes": rep["notes"], "kind": "index" if kind == "index" else "stock",
                "name": name or it["name"]})
    return out


def text(rep: dict) -> str:
    bt = rep.get("backtest") or {}
    lines = [
        f"{rep.get('name', '')}({rep['code']}) 风险档位 · {rep['asof']} · 未来 {rep['horizon']} 个交易日",
        f"档位 {rep['band']}(池内 {rep['rank'] * 100:.0f}% 分位)"
        f" · 当前日波动 {rep['cur_vol_pct']:.2f}%"
        f" · 历史同档未来波动 {rep['pred_vol_pct']:.2f}%"
        f"(P25~P75 {rep['vol_lo_pct']:.2f}%~{rep['vol_hi_pct']:.2f}%)",
        f"建议仓位上限 {rep['pos_cap'] * 100:.0f}% · 置信 {rep['confidence']}"
        f" · 池子 {rep.get('pool')} 只",
    ]
    if bt.get("n"):
        lines.append(f"走查 {bt['n']} 次:命中 {bt['hit'] * 100:.1f}%"
                     f"(永远猜高 {bt['base'] * 100:.1f}%),超额 {bt['excess'] * 100:+.1f} 个点"
                     + (f",平均排名相关 {bt['ic']:+.2f}" if bt.get("ic") is not None else ""))
    lines.append(rep["advice"])
    return "\n".join(lines)
