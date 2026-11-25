# -*- coding: utf-8 -*-
"""验证两个候选信号:低价股 + 低波动。

两个都是在「三重障碍标签」(止盈5%/止损3%/5天)下浮现的:
  低价股  原始 close 的横截面 rank  t=-2.84
  低波动  -ts_std(roc(close,1),20)  t=+3.48(中性化后 +3.67)

这轮做四件事:
  1 beta 中性化(低价股尤其可能是小市值/高 beta 的伪装)
  2 扩样本到 750 天
  3 分半段看稳定
  4 概念漂移检测
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

from quantlab.engine import drift as dr  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402
from quantlab.engine import labeling as lb  # noqa: E402


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


def tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0
    m = float(np.mean(xs))
    sd = float(np.std(xs, ddof=1))
    return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0


def neutral_against(fac: pd.DataFrame, beta: pd.DataFrame) -> pd.DataFrame:
    """逐日把因子对 beta 做横截面回归,取残差。"""
    rows = {}
    for d in fac.index:
        a = fac.loc[d].dropna()
        b = beta.loc[d].reindex(a.index).dropna() if d in beta.index else None
        if b is None or len(a) < 30:
            continue
        a = a.reindex(b.index)
        if len(a) < 30 or b.std() == 0:
            continue
        coef = np.polyfit(b.values, a.values, 1)
        rows[d] = pd.Series(a.values - np.polyval(coef, b.values), index=a.index)
    return pd.DataFrame(rows).T


def main() -> int:
    ap_days = int(sys.argv[1]) if len(sys.argv) > 1 else 750
    log(f"取面板 {ap_days} 天 / 300 只…")
    panel = fm._panel_unbiased(days=ap_days, max_stocks=300, market="a")
    close = panel["close"]
    log(f"面板 {close.shape}")

    log("三重障碍标签…")
    t0 = time.time()
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    ret_df = pd.DataFrame(lab["ret"], index=close.index, columns=close.columns)
    log(f"  {time.time() - t0:.0f}s")

    # beta:60 日滚动,相对全池等权
    rets = close.pct_change()
    mkt = rets.mean(axis=1)
    beta = rets.apply(lambda s: s.rolling(60, min_periods=30).cov(mkt)) \
               .div(mkt.rolling(60, min_periods=30).var(), axis=0)

    signals = {
        "低价股(-rank close)": (-close.rank(axis=1, pct=True)),
        "低波动(-20日波动)": (-rets.rolling(20, min_periods=10).std()),
    }

    rows = []
    for name, fac in signals.items():
        if fac is None or fac.empty:
            continue
        ic_raw = ic_series(fac, ret_df)
        neu = neutral_against(fac, beta)
        ic_neu = ic_series(neu, ret_df)
        half = len(ic_raw) // 2
        d = dr.drift_report(ic_raw)
        rec = {
            "name": name,
            "n": len(ic_raw),
            "raw_ic": round(float(np.mean(ic_raw)), 4),
            "raw_t": round(tstat(ic_raw), 2),
            "neu_ic": round(float(np.mean(ic_neu)), 4) if ic_neu else None,
            "neu_t": round(tstat(ic_neu), 2) if ic_neu else None,
            "half1": [round(float(np.mean(ic_raw[:half])), 4),
                      round(tstat(ic_raw[:half]), 2)],
            "half2": [round(float(np.mean(ic_raw[half:])), 4),
                      round(tstat(ic_raw[half:]), 2)],
            "drift": {"flips": d["sign_flips"], "cusum": d["cusum_break"],
                      "segments": d["segments"].get("verdict")},
        }
        rows.append(rec)
        log(f"{name}")
        log(f"  原始  IC {rec['raw_ic']:+.4f}  t={rec['raw_t']:+.2f}")
        log(f"  中性化 IC {rec['neu_ic']}  t={rec['neu_t']}")
        log(f"  前半 t={rec['half1'][1]:+.2f}  后半 t={rec['half2'][1]:+.2f}")
        log(f"  漂移 翻转 {rec['drift']['flips']} 次, CUSUM {rec['drift']['cusum']}, "
            f"{rec['drift']['segments']}")

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": ap_days, "n_codes": close.shape[1], "rows": rows}
    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("signal-verify-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
