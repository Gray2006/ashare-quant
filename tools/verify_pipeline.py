# -*- coding: utf-8 -*-
"""管道自检:用「未来函数」当因子,看审计流程能不能认出来。

背景:30 个真实因子全部 rejected。在相信这个结论之前,得先排除「工具坏了」——
如果连一个偷看未来的因子都跑不出高 t,那说明 audit_scores / factor_mine
的某一步有问题(方向反了、对齐错了、top_pct 取反了)。

三个对照:
  A. 因子 = 未来 5 日收益(偷看未来)  → 必须给出很高的正 t
  B. 因子 = 负的未来收益              → 必须给出很高的负 t
  C. 因子 = 随机数                    → t 应该接近 0

任何一项不符合预期,就是管道有 bug。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    from quantlab.engine import factor_analysis as fa
    from quantlab.engine import factor_mine as fm

    log("取面板…")
    panel = fm._panel(days=250, max_stocks=300, market="a")
    frames = fm._frames(panel)
    close = panel["close"]
    log(f"面板 {close.shape}")

    log("算未来 5 日收益…")
    fwd = fm._fwd_panel(panel, 5)
    log(f"未来收益面板 {fwd.shape}")

    # A:偷看未来
    scores_a = fwd.copy()
    rep_a = fa.audit_scores(frames, scores_a, horizon=5)
    ra = rep_a.get("raw") or {}
    log(f"A 偷看未来   原始超额 {ra.get('excess_mean'):+.4%} t={ra.get('excess_t')}")

    # B:反向
    scores_b = -fwd.copy()
    rep_b = fa.audit_scores(frames, scores_b, horizon=5)
    rb = rep_b.get("raw") or {}
    log(f"B 反向偷看   原始超额 {rb.get('excess_mean'):+.4%} t={rb.get('excess_t')}")

    # C:随机
    rng = np.random.default_rng(42)
    scores_c = pd.DataFrame(rng.normal(size=close.shape),
                            index=close.index, columns=close.columns)
    rep_c = fa.audit_scores(frames, scores_c, horizon=5)
    rc = rep_c.get("raw") or {}
    log(f"C 随机       原始超额 {rc.get('excess_mean'):+.4%} t={rc.get('excess_t')}")

    print()
    ok = True
    ta = ra.get("excess_t") or -9
    tb = rb.get("excess_t") or 9
    tc = abs(rc.get("excess_t") or 9)
    if ta < 5:
        print(f"✗ A 偷看未来 t={ta} <5,管道有问题")
        ok = False
    else:
        print(f"✓ A 偷看未来 t={ta:.2f} —— 管道能识别有效因子")
    if tb > -5:
        print(f"✗ B 反向 t={tb} >-5,方向可能没生效")
        ok = False
    else:
        print(f"✓ B 反向 t={tb:.2f} —— 方向确实反过来了")
    if tc > 3:
        print(f"✗ C 随机 t={tc:.2f} 绝对值太大,可能是噪声或对齐问题")
        ok = False
    else:
        print(f"✓ C 随机 t={tc:.2f} —— 接近 0,没有系统性偏差")

    print()
    print("结论:" + ("管道正常,30 个因子全 rejected 是真实结论"
                    if ok else "管道有问题,先修工具再看结论"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
