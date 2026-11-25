# -*- coding: utf-8 -*-
"""把 85 个候选因子逐个过一遍双口径判定,重点看新加的 7 条(qlib 风格)。

口径:
  · 因子面板 → 横截面 rank 打分 → Top20% 等权组合
  · audit_scores 一次给原始 / beta 中性化两份,各自扣费 + t 值
  · 判定:两份 t 都 ≥2 才算真 alpha;只有原始显著 = beta 伪装

用法:python tools/run_factor_audit.py [--max-stocks 800] [--days 250]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.engine import factor_analysis as fa  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402

# 今天新加的模板,单独标出来看
NEW_LABELS = ("趋势斜率", "趋势干净度", "趋势残差", "收益偏度",
              "收益峰度", "中位偏离", "量价协方差")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=600)
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--only-new", action="store_true",
                    help="只审今天新加的模板(35 个),快得多")
    ap.add_argument("--windows", default="", help="只审指定窗口,逗号分隔,如 20")
    args = ap.parse_args()

    log(f"取面板 {args.days} 天 / {args.max_stocks} 只…")
    panel = fm._panel_unbiased(days=args.days, max_stocks=args.max_stocks, market="a")
    close = panel.get("close")
    # audit_scores 的 _fwd_ret 要「逐票 dict」(每只票一个带 datetime 列的 DataFrame),
    # 宽表得先拆开,否则报 None of ['datetime'] are in the columns
    frames = fm._frames(panel)
    audit_panel = frames if frames else panel
    log(f"面板 {close.shape if close is not None else '-'},"
        f"逐票 {len(frames)} 只,开始逐个审")

    rows = []
    cands = fm.candidates()
    if args.only_new:
        cands = [c for c in cands if any(k in c["label"] for k in NEW_LABELS)]
    if args.windows:
        keep = {int(w) for w in args.windows.split(",") if w.strip().isdigit()}
        cands = [c for c in cands if c.get("window") in keep]
    log(f"待审 {len(cands)} 个因子")
    for i, c in enumerate(cands, 1):
        expr, label = c["expr"], c["label"]
        try:
            fac = fm._factor_panel(panel, expr)
        except Exception as e:  # noqa: BLE001
            log(f"  {label} 求值失败 {str(e)[:50]}")
            continue
        if fac is None or fac.empty:
            continue
        # 横截面 rank 打分(和项目里 45 因子的做法一致)
        scores = fac.rank(axis=1, pct=True)
        try:
            rep = fa.audit_scores(audit_panel, scores, horizon=args.horizon)
        except Exception as e:  # noqa: BLE001
            log(f"  {label} 审计失败 {str(e)[:50]}")
            continue
        raw = rep.get("raw") or {}
        neu = rep.get("neutral") or {}
        rows.append({
            "label": label, "expr": expr,
            "new": any(k in label for k in NEW_LABELS),
            "ic": rep.get("rank_ic"),
            "raw_excess": raw.get("excess_mean"),
            "raw_t": raw.get("excess_t"),
            "neu_excess": neu.get("excess_mean"),
            "neu_t": neu.get("excess_t"),
            "verdict": rep.get("verdict"),
        })
        if i % 10 == 0:
            log(f"  {i}/{len(cands)}")

    # 排序:两份 t 的最小值(要两边都强才算真)
    def score(r):
        vals = [v for v in (r["raw_t"], r["neu_t"]) if v is not None]
        return min(vals) if vals else -9

    rows.sort(key=lambda r: -score(r))

    log(f"\n审完 {len(rows)} 个因子")
    both = [r for r in rows if (r["raw_t"] or -9) >= 2 and (r["neu_t"] or -9) >= 2]
    log(f"两份 t 都 ≥2(真 alpha):{len(both)} 个")

    print("\n=== 双口径 t 最强的 12 个 ===")
    print(f"{'因子':<16} {'IC':>7} {'原始超额':>9} {'原始t':>7} {'中性超额':>9} {'中性t':>7}")
    for r in rows[:12]:
        def f(x, spec, d="-"):
            return d if x is None else format(x, spec)
        print(f"{r['label']:<16} {f(r['ic'], '+.4f'):>7} "
              f"{f(r['raw_excess'], '+.3%'):>9} {f(r['raw_t'], '+.2f'):>7} "
              f"{f(r['neu_excess'], '+.3%'):>9} {f(r['neu_t'], '+.2f'):>7}"
              + ("  ← 新" if r["new"] else ""))

    # 多重比较校正:测的因子越多,「撞上显著」越容易
    from quantlab.engine import multiple_testing as mt
    fdr = mt.fdr_report([r["raw_t"] for r in rows])
    log(f"  {fdr['verdict']}")
    out_fdr = fdr

    newrows = [r for r in rows if r["new"]]
    if newrows:
        print("\n=== 新加的 7 条模板 ===")
        for r in sorted(newrows, key=lambda x: -score(x)):
            def f(x, spec, d="-"):
                return d if x is None else format(x, spec)
            print(f"{r['label']:<16} IC {f(r['ic'], '+.4f')}  "
                  f"原始 {f(r['raw_t'], '+.2f')}  中性 {f(r['neu_t'], '+.2f')}  "
                  f"{r['verdict'][:40] if r['verdict'] else ''}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": args.days, "max_stocks": args.max_stocks,
           "horizon": args.horizon, "n": len(rows),
           "both_significant": len(both), "rows": rows,
           "fdr": out_fdr}
    rep_dir = ROOT / "reports"
    rep_dir.mkdir(exist_ok=True)
    stem = time.strftime("factor-audit-%Y-%m-%d")
    (rep_dir / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
