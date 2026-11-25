# -*- coding: utf-8 -*-
"""HRP 自检:权重和、低相关资产权重更高、退化输入能兜住。"""
from __future__ import annotations

import numpy as np

from quantlab.engine import hrp


def test_weights_sum_to_one():
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.02, (200, 8))
    w = hrp.hrp_weights(r)
    assert len(w) == 8
    assert abs(w.sum() - 1.0) < 1e-9
    assert (w >= -1e-12).all()


def test_hrp_prefers_low_vol_assets():
    """两组资产:一组高波动,一组低波动。低波动那组应该拿到更多权重。"""
    rng = np.random.default_rng(2)
    r = np.zeros((300, 6))
    r[:, :3] = rng.normal(0, 0.05, (300, 3))      # 高波动
    r[:, 3:] = rng.normal(0, 0.01, (300, 3))      # 低波动
    w = hrp.hrp_weights(r)
    assert w[3:].sum() > w[:3].sum()


def test_corr_to_dist_diagonal_zero():
    c = np.array([[1.0, 0.5], [0.5, 1.0]])
    d = hrp.corr_to_dist(c)
    assert d[0, 0] == 0.0
    assert d[0, 1] > 0


def test_degenerate_inputs_return_equal():
    assert hrp.hrp_weights(np.zeros((10, 1))).sum() == 1.0
    w = hrp.hrp_weights(np.zeros((100, 4)))
    assert abs(w.sum() - 1.0) < 1e-9
    assert np.allclose(w, 0.25)


def test_constant_column_handled():
    rng = np.random.default_rng(3)
    r = rng.normal(0, 0.02, (200, 5))
    r[:, 2] = 0.0                                  # 一只票完全不动
    w = hrp.hrp_weights(r)
    assert abs(w.sum() - 1.0) < 1e-9
    assert w[2] == 0.0
