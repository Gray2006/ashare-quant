# -*- coding: utf-8 -*-
"""最短持有约束的回归测试。

背景:回测 backtest.py 有 min_hold 强制,实盘 autotrader 完全没有,
导致同一策略回测胜率 62.4%、实盘 22.6%。这里锁住修复。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantlab.engine.autotrader import AutoTrader  # noqa: E402
from quantlab.engine.models import Position  # noqa: E402

MIN_HOLD = 3


def would_block(p: Position, min_hold: int = MIN_HOLD,
                now: float | None = None) -> bool:
    """复刻 autotrader 里的判断逻辑。"""
    now = time.time() if now is None else now
    opened = float(getattr(p, "opened_at", 0) or 0)
    if opened <= 0 or min_hold <= 0:
        return False
    return (now - opened) / 86400.0 < min_hold


def test_method_exists() -> None:
    assert hasattr(AutoTrader, "_min_hold")


def test_fresh_position_blocked() -> None:
    now = time.time()
    p = Position(code="600000", qty=1000, avail_qty=1000,
                 cost=10.0, opened_at=now - 86400)
    assert would_block(p, now=now) is True


def test_old_position_allowed() -> None:
    now = time.time()
    p = Position(code="600001", qty=1000, avail_qty=1000,
                 cost=10.0, opened_at=now - 5 * 86400)
    assert would_block(p, now=now) is False


def test_missing_opened_at_does_not_block() -> None:
    """opened_at=0 是数据缺失,不能当成「刚刚买入」把票锁死。"""
    p = Position(code="600002", opened_at=0)
    assert would_block(p) is False


def test_zero_min_hold_disables() -> None:
    now = time.time()
    p = Position(code="600003", opened_at=now - 3600)
    assert would_block(p, min_hold=0, now=now) is False


def test_order_after_stop_loss() -> None:
    """最短持有必须排在止损止盈之后 —— 保本动作不能被它挡住。"""
    src = (ROOT / "quantlab/engine/autotrader.py").read_text(encoding="utf-8")
    i_sl = src.find("sold_in_sl.add(code)")
    i_hold = src.find("min_hold = self._min_hold()")
    assert i_hold > 0, "最短持有代码不存在"
    assert i_sl > 0, "止损处理代码不存在"
    assert i_hold > i_sl, f"止损在 {i_sl},最短持有在 {i_hold},顺序反了"


def test_backtest_still_has_min_hold() -> None:
    """回测侧也要有,否则两边口径又分叉。"""
    bt = (ROOT / "quantlab/engine/backtest.py").read_text(encoding="utf-8")
    assert "min_hold" in bt


def test_missing_opened_at_not_filled_with_now() -> None:
    """持仓加载时 opened_at 缺失不能填 time.time()。

    填「现在」的后果:每次重启程序,所有持仓都变成「刚买入」,
    最短持有会永久锁死卖出。缺失字段不该改变交易行为。
    """
    src = (ROOT / "quantlab/broker/sim.py").read_text(encoding="utf-8")
    assert 'r.get("opened_at") or time.time()' not in src, \
        "opened_at 缺失时又用 time.time() 填了 —— 会让最短持有永久锁死"
    assert 'r.get("opened_at") or 0' in src, \
        "opened_at 缺失时应填 0(未知),让 _min_hold 放行"

