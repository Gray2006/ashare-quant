# -*- coding: utf-8 -*-
"""IC 正比例门槛的测试。

背景(2026-11-28):
  lowvol20 的全样本 5 日 IC = +0.107,是权重最大的因子(+0.21),
  但滚动 60 天窗口正比例只有 47% —— 一半时间是反的。

  均值高但经常反向的因子是噪音,不是信号。加门槛排除。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantlab.engine import pick_strategy as ps  # noqa: E402


def _panel(n_days: int = 200, n_cols: int = 40, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=n_days, freq="B")
    cols = [f"S{i:03d}" for i in range(n_cols)]
    return pd.DataFrame(rng.random((n_days, n_cols)), index=idx, columns=cols)


def test_hit_ratio_exists() -> None:
    assert hasattr(ps, "_ic_hit_ratio")


def test_hit_ratio_returns_one_when_short() -> None:
    """样本不足时不拦 —— 避免误杀新因子。"""
    fac = _panel(50)
    fwd = _panel(50)
    assert ps._ic_hit_ratio(fac, fwd, 5) == 1.0


def test_hit_ratio_between_zero_and_one() -> None:
    fac = _panel(300)
    fwd = _panel(300)
    r = ps._ic_hit_ratio(fac, fwd, 5)
    assert 0.0 <= r <= 1.0


def test_random_factor_low_hit_ratio() -> None:
    """纯随机因子应该接近 50%,不该接近 100%。"""
    fac = _panel(400, seed=1)
    fwd = _panel(400, seed=2)
    r = ps._ic_hit_ratio(fac, fwd, 5)
    assert r < 0.95, f"随机因子的正比例 {r:.0%} 太高,说明算错了"


def test_profiles_have_hit_floor() -> None:
    """两个档位都要配 ic_hit_floor,否则门槛形同虚设。"""
    for name in ("steady", "attack"):
        pf = ps.PROFILES[name]
        assert "ic_hit_floor" in pf, f"{name} 没配 ic_hit_floor"
        assert 0.0 < float(pf["ic_hit_floor"]) <= 1.0


def test_scan_reports_dropped() -> None:
    """被门槛排除的因子要能报出来,不能静默丢弃。"""
    src = (ROOT / "quantlab/engine/pick_strategy.py").read_text(
        encoding="utf-8")
    assert "dropped" in src
    assert "正比例" in src
