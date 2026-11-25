# -*- coding: utf-8 -*-
"""单票当日买入上限自检。"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path



def _fresh_store():
    """建一个临时库,造几只票的委托。"""
    from quantlab.data.store import Store

    tmp = Path(tempfile.mkdtemp()) / "test.db"
    return Store(tmp), None


def test_orders_today_count_by_code():
    s, old = _fresh_store()
    try:
        now = time.time()
        for code in ("600519", "600519", "000001"):
            s.conn.execute(
                "INSERT INTO orders (ts, code, side, qty, price, status, account) "
                "VALUES (?,?,?,?,?,?,?)",
                (now, code, "buy", 100, 10.0, "filled", "default"))
        s.conn.commit()
        assert s.orders_today_count(code="600519") == 2
        assert s.orders_today_count(code="000001") == 1
        assert s.orders_today_count() == 3
        assert s.orders_today_count(code="688111") == 0
    finally:
        pass


def test_rejected_not_counted():
    s, old = _fresh_store()
    try:
        now = time.time()
        s.conn.execute(
            "INSERT INTO orders (ts, code, side, qty, price, status, account) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, "600519", "buy", 100, 10.0, "rejected", "default"))
        s.conn.commit()
        assert s.orders_today_count(code="600519") == 0
    finally:
        pass


def test_config_has_per_code_limit():
    from quantlab.config import DEFAULTS
    at = DEFAULTS.get("autotrade") or {}
    assert "max_buys_per_code_per_day" in at
    assert int(at["max_buys_per_code_per_day"]) == 3


def test_config_hysteresis_default():
    from quantlab.config import DEFAULTS
    sl = DEFAULTS.get("selflearn") or {}
    assert float(sl.get("target_hysteresis_pct") or 0) > 0, \
        "target_hysteresis_pct 应该 > 0,否则碎单连买"
