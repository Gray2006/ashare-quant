# -*- coding: utf-8 -*-
"""因子挖掘引擎测试(离线,合成面板)。"""

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["GRAY_HOME"] = tempfile.mkdtemp(prefix="gray-fm-")

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


def make_panel(n_codes=30, n_days=120, seed=7):
    rng = np.random.default_rng(seed)
    days = pd.date_range("2026-01-01", periods=n_days, freq="B")
    panel = {}
    for i in range(n_codes):
        ret = rng.normal(0.0005, 0.02, n_days)
        close = 10 * np.exp(np.cumsum(ret))
        vol = rng.uniform(1e6, 5e6, n_days)
        df = pd.DataFrame({
            "datetime": days,
            "open": close * (1 + rng.normal(0, 0.002, n_days)),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": vol,
            "amount": close * vol,
        })
        panel[f"{600000 + i:06d}"] = df
    return panel


panel = make_panel()
print("== 表达式求值 ==")
st = fm.evaluate_expr(panel, "roc(close,5)", horizon=5)
ok("有 IC/ICIR", "ic" in st and "icir" in st, st)
ok("IC 在合理范围", -1.0 <= st["ic"] <= 1.0, st.get("ic"))
ok("覆盖率接近 1", st["coverage"] > 0.9, st.get("coverage"))
ok("换手在 0~1", 0.0 <= st["turnover"] <= 1.0, st.get("turnover"))
ok("IC 序列有日期", len(st["ic_series"]) > 20 and len(st["ic_series"][0]) == 2)
ok("分层 5 组", len(st["layers"]) == 5, st.get("layers"))

print("== 坏表达式 ==")
ok("坏表达式返回空", fm.evaluate_expr(panel, "roc(close,", 5) == {})
ok("未知函数返回空", fm.evaluate_expr(panel, "no_such_fn(close,5)", 5) == {})

print("== 候选 ==")
cands = fm.candidates((3, 5))
ok("候选数 = 模板 × 窗口", len(cands) == len(fm.TEMPLATES) * 2, len(cands))
ok("候选表达式可编译", all(fm.dsl.compile_expr(c["expr"])
                          for c in cands))

print("== 挖掘 + 去重 ==")
fm._panel = lambda days, max_stocks, market="a": panel      # 不碰数据库
r = fm.mine(days=120, max_stocks=30, horizon=5, max_corr=0.99,
            windows=(3, 5, 10))
ok("挖掘返回行", r["ok"] and len(r["rows"]) > 10, len(r.get("rows") or []))
ok("按样本外优先、|测试ICIR| 排序",
   all((bool(r["rows"][i].get("oos_ok")), abs(r["rows"][i].get("test_icir") or 0))
       >= (bool(r["rows"][i + 1].get("oos_ok")),
           abs(r["rows"][i + 1].get("test_icir") or 0))
       for i in range(len(r["rows"]) - 1)))
ok("有入选因子", len(r["selected"]) >= 1, len(r["selected"]))
ok("入选都被标记", all(x["selected"] for x in r["selected"]))
ok("入选覆盖率达门槛", all(x["coverage"] >= 0.5 for x in r["selected"]))

fm._panel = lambda days, max_stocks, market="a": panel
r1 = fm.mine(days=120, max_stocks=30, horizon=5, max_corr=0.0,
             windows=(3, 5, 10))
ok("相关性门槛卡死时只留 1 个", len(r1["selected"]) == 1, len(r1["selected"]))

print("== 因子库 ==")
expr = "roc(close,7)"
ok("保存成功", fm.save_factor(expr, "测试因子", st) is True)
lib = fm.load_library()
ok("库里能读回", any(x["expr"] == expr for x in lib))
ok("重复保存被拒", fm.save_factor(expr) is False)
ok("坏表达式存不进去", fm.save_factor("bad(") is False)
ok("删除成功", fm.remove_factor(expr) is True
   and not any(x["expr"] == expr for x in fm.load_library()))

print("== 组合回测 ==")
# 宽表:close/volume/... 各一张 dates×codes
wpanel = {}
for name in ("open", "high", "low", "close", "volume"):
    wpanel[name] = pd.DataFrame({c: df.set_index("datetime")[name]
                                 for c, df in panel.items()})
wpanel["amount"] = wpanel["close"] * wpanel["volume"]
wpanel["codes"] = list(panel)
sel = [r for r in r["rows"] if r.get("selected")][:3] or r["rows"][:2]
sp = fm.score_panel(wpanel, sel)
ok("合成分数面板有值", not sp.empty and sp.notna().sum().sum() > 0)
bt = fm.composite_backtest(wpanel, sel, horizon=5, top_n=5)
ok("回测成功", bt.get("ok") is True, bt.get("error"))
ok("净值曲线覆盖全部交易日", len(bt.get("curve") or []) == len(wpanel["close"]))
m = bt.get("metrics") or {}
ok("指标齐全", all(k in m for k in ("total_return_pct", "annual_return_pct",
                                   "sharpe", "max_drawdown_pct")))
ok("回撤非负", m.get("max_drawdown_pct", 0) <= 0)
ok("有最新持仓", len(bt.get("holdings") or []) > 0)

print("== 自动落盘 ==")
res = {"ok": True, "rows": [{"expr": "roc(close,5)", "ic": 0.05}],
       "selected": [], "market": "a"}
ok("保存结果", fm.save_result("a", res) is True)
doc = fm.load_result("a")
ok("读回结果", (doc.get("result") or {}).get("rows") != [])
ok("今天已跑过", fm.auto_due("a") is False)
ok("换一天该跑", fm.auto_due("a", "2099-01-01") is True)

print(f"\n===== 通过 {PASS} / 失败 {FAIL} =====")
def test_script_assertions_passed():
    """import 期已跑完脚本断言;包一层让 pytest 能收集。"""
    assert FAIL == 0, f"{FAIL} 项脚本断言失败"


if __name__ == "__main__":
    raise SystemExit(0 if FAIL == 0 else 1)