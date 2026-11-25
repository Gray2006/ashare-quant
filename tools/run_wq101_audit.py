# -*- coding: utf-8 -*-
"""WorldQuant 101 Alphas 的一个子集,用项目自己的 dsl 写出来跑审计。

为什么抄这个:它是**公开的、有论文的、被广泛引用**的因子集。
如果连它在我们数据上也不行,那说明问题在数据,不在「没想到好因子」。

只挑能用现有字段算出来的(close/open/high/low/volume/amount)。
vwap 用 amount/volume 近似,returns 用 roc(close,1)。

跑完过双口径 + FDR 校正 —— 20 个假设一起测,不校正必然出假阳性。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from quantlab.engine import factor_analysis as fa  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402
from quantlab.engine import multiple_testing as mt  # noqa: E402

# (名字, 表达式) —— 尽量贴近原公式,部分用近似
ALPHAS = [
    ("a02", "-ts_corr(rank(delta(log(volume),2)), rank((close-open)/open), 6)"),
    ("a03", "-ts_corr(rank(open), rank(volume), 10)"),
    ("a04", "-ts_rank(rank(low), 9)"),
    ("a06", "-ts_corr(open, volume, 10)"),
    ("a08", "-rank(sum(open,5)*sum(roc(close,1),5) - ref(sum(open,5)*sum(roc(close,1),5),10))"),
    ("a09", "sign(delta(close,1)) * (1 - rank(ts_std(roc(close,1),5)/roc(close,1)))"),
    ("a10", "rank(ts_std(roc(close,1),20) * ts_corr(close,open,10))"),
    ("a12", "sign(delta(volume,1)) * (-1 * delta(close,1))"),
    ("a13", "-rank(ts_cov(rank(close), rank(volume), 5))"),
    ("a14", "-rank(delta(roc(close,1),3)) * ts_corr(open, volume, 10)"),
    ("a15", "-sum(rank(ts_corr(rank(high), rank(volume), 3)), 3)"),
    ("a16", "-rank(ts_cov(rank(high), rank(volume), 5))"),
    ("a18", "-rank((ts_std(abs(close-open),5) + (close-open)) + ts_corr(close,open,10))"),
    ("a20", "-rank(open - ref(high,1)) * rank(open - ref(close,1)) * rank(open - ref(low,1))"),
    ("a26", "-ts_argmax(ts_corr(ts_rank(volume,5), ts_rank(high,5), 5), 3)"),
    ("a33", "rank(-(1 - open/close))"),
    ("a34", "rank((1 - rank(ts_std(roc(close,1),2)/ts_std(roc(close,1),5))) + (1 - rank(delta(close,1))))"),
    ("a38", "-rank(ts_rank(close,10)) * rank(close/open)"),
    ("a40", "-rank(ts_std(high,10)) * ts_corr(high, volume, 10)"),
    ("a41", "-rank(high) * rank(volume)"),
    ("a44", "-ts_corr(high, rank(volume), 5)"),
    ("a45", "-rank(delta(amount/volume, 5)) * ts_corr(close, volume, 5)"),
    ("a46", "(0.25 - ts_argmax(ts_corr(rank(close), rank(volume), 3), 4)) * -1"),
    ("a49", "-rank(delta(close,5)) / (rank(delta(close,1)) + 1e-9)"),
    ("a51", "-rank(delta(amount/volume, 5)) * rank(ts_corr(close, volume, 5))"),
    ("a53", "-delta(close,5)"),
    ("a54", "-rank(ts_corr(close, volume, 10))"),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-stocks", type=int, default=500)
    ap.add_argument("--days", type=int, default=250)
    args = ap.parse_args()
    log("取面板…")
    panel = fm._panel_unbiased(days=args.days, max_stocks=args.max_stocks,
                               market="a")
    frames = fm._frames(panel)
    log(f"面板 {panel['close'].shape}")

    todo = ALPHAS[:args.limit] if args.limit else ALPHAS
    rows = []
    for name, expr in todo:
        try:
            fac = fm._factor_panel(panel, expr)
        except Exception as e:  # noqa: BLE001
            log(f"  {name} 求值失败 {str(e)[:50]}")
            continue
        if fac is None or fac.empty:
            continue
        scores = fac.rank(axis=1, pct=True)
        try:
            rep = fa.audit_scores(frames, scores, horizon=5)
        except Exception as e:  # noqa: BLE001
            log(f"  {name} 审计失败 {str(e)[:50]}")
            continue
        raw = rep.get("raw") or {}
        neu = rep.get("neutral") or {}
        rows.append({
            "alpha": name, "expr": expr,
            "ic": rep.get("rank_ic"),
            "raw_excess": raw.get("excess_mean"), "raw_t": raw.get("excess_t"),
            "neu_excess": neu.get("excess_mean"), "neu_t": neu.get("excess_t"),
        })
        log(f"  {name:<5} IC {rep.get('rank_ic')!r:>8} "
            f"原始t {raw.get('excess_t')!r:>6} 中性t {neu.get('excess_t')!r:>6}")

    fdr = mt.fdr_report([r["raw_t"] for r in rows])
    print()
    print(f"{'Alpha':<6} {'IC':>8} {'原始超额':>9} {'原始t':>7} {'中性t':>7}")
    for r in sorted(rows, key=lambda x: -abs(x["raw_t"] or 0)):
        def f(x, spec, d="-"):
            return d if x is None else format(x, spec)
        print(f"{r['alpha']:<6} {f(r['ic'], '+.4f'):>8} "
              f"{f(r['raw_excess'], '+.3%'):>9} {f(r['raw_t'], '+.2f'):>7} "
              f"{f(r['neu_t'], '+.2f'):>7}")
    print()
    log(fdr["verdict"])
    both = [r for r in rows
            if (r["raw_t"] or -9) >= 2 and (r["neu_t"] or -9) >= 2]
    log(f"两份 t 都 ≥2 的:{len(both)} 个")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "n": len(rows), "rows": rows, "fdr": fdr,
           "both_significant": len(both)}
    rep_dir = ROOT / "reports"
    rep_dir.mkdir(exist_ok=True)
    stem = time.strftime("wq101-audit-%Y-%m-%d")
    (rep_dir / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
