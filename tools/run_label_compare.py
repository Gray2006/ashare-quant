# -*- coding: utf-8 -*-
"""标签对比:固定 5 日收益 vs 三重障碍。

同一批因子、同一份数据,只换标签 —— 看「什么算成功」这件事影响多大。

固定收益标签问的是「5 天后涨没涨」;
三重障碍标签问的是「先碰到止盈还是止损」(止盈 5%、止损 3%、最长 5 天)。
后者更接近真实交易。
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

FACTORS = [
    ("反转20", "-roc(close,20)"),
    ("均线偏离20", "close/ma(close,20)-1"),
    ("量价相关20", "ts_corr(close,volume,20)"),
    ("趋势斜率20", "slope(close,20)/(close+1e-9)"),
    ("波动20", "-ts_std(roc(close,1),20)"),
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ic_series(fac: pd.DataFrame, y: pd.DataFrame) -> list[float]:
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


def tstat(xs: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    m = float(np.mean(xs))
    sd = float(np.std(xs, ddof=1))
    return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0


def main() -> int:
    log("取面板…")
    panel = fm._panel_unbiased(days=250, max_stocks=300, market="a")
    close = panel["close"]
    log(f"面板 {close.shape}")

    # 标签 A:固定 5 日收益
    fwd = close.shift(-5) / close - 1.0
    # 标签 B:三重障碍(止盈 5% / 止损 3% / 最长 5 天)
    log("打三重障碍标签(慢,逐票逐日)…")
    t0 = time.time()
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    log(f"  完成 {time.time() - t0:.0f}s")
    idx = close.index
    cols = close.columns
    lab_df = pd.DataFrame(lab["label"], index=idx, columns=cols)
    ret_df = pd.DataFrame(lab["ret"], index=idx, columns=cols)

    # 标签分布
    v = lab["label"][lab["label"] != 0]
    pos = float((v > 0).mean()) if len(v) else 0.0
    log(f"  非零标签 {len(v)} 个,其中止盈占 {pos:.1%}")

    rows = []
    for name, expr in FACTORS:
        try:
            fac = fm._factor_panel(panel, expr)
        except Exception as e:  # noqa: BLE001
            log(f"  {name} 求值失败 {str(e)[:40]}")
            continue
        if fac is None or fac.empty:
            continue
        ic_a = ic_series(fac, fwd)
        # 三重障碍:用「实现收益」当连续目标,方向由标签定
        ic_b = ic_series(fac, ret_df)
        rows.append({
            "factor": name,
            "ic_fixed": round(float(np.mean(ic_a)), 4) if ic_a else None,
            "t_fixed": round(tstat(ic_a), 2),
            "ic_barrier": round(float(np.mean(ic_b)), 4) if ic_b else None,
            "t_barrier": round(tstat(ic_b), 2),
            "n": min(len(ic_a), len(ic_b)),
        })
        log(f"  {name:<12} 固定 IC {rows[-1]['ic_fixed']:+.4f} (t={rows[-1]['t_fixed']:+.2f})"
            f"  障碍 IC {rows[-1]['ic_barrier']:+.4f} (t={rows[-1]['t_barrier']:+.2f})")

    print()
    print(f"{'因子':<12} {'固定收益IC':>10} {'t':>7} {'障碍收益IC':>11} {'t':>7}")
    for r in rows:
        print(f"{r['factor']:<12} {r['ic_fixed']:>+10.4f} {r['t_fixed']:>+7.2f} "
              f"{r['ic_barrier']:>+11.4f} {r['t_barrier']:>+7.2f}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "n_days": len(idx), "n_codes": len(cols),
           "tp": 0.05, "sl": 0.03, "horizon": 5,
           "label_pos_share": round(pos, 3), "rows": rows}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("label-compare-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
