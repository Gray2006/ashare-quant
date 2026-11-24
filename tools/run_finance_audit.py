# -*- coding: utf-8 -*-
"""财务因子审计:用 PIT 面板算基本面因子,过双口径判定。

财务数据是低频的(每季一次),所以因子值在两次财报之间是常数 ——
这类因子的超额主要来自「谁先公布/谁基本面在改善」,和价量因子不是一回事。
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

# 字段 → (显示名, 方向:+1 越大越好 / -1 越小越好)
FIELDS = {
    "roe": ("ROE", 1),
    "revenue_yoy": ("营收增速", 1),
    "profit_yoy": ("净利增速", 1),
    "gross_margin": ("毛利率", 1),
    "net_margin": ("净利率", 1),
    "debt_ratio": ("负债率", -1),
    "ocf_to_profit": ("现金流/利润", 1),
    "roic": ("ROIC", 1),
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_factor(pp: dict, dates: pd.Index, codes: list[str],
                 field: str, direction: int = 1) -> pd.DataFrame:
    """PIT 面板 → 宽表因子。缺数据留 NaN,由 rank 自然跳过。"""
    cols = {}
    for code in codes:
        per_day = pp.get(code)
        if not per_day:
            continue
        vals = []
        for d in dates:
            row = per_day.get(str(d)[:10])
            v = None if row is None else row.get(field)
            vals.append(float(v) if isinstance(v, (int, float)) else None)
        cols[code] = pd.Series(vals, index=dates, dtype="float64")
    if not cols:
        return pd.DataFrame()
    df = pd.DataFrame(cols).reindex(columns=codes)
    return df * direction


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-stocks", type=int, default=300)
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args()

    log("取价量面板…")
    panel = fm._panel(days=args.days, max_stocks=args.max_stocks, market="a")
    close = panel["close"]
    codes = list(close.columns)
    dates = close.index

    cache_path = ROOT / "data" / "finance_pit.json"
    if not cache_path.exists():
        log("还没有财务缓存,先拉数据(会慢,几分钟)")
        cache = fp.fetch(codes)
    else:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        missing = [c for c in codes if c not in cache]
        if missing:
            log(f"补拉 {len(missing)} 只…")
            cache = fp.fetch(missing, sleep=0.3)

    pp = fp.pit_panel(cache, [str(d)[:10] for d in dates])
    log(f"PIT 面板:{len(pp)} 只 / 请求 {len(codes)} 只")

    frames = fm._frames(panel)
    rows = []
    for field, (label, direction) in FIELDS.items():
        fac = build_factor(pp, dates, codes, field, direction)
        if fac.empty:
            log(f"  {label} 无数据")
            continue
        scores = fac.rank(axis=1, pct=True)
        try:
            rep = fa.audit_scores(frames, scores, horizon=args.horizon)
        except Exception as e:  # noqa: BLE001
            log(f"  {label} 审计失败 {str(e)[:60]}")
            continue
        raw = rep.get("raw") or {}
        neu = rep.get("neutral") or {}
        rows.append({
            "label": label, "field": field,
            "ic": rep.get("rank_ic"),
            "raw_excess": raw.get("excess_mean"),
            "raw_t": raw.get("excess_t"),
            "neu_excess": neu.get("excess_mean"),
            "neu_t": neu.get("excess_t"),
            "verdict": rep.get("verdict"),
        })
        log(f"  {label:<12} 原始 t={raw.get('excess_t')} 中性 t={neu.get('excess_t')}")

    both = [r for r in rows
            if (r["raw_t"] or -9) >= 2 and (r["neu_t"] or -9) >= 2]
    log(f"\n{len(rows)} 个财务因子,两份 t 都 ≥2 的 {len(both)} 个")
    print(f"\n{'因子':<14} {'IC':>8} {'原始超额':>10} {'原始t':>7} {'中性超额':>10} {'中性t':>7}")
    for r in sorted(rows, key=lambda x: -(x["raw_t"] or -9)):
        def f(x, spec, d="-"):
            return d if x is None else format(x, spec)
        print(f"{r['label']:<14} {f(r['ic'], '+.4f'):>8} "
              f"{f(r['raw_excess'], '+.3%'):>10} {f(r['raw_t'], '+.2f'):>7} "
              f"{f(r['neu_excess'], '+.3%'):>10} {f(r['neu_t'], '+.2f'):>7}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": args.days, "max_stocks": args.max_stocks,
           "n_codes_with_finance": len(pp), "rows": rows,
           "both_significant": len(both)}
    rep_dir = ROOT / "reports"
    rep_dir.mkdir(exist_ok=True)
    stem = time.strftime("finance-audit-%Y-%m-%d")
    (rep_dir / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
