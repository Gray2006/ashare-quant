# -*- coding: utf-8 -*-
"""回撤闸行为自检 + 记录一个设计问题。

问题:peak 只增不减(max(peak, equity)),一次 30% 回撤后
即使反弹到 25%,按 max_pct=10% / resume_ratio=0.5 也要
dd <= 5% 才解除 —— 意味着要涨回峰值的 95%,基本等于永久停手。

这里锁住当前行为,并把问题写进 docs。改不改要主人定。

状态存在 SQLite 的 account 表里,所以用临时库隔离,别污染生产数据。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# 注意:quantlab.data.store 这个模块名被包里的同名函数遮蔽了
# (quantlab/data/__init__.py 里 from .store import store),
# 所以 `import quantlab.data.store as x` 拿到的是函数。用 importlib 绕开。
import importlib

st_mod = importlib.import_module("quantlab.data.store")
Store = st_mod.Store
from quantlab.engine.adaptive import DrawdownGuard  # noqa: E402


@pytest.fixture()
def tmp_store(monkeypatch):
    old = st_mod._store
    st_mod._store = Store(Path(tempfile.mkdtemp()) / "t.db")
    try:
        yield st_mod._store
    finally:
        st_mod._store = old


def test_no_limit_when_max_pct_zero(tmp_store):
    g = DrawdownGuard("t0")
    out = g.evaluate(100.0, max_pct=0.0)
    assert out["triggered"] is False
    assert out["block_buys"] is False


def test_trigger_on_drawdown(tmp_store):
    g = DrawdownGuard("t1")
    g.reset()
    g.evaluate(100.0, max_pct=0.10)
    out = g.evaluate(85.0, max_pct=0.10)      # dd = 15%
    assert out["triggered"] is True
    assert out["block_buys"] is True


def test_resume_needs_deep_recovery(tmp_store):
    """回撤 15% 后,要涨回峰值 95% 才解除 —— 锁住这个偏严的行为。"""
    g = DrawdownGuard("t2")
    g.reset()
    g.evaluate(100.0, max_pct=0.10)
    g.evaluate(85.0, max_pct=0.10, action="stop_new", resume_ratio=0.5)
    out = g.evaluate(92.0, max_pct=0.10, action="stop_new", resume_ratio=0.5)
    assert out["triggered"] is True, "dd=8% 仍高于解除线 5%"
    out = g.evaluate(96.0, max_pct=0.10, action="stop_new", resume_ratio=0.5)
    assert out["triggered"] is False


def test_peak_monotonic(tmp_store):
    """peak 只增不减 —— 这是「永久停手」风险的来源。"""
    g = DrawdownGuard("t3")
    g.reset()
    g.evaluate(100.0, max_pct=0.10)
    out1 = g.evaluate(80.0, max_pct=0.10)
    out2 = g.evaluate(90.0, max_pct=0.10)
    assert out2["peak"] == out1["peak"] == 100.0


def test_reset_clears(tmp_store):
    g = DrawdownGuard("t4")
    g.evaluate(100.0, max_pct=0.10)
    g.evaluate(80.0, max_pct=0.10)
    g.reset()
    out = g.evaluate(80.0, max_pct=0.10)
    assert out["dd"] == 0.0
    assert out["triggered"] is False
