# -*- coding: utf-8 -*-
"""概念漂移检测自检。"""
from __future__ import annotations

import numpy as np

from quantlab.engine import drift


def _stable(n=120, seed=1):
    rng = np.random.default_rng(seed)
    return list(rng.normal(0.03, 0.02, n // 2)) + list(rng.normal(0.02, 0.02, n // 2))


def _shifted(n=120, seed=1):
    rng = np.random.default_rng(seed)
    return list(rng.normal(0.04, 0.02, n // 2)) + list(rng.normal(-0.03, 0.02, n // 2))


def test_stable_series_no_drift():
    r = drift.drift_report(_stable())
    assert r["sign_flips"] == 0
    assert r["cusum_break"] == -1
    assert r["segments"]["all_same"] is True


def test_shifted_series_detected():
    r = drift.drift_report(_shifted())
    assert r["sign_flips"] >= 1
    assert r["cusum_break"] >= 0
    assert r["segments"]["all_same"] is False


def test_sign_flips_counts_crossings():
    ic = [0.05] * 50 + [-0.05] * 50 + [0.05] * 50
    assert drift.sign_flips(ic, window=10) >= 1


def test_cusum_needs_enough_data():
    assert drift.cusum_break([0.1] * 10) == -1


def test_segment_stability_too_short():
    r = drift.segment_stability([0.1] * 20, n_seg=3)
    assert r["ok"] is False


def test_rolling_ic_length():
    r = drift.rolling_ic(list(np.linspace(0.01, 0.05, 100)), window=20)
    assert len(r) == 100 - 20 + 1
