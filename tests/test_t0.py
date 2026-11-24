# -*- coding: utf-8 -*-
"""做T引擎自测:撮合、成本、T+1 锁、涨跌停、日线近似路径。"""
from __future__ import annotations

import pytest

from quantlab.engine.t0 import (DayResult, T0Config, T0Engine, buy_fee,
                                daily_pseudo_bars, round_qty, sell_fee,
                                summarize)


def _engine(**kw):
    base = dict(base_shares=1000, ratio=0.30, min_pct=0.01, take_pct=0.005)
    base.update(kw)
    return T0Engine("600000", T0Config(**base))


def test_reverse_round_trip_pnl_and_cost():
    eng = _engine()
    bars = [
        {"open": 10.00, "high": 10.12, "low": 10.00, "close": 10.10},
        {"open": 10.06, "high": 10.07, "low": 10.02, "close": 10.03},
    ]
    r = eng.run_day(bars, 10.00, "2026-11-20")
    assert r.filled and r.rounds == 1
    assert r.qty == 300
    assert r.sell_px == 10.10          # 10.00 * 1.01
    assert r.buy_px == 10.05           # 10.10 * 0.995,四舍五入到分
    assert r.gross == pytest.approx((10.10 - 10.05) * 300)
    assert r.cost == pytest.approx(sell_fee(10.10, 300, eng.cfg)
                                   + buy_fee(10.05, 300, eng.cfg))
    assert r.net == pytest.approx(r.gross - r.cost)
    assert eng.base == 1000            # 底仓不变


def test_reverse_unfilled_closes_at_last_close():
    eng = _engine()
    bars = [
        {"open": 10.00, "high": 10.12, "low": 10.00, "close": 10.08},
        {"open": 10.08, "high": 10.12, "low": 10.06, "close": 10.09},
    ]
    r = eng.run_day(bars, 10.00)
    assert not r.filled
    assert r.note.startswith("尾盘回补")
    assert eng.base == 1000            # 底仓不变
    assert r.buy_px == 10.09
    assert r.gross == pytest.approx((10.10 - 10.09) * 300)
    assert r.net == pytest.approx(r.gross - sell_fee(10.10, 300, eng.cfg)
                                  - buy_fee(10.09, 300, eng.cfg))


def test_forward_t_uses_base_for_sell():
    eng = _engine(mode="forward")
    bars = [
        {"open": 10.00, "high": 10.00, "low": 9.88, "close": 9.90},
        {"open": 9.92, "high": 9.96, "low": 9.90, "close": 9.95},
    ]
    r = eng.run_day(bars, 10.00)
    assert r.filled
    assert r.buy_px == 9.90            # 10.00 * 0.99
    assert r.sell_px == 9.95           # 9.90 * 1.005 = 9.9495 → 9.95
    assert eng.base == 1000


def test_forward_unfilled_closes_at_last_close():
    eng = _engine(mode="forward")
    bars = [{"open": 10.00, "high": 10.00, "low": 9.85, "close": 9.86}]
    r = eng.run_day(bars, 10.00)
    assert not r.filled and r.note.startswith("尾盘卖出")
    assert eng.base == 1000            # 底仓不变
    assert r.sell_px == 9.86
    assert r.gross == pytest.approx((9.86 - 9.90) * 300)
    assert r.net == pytest.approx(r.gross - buy_fee(9.90, 300, eng.cfg)
                                  - sell_fee(9.86, 300, eng.cfg))


def test_limit_up_open_skips():
    eng = _engine()
    bars = [{"open": 11.00, "high": 11.00, "low": 11.00, "close": 11.00}]
    r = eng.run_day(bars, 10.00)
    assert not r.filled and "涨停" in r.note
    assert eng.base == 1000


def test_too_small_base_no_trade():
    eng = _engine(base_shares=200, ratio=0.3)     # 60 股,不足一手
    r = eng.run_day([{"open": 10.0, "high": 10.5, "low": 10.0, "close": 10.4}], 10.0)
    assert not r.filled and "不足一手" in r.note


def test_round_qty_boards():
    assert round_qty(300, "600000") == 300
    assert round_qty(350, "600000") == 300
    assert round_qty(150, "600000") == 100
    assert round_qty(250, "688001") == 250      # 科创板 1 股递增
    assert round_qty(150, "688001") == 0        # 科创板 200 股起
    assert round_qty(150, "830001") == 150      # 北交所 1 股递增
    assert round_qty(50, "600000") == 0


def test_daily_pseudo_bars_order():
    yang = daily_pseudo_bars({"open": 10, "high": 11, "low": 9.5, "close": 10.8})
    assert yang[0]["high"] == 11 and yang[0]["low"] == 10     # 先冲高
    assert yang[1]["low"] == 9.5                              # 后回落
    yin = daily_pseudo_bars({"open": 10, "high": 10.4, "low": 9.2, "close": 9.4})
    assert yin[0]["low"] == 9.2 and yin[0]["high"] == 10      # 先砸低
    assert yin[1]["high"] == 10.4                             # 后反弹


def test_engine_run_daily_and_summarize():
    eng = _engine()
    days = [
        {"date": "d1", "open": 10.0, "high": 10.2, "low": 9.95, "close": 10.1,
         "pre_close": 10.0},
        {"date": "d2", "open": 10.1, "high": 10.15, "low": 10.0, "close": 10.05,
         "pre_close": 10.1},
    ]
    recs = eng.run(days)
    assert len(recs) == 2
    s = summarize(recs, base_value=10000.0)
    assert s["days"] == 2
    assert s["gross"] == pytest.approx(sum(r.gross for r in recs), abs=0.01)
    assert s["net"] == pytest.approx(sum(r.net for r in recs), abs=0.01)
    assert 0.0 <= s["win_rate"] <= 1.0


def test_carry_reverse_waits_until_next_day():
    eng = _engine(carry_unfilled=True)
    d1 = [{"open": 10.00, "high": 10.12, "low": 10.00, "close": 10.11}]
    r1 = eng.run_day(d1, 10.00, "2026-11-20")
    assert r1.note == "留仓待回补"
    assert eng.base == 700 and len(eng.pending) == 1
    d2 = [{"open": 10.11, "high": 10.11, "low": 10.02, "close": 10.04}]
    r2 = eng.run_day(d2, 10.11, "2026-11-23")
    assert r2.note == "次日回补"
    assert eng.base == 1000 and not eng.pending
    assert r2.net == pytest.approx((10.10 - 10.05) * 300
                                   - sell_fee(10.10, 300, eng.cfg)
                                   - buy_fee(10.05, 300, eng.cfg))


def test_carry_timeout_forces_close():
    eng = _engine(carry_unfilled=True, max_carry_days=2)
    eng.run_day([{"open": 10.00, "high": 10.12, "low": 10.00, "close": 10.11}],
                10.00, "2026-11-20")
    assert len(eng.pending) == 1
    eng.run_day([{"open": 10.12, "high": 10.30, "low": 10.12, "close": 10.25}],
                10.11, "2026-11-23")
    assert len(eng.pending) == 1
    r = eng.run_day([{"open": 10.25, "high": 10.40, "low": 10.25, "close": 10.35}],
                    10.25, "2026-11-24")
    assert r.note == "留仓超时强平"
    assert eng.base == 1000 and not eng.pending


def test_carry_forward_waits_until_next_day():
    eng = _engine(carry_unfilled=True, mode="forward")
    d1 = [{"open": 10.00, "high": 10.00, "low": 9.80, "close": 9.82}]
    r1 = eng.run_day(d1, 10.00, "2026-11-20")
    assert r1.note == "留仓待卖出"
    assert eng.base == 1300 and len(eng.pending) == 1
    d2 = [{"open": 9.82, "high": 9.95, "low": 9.82, "close": 9.94}]
    r2 = eng.run_day(d2, 9.82, "2026-11-23")
    assert r2.note == "次日卖出"
    assert eng.base == 1000 and not eng.pending


def test_run_flushes_pending_at_end():
    eng = _engine(carry_unfilled=True)
    days = [{"date": "2026-11-20", "pre_close": 10.0, "close": 10.11,
             "bars": [{"open": 10.00, "high": 10.12, "low": 10.00, "close": 10.11}]},
            {"date": "2026-11-23", "pre_close": 10.11, "close": 10.30,
             "bars": [{"open": 10.12, "high": 10.30, "low": 10.12, "close": 10.30}]}]
    recs = eng.run(days)
    assert not eng.pending
    assert eng.base == 1000
    assert "期末强平" in recs[-1].note


def test_through_fill_requires_penetration():
    eng = _engine(fill_mode="through")
    bars = [{"open": 10.00, "high": 10.10, "low": 10.00, "close": 10.10}]
    assert eng.run_day(bars, 10.00, "2026-11-20").note == "未触发"
    eng2 = _engine(fill_mode="through")
    bars2 = [{"open": 10.00, "high": 10.11, "low": 10.00, "close": 10.10}]
    assert eng2.run_day(bars2, 10.00, "2026-11-20").sell_px == 10.10


def test_auto_trigger_scales_with_atr():
    from quantlab.engine.t0 import atr_pct_series
    days = [{"date": str(i), "open": 10.0, "high": 10.2, "low": 9.8,
             "close": 10.0, "pre_close": 10.0} for i in range(25)]
    a = atr_pct_series(days, 20)
    assert a[0] is None and a[-1] is not None
    assert a[-1] == pytest.approx(0.04, rel=0.2)
