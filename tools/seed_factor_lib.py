# -*- coding: utf-8 -*-
"""给因子库填几条真实审计记录(自检时不该是空的)。

只审 5 个代表性因子,几分钟;要全量审用 factor_evolve.evolve。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.engine import factor_evolve as fe  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402

PICKS = [
    ("-roc(close,20)", "反转20"),
    ("close/ma(close,20)-1", "均线偏离20"),
    ("ts_corr(close,volume,20)", "量价相关20"),
    ("slope(close,20)/(close+1e-9)", "趋势斜率20"),
    ("-skew(roc(close,1),20)", "收益偏度20"),
]


def main() -> int:
    panel = fm._panel(days=250, max_stocks=300, market="a")
    for expr, label in PICKS:
        t = time.time()
        r = fe.audit_and_record(expr, label, panel=panel, horizon=5)
        if not r.get("ok"):
            print(f"  {label:<14} 失败 {r.get('msg')}")
            continue
        print(f"  {label:<14} 原始 t={r['raw_t']} 中性 t={r['neu_t']} "
              f"→ {r['status']}  ({time.time() - t:.0f}s)", flush=True)
    print(f"\n因子库现有 {len(fm.load_library())} 条")
    for row in fe.leaderboard(8):
        print(f"  {row['label']:<14} {row['status']:<10} "
              f"原始 t={row['raw_t']} 中性 t={row['neu_t']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
