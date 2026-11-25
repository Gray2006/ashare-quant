# -*- coding: utf-8 -*-
"""对照:同样的信号,固定收益标签 vs 三重障碍标签。

三重障碍下低价股 t=6.95、低波动 t=7.65,高得可疑。
必须排除「标签构造的机械关联」——
低价股波动大容易碰止盈止损,低波动股不容易碰止损、更容易到期拿正收益,
这两条都可能让 IC 虚高,但**实盘赚不到**。
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
from quantlab.engine import labeling as lb  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ic_series(fac, y):
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
    log("取面板 750 天 / 300 只…")
    panel = fm._panel_unbiased(days=750, max_stocks=300, market="a")
    close = panel["close"]
    log(f"面板 {close.shape}")

    # 标签 A:固定 5 日收益
    fwd = close.shift(-5) / close - 1.0
    # 标签 B:三重障碍实现收益
    log("三重障碍标签…")
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    ret_df = pd.DataFrame(lab["ret"], index=close.index, columns=close.columns)
    # 标签 C:三重障碍的「标签」(离散 -1/0/1)
    lab_df = pd.DataFrame(lab["label"], index=close.index, columns=close.columns)

    rets = close.pct_change()
    signals = {
        "低价股": -close.rank(axis=1, pct=True),
        "低波动": -rets.rolling(20, min_periods=10).std(),
    }

    rows = []
    print()
    print(f"{'信号':<8} {'固定收益 t':>10} {'障碍收益 t':>10} {'障碍标签 t':>10}")
    for name, fac in signals.items():
        if fac is None or fac.empty:
            continue
        t_fixed = tstat(ic_series(fac, fwd))
        t_bar = tstat(ic_series(fac, ret_df))
        t_lab = tstat(ic_series(fac, lab_df))
        rows.append({"name": name, "t_fixed": round(t_fixed, 2),
                     "t_barrier_ret": round(t_bar, 2),
                     "t_barrier_label": round(t_lab, 2)})
        print(f"{name:<8} {t_fixed:>+10.2f} {t_bar:>+10.2f} {t_lab:>+10.2f}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": 750, "rows": rows}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("label-robust-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    log("读法:三种标签下都显著才算真;只有障碍标签显著 = 标签构造的产物")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
