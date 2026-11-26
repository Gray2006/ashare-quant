# -*- coding: utf-8 -*-
"""卖出不再分批自检。mock 掉 DB 方法,只测数量逻辑。"""
from __future__ import annotations

import importlib

import pytest

sl = importlib.import_module("quantlab.engine.selflearn")


@pytest.fixture()
def planner(monkeypatch):
    p = sl.TranchePlanner("test_acc")
    # 状态读写全部 mock,不碰 SQLite
    monkeypatch.setattr(p, "_get", lambda code, side: {})
    monkeypatch.setattr(p, "_put", lambda *a, **k: None)
    monkeypatch.setattr(p, "_drop", lambda *a, **k: None)
    return p


def test_sell_all_in_one_shot(planner):
    """3300 股一次卖完,不分批。"""
    r = planner.plan_sell("600519", 3300, 0.5, 100, price=17.8)
    assert r["qty"] == 3300
    assert r["tranches"] == 1


def test_sell_below_one_lot_returns_zero(planner):
    r = planner.plan_sell("600519", 50, 0.5, 100, price=17.8)
    assert r["qty"] == 0
    assert "一手" in r["why"]


def test_sell_small_amount_not_split(planner):
    """金额小于 MIN_AMOUNT 不拆,全卖。"""
    r = planner.plan_sell("600519", 200, 0.5, 100, price=10.0,
                          min_amount=5000.0)
    assert r["qty"] == 200


def test_sell_rounds_to_lot(planner):
    """非整手向下取整。"""
    r = planner.plan_sell("600519", 3350, 0.5, 100, price=17.8)
    assert r["qty"] == 3300
