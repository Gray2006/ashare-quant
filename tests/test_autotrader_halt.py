# -*- coding: utf-8 -*-
"""自动交易参数落地:策略判死时只平不新开(纯函数测试)。

    python tests/test_autotrader_halt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantlab.engine.autotrader import apply_learning  # noqa: E402

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (passed if cond else failed).append(f"{name} {extra}".strip())


def main() -> int:
    # 1) 没变化、没 halt:原样返回
    p, sl, tp, note = apply_learning({"changed": False}, 0.1, 5.0, 10.0)
    check("无变化不动", (p, sl, tp, note) == (0.1, 5.0, 10.0, ""), str((p, sl, tp, note)))

    # 2) 有变化:按 tuner 结果覆盖
    tuned = {"changed": True, "reason": "胜率低",
             "params": {"per_position_pct": 0.05, "stop_loss": 4.0,
                        "take_profit": 9.0}}
    p, sl, tp, note = apply_learning(tuned, 0.1, 5.0, 10.0)
    check("有变化覆盖", (p, sl, tp) == (0.05, 4.0, 9.0), str((p, sl, tp)))
    check("理由带前缀", note.startswith("学习:胜率低"), note)

    # 3) halt:仓位归零,止损止盈不动
    halt = {"changed": False, "params": {"strategy_halt": True}}
    p, sl, tp, note = apply_learning(halt, 0.1, 5.0, 10.0)
    check("halt 仓位归零", p == 0.0, str(p))
    check("halt 不碰止损止盈", (sl, tp) == (5.0, 10.0), str((sl, tp)))
    check("halt 有说明", "只平不新开" not in note and "归零" in note, note)

    # 4) halt + 有变化:两者都生效,说明里两段都在
    both = {"changed": True, "reason": "归因判负",
            "params": {"per_position_pct": 0.03, "stop_loss": 4.0,
                       "take_profit": 8.0, "strategy_halt": True}}
    p, sl, tp, note = apply_learning(both, 0.1, 5.0, 10.0)
    check("halt 覆盖调参结果", p == 0.0, str(p))
    check("止损止盈仍按调参", (sl, tp) == (4.0, 8.0), str((sl, tp)))
    check("两段说明都在", "学习:归因判负" in note and "归零" in note, note)

    # 5) 空/None 不炸
    p, sl, tp, note = apply_learning(None, 0.2, 6.0, 12.0)
    check("None 不炸", (p, sl, tp) == (0.2, 6.0, 12.0), str((p, sl, tp)))

    print(f"通过 {len(passed)} / 失败 {len(failed)}")
    for f in failed:
        print("  FAIL", f)
    return 1 if failed else 0


def test_script_assertions_passed() -> None:
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
