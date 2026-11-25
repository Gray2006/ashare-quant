# -*- coding: utf-8 -*-
"""日内微观结构因子:从 5 分钟线挖日线看不到的东西。

数据:268 只 × 21 个交易日(2026-10-26 ~ 11-24)的 5 分钟线。

七个特征:
  1 隔夜跳空    开盘 / 前收 - 1
  2 尾盘拉升    14:30-15:00 的收益
  3 开盘量占比  前 6 根(9:30-10:00) 成交量 / 全天
  4 尾盘量占比  最后 6 根(14:30-15:00) / 全天
  5 日内动量    下午收益 - 上午收益
  6 日内波动率  5 分钟收益的标准差
  7 收盘位置    收盘在当日最高最低之间的位置

然后用它们预测**次日**收益,过双口径 + FDR 校正。

样本很小(21 天横截面),t 检验力弱,先看方向。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from quantlab.data.store import store  # noqa: E402
from quantlab.engine import multiple_testing as mt  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_intraday() -> dict[str, pd.DataFrame]:
    """{code: DataFrame(index=日期, 列=七个特征)}"""
    c = store().conn
    codes = [r[0] for r in c.execute(
        "select distinct code from kline_cache where klt=5 and fqt=1").fetchall()]
    out: dict[str, pd.DataFrame] = {}
    for code in codes:
        rows = c.execute(
            "select bar_time, open, high, low, close, volume from kline_cache "
            "where klt=5 and fqt=1 and code=? order by bar_time", (code,)).fetchall()
        if len(rows) < 40:
            continue
        df = pd.DataFrame([dict(r) for r in rows])
        df["day"] = df["bar_time"].str[:10]
        df["hm"] = df["bar_time"].str[11:16]
        recs = []
        for day, g in df.groupby("day"):
            g = g.sort_values("bar_time")
            if len(g) < 40:
                continue
            o = float(g["open"].iloc[0])
            c0 = float(g["close"].iloc[-1])
            hi = float(g["high"].max())
            lo = float(g["low"].min())
            vol = float(g["volume"].sum()) or 1.0
            # 前 6 根(9:30-10:00)、后 6 根(14:30-15:00)
            head = g.head(6)
            tail = g.tail(6)
            am = g[g["hm"] <= "11:30"]
            pm = g[g["hm"] >= "13:00"]
            rets = g["close"].pct_change().dropna()
            recs.append({
                "day": day,
                "gap": 0.0,                       # 需要前收,后面补
                "tail_ret": (float(tail["close"].iloc[-1]) / float(tail["open"].iloc[0]) - 1)
                            if len(tail) else 0.0,
                "head_vol": float(head["volume"].sum()) / vol,
                "tail_vol": float(tail["volume"].sum()) / vol,
                "intra_mom": ((float(pm["close"].iloc[-1]) / float(pm["open"].iloc[0]) - 1)
                              if len(pm) else 0.0)
                             - ((float(am["close"].iloc[-1]) / float(am["open"].iloc[0]) - 1)
                                if len(am) else 0.0),
                "intra_vol": float(rets.std(ddof=0)) if len(rets) > 3 else 0.0,
                "close_pos": (c0 - lo) / (hi - lo) if hi > lo else 0.5,
                "_open": o,
                "_close": c0,
            })
        if not recs:
            continue
        d = pd.DataFrame(recs).set_index("day").sort_index()
        out[code] = d
    return out


def main() -> int:
    log("读 5 分钟线…")
    intra = load_intraday()
    log(f"{len(intra)} 只票有数据")

    # 日线前收/次日收益
    c = store().conn
    daily: dict[str, pd.DataFrame] = {}
    for code in intra:
        rows = c.execute(
            "select bar_time, close from kline_cache where klt=101 and fqt=1 "
            "and code=? order by bar_time", (code,)).fetchall()
        if len(rows) < 30:
            continue
        daily[code] = pd.DataFrame([dict(r) for r in rows]).set_index("bar_time")

    # 补 gap 和次日收益
    feats = ["gap", "tail_ret", "head_vol", "tail_vol",
             "intra_mom", "intra_vol", "close_pos"]
    panels: dict[str, dict[str, float]] = {f: {} for f in feats}
    fwd: dict[str, dict[str, float]] = {}
    for code, d in intra.items():
        dl = daily.get(code)
        if dl is None:
            continue
        idx = list(dl.index)
        for day in d.index:
            if day not in dl.index:
                continue
            i = idx.index(day)
            prev = float(dl["close"].iloc[i - 1]) if i > 0 else None
            nxt = float(dl["close"].iloc[i + 1]) if i + 1 < len(idx) else None
            if prev and prev > 0:
                panels["gap"].setdefault(day, {})[code] = float(d.loc[day, "_open"]) / prev - 1
            for f in feats[1:]:
                panels[f].setdefault(day, {})[code] = float(d.loc[day, f])
            if nxt and nxt > 0:
                fwd.setdefault(day, {})[code] = nxt / float(d.loc[day, "_close"]) - 1

    days = sorted(fwd)
    log(f"有效交易日 {len(days)},平均每天 {np.mean([len(v) for v in fwd.values()]):.0f} 只")
    if len(days) < 5:
        log("样本太少,没法算")
        return 1

    rows = []
    for f in feats:
        ics, spreads = [], []
        for day in days:
            fv = panels[f].get(day) or {}
            fv = {k: v for k, v in fv.items() if k in fwd[day]}
            if len(fv) < 20:
                continue
            s = pd.Series(fv)
            y = pd.Series({k: fwd[day][k] for k in fv})
            ics.append(float(s.corr(y, method="spearman")))
            # 十分位多空
            k = max(3, len(s) // 5)
            top = s.nlargest(k).index
            bot = s.nsmallest(k).index
            spreads.append(float(y.reindex(top).mean() - y.reindex(bot).mean()))
        if len(ics) < 5:
            continue

        def tstat(xs):
            n = len(xs)
            m = float(np.mean(xs))
            sd = float(np.std(xs, ddof=1))
            return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0

        rows.append({
            "factor": f, "n_days": len(ics),
            "ic_mean": round(float(np.mean(ics)), 4),
            "ic_t": round(tstat(ics), 2),
            "spread_mean": round(float(np.mean(spreads)), 5),
            "spread_t": round(tstat(spreads), 2),
        })

    fdr = mt.fdr_report([r["ic_t"] for r in rows])
    print()
    print(f"{'因子':<12} {'天数':>4} {'IC均值':>8} {'IC t':>7} {'多空均值':>10} {'多空 t':>7}")
    for r in sorted(rows, key=lambda x: -abs(x["ic_t"])):
        print(f"{r['factor']:<12} {r['n_days']:>4} {r['ic_mean']:>+8.4f} "
              f"{r['ic_t']:>+7.2f} {r['spread_mean']:>+10.4f} {r['spread_t']:>+7.2f}")
    print()
    log(fdr["verdict"])

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "n_codes": len(intra), "n_days": len(days),
           "rows": rows, "fdr": fdr}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("intraday-audit-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
