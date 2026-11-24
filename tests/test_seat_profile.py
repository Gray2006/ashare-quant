# -*- coding: utf-8 -*-
"""席位画像:特征计算的纯函数 + 真实库上的结构完整性。"""
from __future__ import annotations

import pytest

from quantlab.engine import seat_profile as sp


def _bars(closes: list[float], opens: list[float] | None = None) -> list[dict]:
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        out.append({"datetime": f"2026-01-{i + 1:02d}", "open": o,
                    "high": max(o, c) * 1.02, "low": min(o, c) * 0.98,
                    "close": c, "volume": 1000 + i * 10})
    return out


def test_pct_and_range_pos():
    bars = _bars([10.0, 11.0, 12.0])
    assert sp._pct(bars, 1) == pytest.approx(0.1)
    assert sp._pct(bars, 0) is None
    # 只有 3 根,不足 20 根 → 位置给 None
    assert sp._range_pos(bars, 2) is None


def test_limit_streak_counts_consecutive_limits():
    bars = _bars([10.0, 11.0, 12.1, 13.31, 13.5])
    assert sp._limit_streak(bars, 3) == 3      # 11/10、12.1/11、13.31/12.1 都 ≥9.5%
    assert sp._limit_streak(bars, 4) == 0


def test_vol_ratio_and_amp():
    bars = _bars([10.0] * 30)
    bars[-1]["volume"] = 3000
    r = sp._vol_ratio(bars, len(bars) - 1, window=20)
    assert r is not None and r > 1.5
    a = sp._amp(bars, 5)
    assert a is not None and a > 0


def test_entry_exit_uses_next_open():
    """买入基准必须是 T+1 开盘价,不是 T 日收盘。"""
    bars = _bars([10.0, 10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0],
                 opens=[10.0, 12.0, 10.4, 10.9, 11.4, 11.9, 12.4, 12.9])
    ret, entry, _ = sp._entry_exit(bars, 1, 5)   # T=索引1,次日开盘 10.4 买
    assert entry == pytest.approx(10.4)
    assert ret == pytest.approx(13.0 / 10.4 - 1)  # 索引 1+1+5=7 收盘 13.0


def test_market_bench_handles_empty_days():
    out = sp.market_bench(["1990-01-01"])
    assert out["1990-01-01"] is None


def test_profile_structure_on_known_seat():
    p = sp.profile("开源证券股份有限公司西安西大街证券营业部")
    if not p.get("ok"):
        pytest.skip("库里没有这个席位的记录")
    for k in ("n", "codes", "tags", "verdict", "ret5", "win5",
              "bench5", "excess5", "samples", "partners"):
        assert k in p
    assert isinstance(p["tags"], list) and p["tags"]
    assert -1.0 <= p["ret5"] <= 1.0
    md = sp.render_markdown(p)
    assert "席位画像" in md and "T+1 开盘价" in md


def test_profile_unknown_seat_is_graceful():
    p = sp.profile("不存在的营业部XYZ")
    assert p["ok"] is False and p["n"] == 0
    assert "没有记录" in sp.render_markdown(p)
