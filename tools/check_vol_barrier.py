# -*- coding: utf-8 -*-
"""验证「波动20 在障碍标签下 t=3.48」是不是真的。

三步:
1. FDR 校正(5 个假设一起测)
2. beta 中性化(低波动因子最容易是 beta 伪装)
3. 分年/分半段看稳定
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
from quantlab.engine import multiple_testing as mt  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ic_series(fac: pd.DataFrame, y: pd.DataFrame, mask: pd.Series | None = None):
    out = []
    for d in fac.index:
        if d not in y.index:
            continue
        a = fac.loc[d].dropna()
        b = y.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        if mask is not None:
            m = mask.reindex(a.index).fillna(False)
            a, b = a[m], b[m]
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
    panel = fm._panel_unbiased(days=250, max_stocks=300, market="a")
    close = panel["close"]
    log(f"面板 {close.shape}")

    log("三重障碍标签…")
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    ret_df = pd.DataFrame(lab["ret"], index=close.index, columns=close.columns)

    # 1) 五个因子的 FDR
    factors = [("反转20", "-roc(close,20)"),
               ("均线偏离20", "close/ma(close,20)-1"),
               ("量价相关20", "ts_corr(close,volume,20)"),
               ("趋势斜率20", "slope(close,20)/(close+1e-9)"),
               ("波动20", "-ts_std(roc(close,1),20)")]
    ts = []
    for name, expr in factors:
        fac = fm._factor_panel(panel, expr)
        ts.append(round(tstat(ic_series(fac, ret_df)), 2))
    fdr = mt.fdr_report(ts)
    log(f"FDR:{fdr['verdict']}")
    log(f"  各因子 t:{dict(zip([f[0] for f in factors], ts))}")

    # 2) 波动20 的 beta 中性化
    log("波动20 做 beta 中性化…")
    fac = fm._factor_panel(panel, " -ts_std(roc(close,1),20)")
    if fac is None or fac.empty:
        return 1
    # 用 20 日收益算 beta,滚动
    rets = close.pct_change()
    mkt = rets.mean(axis=1)
    beta = {}
    for code in close.columns:
        r = rets[code]
        cov = r.rolling(60, min_periods=30).cov(mkt)
        var = mkt.rolling(60, min_periods=30).var()
        beta[code] = cov / var
    beta_df = pd.DataFrame(beta)
    # 每天把因子对 beta 做横截面回归,取残差
    resid = {}
    for d in fac.index:
        a = fac.loc[d].dropna()
        b = beta_df.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        if len(a) < 30 or b.std() == 0:
            continue
        x = np.polyfit(b.values, a.values, 1)
        resid[d] = pd.Series(a.values - np.polyval(x, b.values), index=a.index)
    resid_df = pd.DataFrame(resid).T
    ic_raw = ic_series(fac, ret_df)
    ic_neu = ic_series(resid_df, ret_df)
    log(f"  原始  IC {np.mean(ic_raw):+.4f} t={tstat(ic_raw):+.2f}")
    log(f"  中性化 IC {np.mean(ic_neu):+.4f} t={tstat(ic_neu):+.2f}")

    # 3) 分半段
    half = len(ic_raw) // 2
    a1, a2 = ic_raw[:half], ic_raw[half:]
    log(f"  前半 IC {np.mean(a1):+.4f} t={tstat(a1):+.2f}  "
        f"后半 IC {np.mean(a2):+.4f} t={tstat(a2):+.2f}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "fdr": fdr, "t_values": dict(zip([f[0] for f in factors], ts)),
           "vol_raw_ic": round(float(np.mean(ic_raw)), 4),
           "vol_raw_t": round(tstat(ic_raw), 2),
           "vol_neu_ic": round(float(np.mean(ic_neu)), 4),
           "vol_neu_t": round(tstat(ic_neu), 2),
           "half1": [round(float(np.mean(a1)), 4), round(tstat(a1), 2)],
           "half2": [round(float(np.mean(a2)), 4), round(tstat(a2), 2)]}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("vol-barrier-check-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
