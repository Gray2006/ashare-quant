# -*- coding: utf-8 -*-
"""真样本外:用 2018-2022 的数据验证「低价股」和「低波动」。

之前那 750 天是 2023-2026。分半段只是同一批数据切两半,不算样本外。
这里换一段**完全没参与过**的历史 —— 如果信号还在,才是真的。

注意:股票池按**样本起点**的成交额选(无偏),不是按今天。
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
from quantlab.engine import labeling as lb  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_range(start: str, end: str, max_stocks: int = 300) -> dict:
    """按起点成交额选票,取 start~end 的日线宽表。"""
    c = store().conn
    # start 可能是元旦/周末,先找区间内第一个真有数据的交易日
    row = c.execute(
        "select min(bar_time) from kline_cache where klt=101 and fqt=1 "
        "and bar_time>=? and bar_time<=?", (start, end)).fetchone()
    first = str(row[0] or "") if row else ""
    if not first:
        return {}
    codes = [r[0] for r in c.execute(
        "select code from kline_cache where klt=101 and fqt=1 and bar_time=? "
        "and length(code)=6 and substr(code,1,1) in ('0','3','6') "
        "and close is not null "
        "order by coalesce(amount, close*volume*100.0) desc limit ?",
        (first, max_stocks * 2)).fetchall()]
    if not codes:
        return {}
    marks = ",".join("?" * len(codes))
    q = ("select code, bar_time, open, high, low, close, volume, amount "
         f"from kline_cache where klt=101 and fqt=1 and bar_time>=? and bar_time<=? "
         f"and code in ({marks})")
    df = pd.read_sql_query(q, c, params=[start, end] + codes)
    if df.empty:
        return {}
    # 只留数据够长的
    cnt = df.groupby("code")["close"].count()
    keep = set(cnt[cnt >= 200].index)
    df = df[df["code"].isin(keep)]
    if df.empty:
        return {}
    df["bar_time"] = pd.to_datetime(df["bar_time"])

    def wide(col):
        return df.pivot(index="bar_time", columns="code", values=col).sort_index()

    panel = {k: wide(k) for k in ("open", "high", "low", "close", "volume", "amount")}
    # 再截到 max_stocks
    close = panel["close"]
    cols = list(close.columns)[:max_stocks]
    return {k: v[cols] for k, v in panel.items()}


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


def run_batch(start: str, end: str, tag: str) -> dict:
    log(f"[{tag}] 取 {start} ~ {end}…")
    panel = load_range(start, end)
    if not panel:
        log("  没数据")
        return {}
    close = panel["close"]
    log(f"  {close.shape}")
    fwd = close.shift(-5) / close - 1.0
    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    ret_df = pd.DataFrame(lab["ret"], index=close.index, columns=close.columns)

    rets = close.pct_change()
    signals = {
        "低价股": -close.rank(axis=1, pct=True),
        "低波动": -rets.rolling(20, min_periods=10).std(),
    }
    rows = []
    for name, fac in signals.items():
        if fac is None or fac.empty:
            continue
        ic_fixed = ic_series(fac, fwd)
        ic_bar = ic_series(fac, ret_df)
        rows.append({
            "name": name,
            "t_fixed": round(tstat(ic_fixed), 2),
            "t_barrier": round(tstat(ic_bar), 2),
            "ic_fixed": round(float(np.mean(ic_fixed)), 4) if ic_fixed else None,
            "ic_barrier": round(float(np.mean(ic_bar)), 4) if ic_bar else None,
            "n": len(ic_fixed),
        })
        log(f"  {name}: 固定 t={rows[-1]['t_fixed']:+.2f} "
            f"障碍 t={rows[-1]['t_barrier']:+.2f}")
    return {"tag": tag, "start": start, "end": end,
            "shape": list(close.shape), "rows": rows}


def main() -> int:
    batches = [
        ("2018-01-01", "2018-12-31", "2018"),
        ("2019-01-01", "2019-12-31", "2019"),
        ("2020-01-01", "2020-12-31", "2020"),
        ("2021-01-01", "2021-12-31", "2021"),
        ("2022-01-01", "2022-12-31", "2022"),
    ]
    out = []
    for start, end, tag in batches:
        r = run_batch(start, end, tag)
        if r:
            out.append(r)

    print()
    print(f"{'年份':<8} {'信号':<8} {'固定t':>8} {'障碍t':>8} {'固定IC':>9}")
    for b in out:
        for r in b["rows"]:
            print(f"{b['tag']:<8} {r['name']:<8} {r['t_fixed']:>+8.2f} "
                  f"{r['t_barrier']:>+8.2f} {r['ic_fixed']:>+9.4f}")

    rep = ROOT / "reports"
    rep.mkdir(exist_ok=True)
    stem = time.strftime("oos-verify-%Y-%m-%d")
    (rep / f"{stem}.json").write_text(
        json.dumps({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "batches": out}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    log(f"报告 reports/{stem}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
