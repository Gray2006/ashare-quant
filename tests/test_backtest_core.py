# -*- coding: utf-8 -*-
"""回测口径的唯一出处:锁住「买入价必须是 T+1 开盘」这件事。

2026-11-24 踩过的坑:席位画像拿 T 日收盘当买入价,而龙虎榜是盘后才披露的,
凭空多出几个点的虚假收益。这里用测试把它钉死。
"""
from __future__ import annotations

import pytest

from quantlab.engine import backtest_core as bc


def _bars(closes, opens=None):
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        out.append({"datetime": f"2026-01-{i + 1:02d}", "open": o,
                    "high": max(o, c) * 1.01, "low": min(o, c) * 0.99,
                    "close": c, "volume": 1000.0, "amount": 1000.0 * c})
    return out


def test_buy_price_next_open_is_t_plus_1():
    bars = _bars([10.0, 12.0, 11.0], opens=[10.0, 12.0, 11.0])
    # 事件在索引 0,买入价必须是索引 1 的开盘
    assert bc.buy_price(bars, 0, "next_open") == pytest.approx(12.0)
    # 对照口径才用当天收盘
    assert bc.buy_price(bars, 0, "close") == pytest.approx(10.0)


def test_buy_price_returns_none_when_out_of_range():
    bars = _bars([10.0])
    assert bc.buy_price(bars, 0, "next_open") is None      # 没有次日
    assert bc.buy_price(bars, 5, "close") is None


def test_holding_return_uses_next_open_entry():
    bars = _bars([10.0, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5],
                 opens=[10.0, 12.0, 10.4, 10.9, 11.4, 11.9, 12.4])
    # 索引 0 事件 → 索引 1 开盘 12.0 买,持有 5 日 → 索引 6 收盘 12.5
    assert bc.holding_return(bars, 0, 5) == pytest.approx(12.5 / 12.0 - 1)


def test_net_return_subtracts_cost():
    assert bc.net_return(0.01, 0.0016) == pytest.approx(0.0084)
    assert bc.net_return(None) is None


def test_tstat_basic():
    assert bc.tstat([0.01] * 10) == 0.0          # 常数序列方差为 0
    assert bc.tstat([1.0, 2.0, 3.0]) > 3.0
    assert bc.tstat([-1.0, -2.0, -3.0]) < -3.0
    assert bc.tstat([]) == 0.0


def test_judge_flags_small_sample():
    r = bc.judge([0.01] * 5, bench=0.0, min_n=15)
    assert r["ok"] and "样本太少" in r["verdict"]


def test_judge_significant_positive():
    r = bc.judge([0.02, 0.021, 0.019, 0.022, 0.018] * 4, bench=0.0, min_n=15)
    assert r["ok"] and r["t"] > 2 and "显著正" in r["verdict"]


def test_judge_significant_negative():
    r = bc.judge([-0.02, -0.021, -0.019, -0.022, -0.018] * 4, bench=0.0, min_n=15)
    assert r["ok"] and r["t"] < -2 and "显著负" in r["verdict"]


def test_market_benchmark_empty_day_is_none():
    out = bc.market_benchmark(["1990-01-01"], hold=5)
    assert out["1990-01-01"] is None
