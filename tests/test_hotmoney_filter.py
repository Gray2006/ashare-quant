# -*- coding: utf-8 -*-
"""游资负面清单:位置计算 + 清单接口不炸。"""
from __future__ import annotations

import pytest

from quantlab.engine import hotmoney as hm
from quantlab.engine import hotmoney_filter as hf


def _bars(closes):
    return [{"datetime": f"2026-01-{i + 1:02d}", "open": c, "high": c * 1.01,
             "low": c * 0.99, "close": c, "volume": 1000.0} for i, c in enumerate(closes)]


def test_range_pos_at_extremes():
    # 区间用 high/low 算,收盘价到不了 1.0 —— 新高那天收盘在最高价下方
    bars = _bars([10.0] * 20 + [12.0])
    assert hf._range_pos(bars, len(bars) - 1) > 0.9
    bars2 = _bars([12.0] * 20 + [10.0])
    assert hf._range_pos(bars2, len(bars2) - 1) < 0.1


def test_range_pos_needs_enough_history():
    assert hf._range_pos(_bars([10.0] * 5), 4) is None


def test_hot_buys_returns_dict_on_known_day():
    day = hm.latest_day()
    if not day:
        pytest.skip("库里没有龙虎榜数据")
    buys = hf.hot_buys(day)
    assert isinstance(buys, dict)
    for code, info in list(buys.items())[:5]:
        assert len(code) == 6 and code.isdigit()
        assert info["net"] >= hf.NET_THRESHOLD


def test_check_unknown_code_is_not_risky():
    day = hm.latest_day()
    if not day:
        pytest.skip("库里没有龙虎榜数据")
    r = hf.check("999999", day)
    assert r["risky"] is False and r["reasons"] == []


def test_screen_shape():
    day = hm.latest_day()
    if not day:
        pytest.skip("库里没有龙虎榜数据")
    rows = hf.screen(day)
    assert isinstance(rows, list)
    for r in rows[:5]:
        assert r["risky"] is True and r["reasons"]
        assert r["pos"] is None or 0.0 <= r["pos"] <= 1.0
