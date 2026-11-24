# -*- coding: utf-8 -*-
"""高 ROIC 排除:能不能降低回撤?

背景:3 年样本上 ROIC 中性化后 t=-2.05 —— 高 ROIC 的票跑输。
A 股不能做空,所以这不是 alpha,是**过滤器**。

做法:
  策略 A(基准) = 45 因子等权 rank,取 Top20%
  策略 B(过滤) = 同一个打分,但把 ROIC 截面分位 > 阈值的票打成 NaN,
                 Top20% 自然选不到它们
比较:收益、超额、最大回撤、夏普、以及 qagh 双口径判定。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from quantlab.engine import factor_analysis as fa  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402
from quantlab.engine import finance_pit as fp  # noqa: E402

FIELDS = ("roe", "revenue_yoy", "profit_yoy", "gross_margin",
          "net_margin", "debt_ratio", "ocf_to_profit", "roic")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def signal(panel: dict) -> pd.DataFrame:
    """45 因子那套的简化版:一批价量因子等权 rank。"""
    exprs = ["-roc(close,20)", "close/ma(close,20)-1", "-ts_std(roc(close,1),20)",
             "volume/ts_mean(volume,20)-1", "ts_corr(close,volume,20)",
             "50-rsi(14)", "(close-ts_mean(close,20))/ts_std(close,20)",
             "slope(close,20)/(close+1e-9)", "-skew(roc(close,1),20)"]
    ranks = []
    for e in exprs:
        try:
            fac = fm._factor_panel(panel, e)
        except Exception:  # noqa: BLE001
            continue
        if fac is not None and not fac.empty:
            ranks.append(fac.rank(axis=1, pct=True))
    if not ranks:
        return pd.DataFrame()
    return sum(ranks) / len(ranks)


def roic_pct(pp: dict, dates: pd.Index, codes: list[str]) -> pd.DataFrame:
    """ROIC 的横截面分位(按日算,PIT)。"""
    cols = {}
    for code in codes:
        per_day = pp.get(code)
        if not per_day:
            continue
        vals = []
        for d in dates:
            row = per_day.get(str(d)[:10])
            v = None if row is None else row.get("roic")
            vals.append(float(v) if isinstance(v, (int, float)) else None)
        cols[code] = pd.Series(vals, index=dates, dtype="float64")
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).reindex(columns=codes).rank(axis=1, pct=True)


def drawdown(nav: pd.Series) -> float:
    peak = nav.cummax()
    return float((nav / peak - 1).min())


def run_variant(panel, scores, roic, thr: float | None, horizon: int) -> dict:
    sc = scores.copy()
    if thr is not None and not roic.empty:
        # ROIC 分位高于阈值 → 打成 NaN,不进 Top20%
        mask = roic.reindex(index=sc.index, columns=sc.columns) > thr
        sc = sc.mask(mask)
    rep = fa.net_return(fm._frames(panel) or panel, sc, horizon=horizon, step=horizon)
    if not rep:
        return {}
    strat = rep["strategy"]
    # 净值
    fwd = fa._fwd_ret(fm._frames(panel) or panel, horizon)
    idx = list(sc.index)[::horizon]
    rets = []
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
        k = max(1, int(len(s) * 0.2))
        top = s.nlargest(k).index
        rt = fwd.loc[d].reindex(top).dropna()
        if not rt.empty:
            rets.append(float(rt.mean()))
    nav = pd.Series((1 + pd.Series([r - fa.COST_ROUND_TRIP for r in rets])).cumprod()) \
        if rets else pd.Series([1.0])
    return {
        "n_periods": len(rets), "mean": strat.get("mean"),
        "net_mean": strat.get("net_mean"), "annual": strat.get("annual"),
        "sharpe": strat.get("sharpe"), "win": strat.get("win"),
        "excess_mean": rep.get("excess_mean"), "excess_t": rep.get("excess_t"),
        "max_dd": round(drawdown(nav), 4) if len(nav) > 1 else 0.0,
        "final_nav": round(float(nav.iloc[-1]), 4) if len(nav) else 1.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=300)
    ap.add_argument("--days", type=int, default=750)
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--thr", default="0.8,0.7,0.6",
                    help="ROIC 分位阈值,逗号分隔;越高 = 只排除最贵的那批")
    args = ap.parse_args()

    log("取面板…")
    panel = fm._panel(days=args.days, max_stocks=args.max_stocks, market="a")
    close = panel["close"]
    codes, dates = list(close.columns), close.index

    log("算价量信号…")
    scores = signal(panel)
    if scores.empty:
        log("信号为空")
        return 1

    log("读财务 PIT…")
    cache_path = ROOT / "data" / "finance_pit.json"
    if not cache_path.exists():
        log("没有财务缓存,先跑 finance_pit.fetch")
        return 1
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    pp = fp.pit_panel(cache, [str(d)[:10] for d in dates])
    roic = roic_pct(pp, dates, codes)
    log(f"ROIC 覆盖 {roic.notna().mean().mean():.0%}")

    log(f"基准(A):不加过滤,持有 {args.horizon} 日")
    base = run_variant(panel, scores, roic, None, args.horizon)
    rows = [{"name": "A 不加过滤", **base}]

    for thr in [float(x) for x in args.thr.split(",") if x.strip()]:
        log(f"实验(B):排除 ROIC 分位 > {thr:.0%}")
        v = run_variant(panel, scores, roic, thr, args.horizon)
        rows.append({"name": f"B 排除 ROIC>{thr:.0%}", "thr": thr, **v})

    print(f"\n{'方案':<20} {'期数':>4} {'每期净':>8} {'年化':>8} "
          f"{'夏普':>6} {'最大回撤':>9} {'期末净值':>9}")
    for r in rows:
        print(f"{r['name']:<20} {r['n_periods']:>4} "
              f"{(r['net_mean'] or 0):>+7.2%} {(r['annual'] or 0):>+7.1%} "
              f"{(r['sharpe'] or 0):>6.2f} {r['max_dd']:>8.2%} {r['final_nav']:>9.3f}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": args.days, "max_stocks": args.max_stocks,
           "horizon": args.horizon, "rows": rows}
    rep_dir = ROOT / "reports"
    rep_dir.mkdir(exist_ok=True)
    stem = time.strftime("roic-filter-%Y-%m-%d")
    (rep_dir / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
