from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_quant.engine import factors


def test_factor_registry_complete():
    items = factors.list_factors()
    assert len(items) >= 10
    assert {item["name"] for item in items} == set(factors.FACTORS)
    for item in items:
        assert item["direction"] in (1, -1)
        assert item["lookback"] > 0


def test_momentum_matches_close(panel):
    value = factors.compute("momentum_20", panel)
    expected = panel["close"] / panel["close"].shift(20) - 1
    pd.testing.assert_frame_equal(value, expected)


def test_direction_flips_scores(panel):
    scores = factors.compute("volatility_20", panel) * factors.FACTORS["volatility_20"].direction
    raw = factors.compute("volatility_20", panel)
    pd.testing.assert_frame_equal(scores, -raw)


def test_latest_returns_series(panel):
    series = factors.latest("momentum_60", panel)
    assert isinstance(series, pd.Series)
    assert len(series) == panel["close"].shape[1]


def test_normalize(panel):
    raw = factors.compute("momentum_20", panel)
    ranked = factors.normalize(raw, "rank")
    assert ranked.max().max() <= 1.0
    assert ranked.min().min() >= 0.0
    z = factors.normalize(raw, "zscore")
    assert abs(float(z.mean(axis=1).mean())) < 1e-9


def test_unknown_factor():
    try:
        factors.compute("nope", {})
    except KeyError as exc:
        assert "未知因子" in str(exc)
    else:
        raise AssertionError("应当抛出 KeyError")
