# -*- coding: utf-8 -*-
"""GP 因子挖掘测试(离线,合成宽表面板)。"""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["GRAY_HOME"] = tempfile.mkdtemp(prefix="gray-gp-")

from quantlab.engine import dsl  # noqa: E402
from quantlab.engine import factor_gp as gp  # noqa: E402
from quantlab.engine import factor_mine as fm  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def make_wide(n_codes=24, n_days=140, seed=5):
    rng = np.random.default_rng(seed)
    days = pd.date_range("2026-01-01", periods=n_days, freq="B")
    close = pd.DataFrame({
        f"{600000 + i:06d}": 10 * np.exp(np.cumsum(
            rng.normal(0.0005, 0.02, n_days))) for i in range(n_codes)},
        index=days)
    vol = pd.DataFrame(rng.uniform(1e6, 5e6, (n_days, n_codes)),
                       index=days, columns=close.columns)
    p = {"close": close, "open": close * 1.001, "high": close * 1.01,
         "low": close * 0.99, "volume": vol, "amount": close * vol,
         "codes": list(close.columns)}
    return p


panel = make_wide()
fm._panel = lambda days, max_stocks, market="a": panel

print("== 表达式树 ==")
t = ("b", "-", ("w", "ts_mean", ("v", "close"), 5), ("v", "close"))
expr = gp.render(t)
ok("渲染成 dsl 表达式", expr == "(ts_mean(close,5))-(close)", expr)
ok("表达式可编译", fm._valid(expr))
ok("树深计算", gp.depth_of(t) == 3, gp.depth_of(t))
m = gp.mutate(t, np.random.default_rng(1))
ok("变异后仍可编译", fm._valid(gp.render(m)))
c = gp.crossover(t, ("u", "rank", ("w", "roc", ("v", "close"), 10)),
                 np.random.default_rng(2))
ok("交叉后仍可编译", fm._valid(gp.render(c)))

print("== 评估(样本外) ==")
fwd = fm._fwd_panel(panel, 5)
st = gp.evaluate(panel, t, fwd)
ok("评估有训练/测试段", bool(st) and "train_ic" in st and "test_ic" in st, st)
ok("有覆盖率与适应度", st.get("coverage", 0) > 0.6 and "fitness" in st)
ok("裸变量不算因子", gp.evaluate(panel, ("v", "close"), fwd) == {})

print("== 遗传搜索 ==")
calls = []
r = gp.run(days=120, horizon=5, market="a", generations=2, population=8,
           time_budget=90, progress=lambda d: calls.append(d["generation"]))
ok("搜索成功", r.get("ok") is True, r.get("error"))
ok("有候选", len(r.get("rows") or []) > 0, len(r.get("rows") or []))
ok("有入选", len(r.get("selected") or []) >= 1, len(r.get("selected") or []))
ok("入选都通过样本外", all(x.get("oos_ok") for x in r.get("selected") or []))
ok("入选都至少一层运算", all(x.get("depth", 0) >= 2
                          for x in r.get("selected") or []))
ok("表达式都能编译", all(fm._valid(x["expr"]) for x in r.get("rows") or []))
ok("progress 每代回调", calls == [1, 2], calls)
ok("轮次历史有记录", len(r.get("rounds_history") or []) == 2)
ok("提前停止生效", gp.run(days=120, market="a", generations=2, population=8,
                       should_stop=lambda: True).get("rounds") == 0)

print(f"\n===== 通过 {PASS} / 失败 {FAIL} =====")
def test_script_assertions_passed():
    """import 期已跑完脚本断言;包一层让 pytest 能收集。"""
    assert FAIL == 0, f"{FAIL} 项脚本断言失败"


if __name__ == "__main__":
    raise SystemExit(0 if FAIL == 0 else 1)