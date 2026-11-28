# -*- coding: utf-8 -*-
"""权益快照的「疑似重置」判定测试。

背景(2026-11-28):
  2026-11-25 默认账户有持仓,权益 1,002,630。
  11-26 清仓后回到 1,000,000。
  原判定只看「权益回到初始 + 无持仓 + 上次不是初始」,于是判成账户被重置,
  跳过快照 —— 结果权益曲线从 11-26 起断掉。

  实际那是正常清仓(全部卖出),不是重置。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.daily_maintain import looks_reset  # noqa: E402

INIT = 1_000_000.0
PREV = 1_002_630.0


def test_normal_liquidation_not_reset() -> None:
    """当天有成交 → 正常清仓,不判重置。"""
    assert looks_reset(INIT, 0.0, INIT, PREV, had_trades=True) is False


def test_real_reset_detected() -> None:
    """权益回初始、无持仓、当天无成交 → 判重置。"""
    assert looks_reset(INIT, 0.0, INIT, PREV, had_trades=False) is True


def test_first_snapshot_not_reset() -> None:
    """没有上次记录时不判 —— 首次快照是正常的。"""
    assert looks_reset(INIT, 0.0, INIT, None) is False


def test_holding_not_reset() -> None:
    """有持仓时不判重置。"""
    assert looks_reset(INIT, 500_000.0, INIT, PREV) is False


def test_equity_above_initial_not_reset() -> None:
    """权益高于初始(赚了)时不判重置。"""
    assert looks_reset(1_050_000.0, 0.0, INIT, PREV) is False


def test_prev_was_initial_not_reset() -> None:
    """上次也是初始值(一直空仓)→ 不是重置,是没交易。"""
    assert looks_reset(INIT, 0.0, INIT, INIT) is False
