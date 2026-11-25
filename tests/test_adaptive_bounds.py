# -*- coding: utf-8 -*-
"""自适应系数边界自检。

注意:AdaptiveSizer 的状态存在 SQLite 的 account 表里(键 autotrade_adaptive),
不是文件。所以这里**只测纯函数 _decide**,不碰真实库,避免污染生产数据。

adaptive_enabled 默认从 False 改成 True(2026-11-26),实盘仓位会跟着变,
必须锁住:系数只在 [0.60, 1.25],样本不足保持 1.0,
「一直没交易」不能被当成「一直输」。
"""
from __future__ import annotations

import pytest


def _sizer() -> "object":
    from quantlab.engine.adaptive import AdaptiveSizer
    return AdaptiveSizer("t", "acc")


def test_multiplier_bounds():
    sz = _sizer()
    # 大赚 → 1.25
    m, _ = sz._decide([0.03] * 20, 1.0)
    assert m == pytest.approx(1.25)
    # 大亏 → 0.60
    m, _ = sz._decide([-0.03] * 20, 1.0)
    assert m == pytest.approx(0.60)
    # 永不超出 [0.60, 1.25]
    for samples in ([0.05] * 30, [-0.05] * 30, [0.001] * 30):
        m, _ = sz._decide(samples, 1.0)
        assert 0.60 - 1e-9 <= m <= 1.25 + 1e-9


def test_insufficient_samples_keeps_one():
    sz = _sizer()
    m, why = sz._decide([0.05, 0.01], 1.0)
    assert m == pytest.approx(1.0)
    assert "不足" in why


def test_flat_samples_not_counted_as_loss():
    """权益几乎没动(空仓/没成交)不能被当成亏损。"""
    sz = _sizer()
    m, why = sz._decide([0.0] * 10, 1.0)
    assert m == pytest.approx(1.0)
    assert "不足" in why or "没动" in why


def test_bad_performance_drops():
    """胜率 25% 应该降到 0.60。"""
    sz = _sizer()
    samples = [0.01 if i % 4 == 0 else -0.02 for i in range(20)]
    m, why = sz._decide(samples, 1.0)
    assert m == pytest.approx(0.60)
    assert "0.60" in why


def test_good_performance_raises():
    sz = _sizer()
    samples = [0.02 if i % 4 != 0 else -0.005 for i in range(20)]
    m, _ = sz._decide(samples, 1.0)
    assert m >= 1.10 - 1e-9


def test_adaptive_config_default_on():
    from quantlab.config import DEFAULTS
    at = DEFAULTS.get("autotrade") or {}
    assert bool(at.get("adaptive_enabled")) is True
