# -*- coding: utf-8 -*-
"""分数阶差分能不能改进因子?拿三重障碍标签当裁判。

对比三个「价格位置」类因子:
  A 原始 close 的横截面 rank(不平稳,理论上最差)
  B -roc(close,20)(一阶差分,平稳但丢记忆)
  C 分数阶差分 d=0.4(平稳 + 保留记忆)

如果 C 比 B 好,说明「保留记忆」这件事在因子里有价值。
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

from quantlab.engine import factor_mine as fm  # noqa: E402
from quantlab.engine import fracdiff as fd  # noqa: E402
from quantlab.engine import labeling as lb  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ic_series(fac: pd.DataFrame, y: pd.DataFrame):
    out = []
    for d in fac.index:
        if d not in y.index:
            continue
        a = fac.loc[d].dropna()
        b = y.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        if len(a) < 20:
            continue
        c = a.corr(b, method="spearman")
        if np.isfinite(c):
            out.append(float(c))
    return out


def tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    m = float(np.mean(xs))
    sd = float(np.std(xs, ddof=1))
    return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0


def main() -> int:
    log("取面板…")
    panel = fm._panel_unbiased(days=250, max_stocks=200, market="a")
    close = panel["close"]
    log(f"面板 {close.shape}")

    log("三重障碍标签…")
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    ret_df = pd.DataFrame(lab["ret"], index=close.index, columns=close.columns)

    rows = []
    # A 原始 close(不平稳)
    a = close.rank(axis=1, pct=True)
    rows.append(("A 原始close", a, ret_df))

    # B 一阶差分
    b = (-close.pct_change(20)).rank(axis=1, pct=True)
    rows.append(("B 一阶差分(-20日收益)", b, ret_df))

    # C 分数阶差分
    for d in (0.3, 0.4, 0.5):
        log(f"  分数阶差分 d={d}…")
        t0 = time.time()
        fdp = fd.frac_diff_panel(panel, d=d, threshold=1e-3)
        if not fdp:
            continue
        c = fdp["close"].rank(axis=1, pct=True)
        log(f"    d={d} 用时 {time.time() - t0:.0f}s")
        rows.append((f"C 分数阶 d={d}", c, ret_df))

    print()
    print(f"{'因子':<22} {'天数':>4} {'IC均值':>9} {'t':>7}")
    out_rows = []
    for name, fac, y in rows:
        ics = ic_series(fac, y)
        if not ics:
            continue
        rec = {"name": name, "n": len(ics),
               "ic": round(float(np.mean(ics)), 4),
               "t": round(tstat(ics), 2)}
        out_rows.append(rec)
        print(f"{name:<22} {rec['n']:>4} {rec['ic']:>+9.4f} {rec['t']:>+7.2f}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "rows": out_rows, "tp": 0.05, "sl": 0.03}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("fracdiff-compare-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
