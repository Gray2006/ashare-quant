# -*- coding: utf-8 -*-
"""学习闭环体检:渲染函数测试(纯格式化,不读库)。

    python tests/test_learn_status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# learn_status 模块级没有 quantlab import(都在 collect 里),可以安全 import
from quantlab.engine.learn_status import render  # noqa: E402

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (passed if cond else failed).append(f"{name} {extra}".strip())


def main() -> int:
    d = {
        "ts": "2026-11-25 21:00:00", "account": "默认账户", "days": 90,
        "attribution": {"n": 300, "overall": {"net_mean": -0.02},
                        "by_strategy": [{"key": "multifactor", "n": 293,
                                         "t": -8.8, "state": "suspend",
                                         "mult": 0.5,
                                         "why": "扣费均值 -3.04%/笔"}],
                        "advice": ["策略 multifactor:建议暂停"]},
        "predict": {"n": 21, "acc": 0.048, "raw_n": 27,
                    "by_horizon": {"1": {"acc": 0.0, "n": 9}}},
        "versions": [{"account": "默认账户", "strategy": "multifactor",
                      "reason": "紧急降权:策略归因"}],
        "spaces": {"spaces": [{"label": "日线价量", "audited": 30,
                               "promoted": 0, "verdict": "dead",
                               "budget": 0}],
                   "verdict": "还该挖的空间:无"},
        "pool": {"n": 0, "by_status": {}},
        "errors": [],
    }
    out = render(d)
    check("有标题", "学习闭环体检" in out, out[:40])
    check("归因段", "## 归因" in out and "multifactor" in out, out[:200])
    check("归因理由", "扣费均值" in out)
    check("建议段", "建议:策略" in out)
    check("预测段带原始条数", "原始 27 条" in out, out)
    check("按周期", "1日" in out)
    check("参数调整段", "紧急降权" in out)
    check("空间段", "日线价量" in out and "dead" in out)
    check("观察池段", "## 观察池" in out)
    check("无错误段", "读取失败" not in out)

    # 口径漂移提示
    d["key_drift"] = {"config": "wfmulti", "db": "multifactor",
                      "n": 393, "mismatch": True}
    out2 = render(d)
    check("口径段", "## 口径" in out2 and "wfmulti" in out2, out2[-300:])
    check("漂移有警告", "⚠" in out2, out2[-200:])

    # 涨幅预选段(样本不足要提示)
    d["picks"] = {"days": 2, "runs": 9, "excess_mean": 0.0116, "excess_t": 4.36,
                  "verify_n": 47, "win_rate": 0.23, "hit_rate": 0.0,
                  "enough": False, "min_days": 10}
    out3 = render(d)
    check("预选段", "## 涨幅预选" in out3, out3[-400:])
    check("预选样本不足提示", "样本不够下结论" in out3, out3[-400:])

    # 分批 churn 段
    d["churn"] = {"orders": 46, "codes": 2, "buy_n": 6, "sell_n": 25,
                  "buy_amt": 114916, "sell_amt": 565060, "fees": 350,
                  "avg_fee": 1.5,
                  "top": [{"code": "002086", "name": "东方海洋", "orders": 6,
                           "dup": {"1/2": 4}}]}
    out4 = render(d)
    check("交易段", "## 当日交易与成本" in out4, out4[-400:])
    check("交易段数字", "买 6 笔" in out4 and "卖 25 笔" in out4, out4[-400:])
    check("费用行", "估费用 350" in out4, out4[-300:])

    # 自学习参数段
    d["selflearn"] = {"enabled": True, "tranche": True, "tune": True,
                      "auto_suspend": False, "hysteresis": 0.0,
                      "replan_tolerance": 0.05}
    out8 = render(d)
    check("参数段", "## 自学习参数" in out8, out8[-400:])
    check("参数生效值", "目标缓动 0%" in out8 and "容差 5%" in out8, out8[-300:])

    # 实盘 vs 基准段
    d["equity"] = {"account": "默认账户", "days": 8, "start": "2026-11-14",
                   "end": "2026-11-25", "ret": 0.0026, "bench": -0.0205,
                   "diff": 0.0231}
    out7 = render(d)
    check("实盘段", "## 实盘 vs 基准" in out7, out7[-400:])
    check("实盘数字", "+0.26%" in out7 and "-2.05%" in out7, out7[-300:])
    check("口径提醒", "总权益" in out7, out7[-200:])

    # 数据积累段
    d["data"] = {"5分钟": {"days": 33, "codes": 333,
                           "per_code_median": 23, "per_code_min": 22},
                 "60分钟": {"days": 282, "codes": 600,
                           "per_code_median": 256, "per_code_min": 250}}
    out6 = render(d)
    check("数据段", "## 数据积累" in out6, out6[-400:])
    check("数据段天数", "33 个交易日" in out6 and "282 个交易日" in out6, out6[-300:])
    d["data"]["日线"] = {"days": 0, "codes": 5098, "total": 5647,
                        "latest": "2026-11-25"}
    out6b = render(d)
    check("日线覆盖行", "5098/5647 只有最新日" in out6b, out6b[-300:])
    check("缺口提示", "缺 549 只" in out6b, out6b[-300:])
    d["data"] = {"5分钟": {"days": 70, "codes": 333, "per_code_median": 65}}
    check("够了会提示", "够重审" in render(d), render(d)[-200:])
    check("显示每只中位数", "每只中位 65 天" in render(d), render(d)[-200:])
    d["data"] = {"5分钟": {"days": 34, "codes": 396, "per_code_median": 24}}
    check("还差多少天", "还差 36 个交易日" in render(d), render(d)[-200:])

    # LLM 假设段
    d["hypothesis"] = {"day": "2026-11-26", "runs": 1, "generated": 5,
                       "audited": 5, "near_miss": 1}
    out5 = render(d)
    check("假设段", "## LLM 假设" in out5, out5[-400:])
    check("假设段数字", "5 条" in out5 and "beta 伪装 1 条" in out5, out5[-300:])

    bad = render({"attribution": {}, "predict": {}, "versions": [],
                  "spaces": {}, "pool": {}, "errors": ["归因:测试错误"]})
    check("空数据不炸", "没有已回填的决策" in bad, bad[:80])
    check("错误可见", "归因:测试错误" in bad, bad)

    print(f"通过 {len(passed)} / 失败 {len(failed)}")
    for f in failed:
        print("  FAIL", f)
    return 1 if failed else 0


def test_script_assertions_passed() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
