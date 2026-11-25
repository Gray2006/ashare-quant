# -*- coding: utf-8 -*-
"""分数阶差分自检。"""
from __future__ import annotations

import numpy as np

from quantlab.engine import fracdiff as fd


def test_weights_d0_is_identity():
    w = fd.frac_weights(0.0)
    assert abs(w[0] - 1.0) < 1e-12
    # d=0 时二阶权重就是 0,按阈值截断后只剩一项
    assert len(w) == 1


def test_weights_d1_is_first_difference():
    w = fd.frac_weights(1.0)
    assert abs(w[0] - 1.0) < 1e-12
    assert abs(w[1] + 1.0) < 1e-12
    # d=1 时三阶以后权重应该很小
    assert len(w) <= 4


def test_frac_diff_d1_matches_diff():
    x = np.array([1.0, 3.0, 6.0, 10.0, 15.0, 21.0, 28.0])
    y = fd.frac_diff(x, 1.0)
    assert np.isnan(y[0])
    assert abs(y[1] - 2.0) < 1e-9      # 3-1
    assert abs(y[2] - 3.0) < 1e-9      # 6-3


def test_frac_diff_keeps_memory():
    """d=0.4 时,权重不只两项 —— 说明保留了更远的记忆。"""
    w = fd.frac_weights(0.4, threshold=1e-6)
    assert len(w) > 3
    # 权重符号交替衰减
    assert abs(w[1]) > abs(w[2]) > abs(w[3])


def test_frac_diff_short_series_safe():
    y = fd.frac_diff([1.0, 2.0, 3.0], 0.4)
    assert len(y) == 3
    assert np.isnan(y).all()


def test_best_d_on_trend():
    """带趋势的序列,需要 d 接近 1 才平稳。"""
    rng = np.random.default_rng(5)
    x = np.cumsum(rng.normal(0, 1, 300)) + 10
    d = fd.best_d(x)
    assert 0.1 <= d <= 1.0
