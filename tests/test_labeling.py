# -*- coding: utf-8 -*-
"""三重障碍标注自检。"""
from __future__ import annotations

import numpy as np

from quantlab.engine import labeling as lb


def test_hits_take_profit_first():
    # 入场 10,止盈 10.5,止损 9.7;第 1 根就冲到 10.6
    lab, ret = lb.triple_barrier([10.6, 10.0], [10.0, 9.9], [10.5, 10.0],
                                 10.0, 0.05, 0.03, 5)
    assert lab == 1
    assert abs(ret - 0.05) < 1e-9


def test_hits_stop_loss_first():
    lab, ret = lb.triple_barrier([10.1, 10.0], [9.6, 9.9], [9.7, 9.9],
                                 10.0, 0.05, 0.03, 5)
    assert lab == -1
    assert abs(ret + 0.03) < 1e-9


def test_time_expiry():
    lab, ret = lb.triple_barrier([10.1, 10.2], [9.9, 10.0], [10.05, 10.1],
                                 10.0, 0.05, 0.03, 2)
    assert lab == 0
    assert abs(ret - 0.01) < 1e-9


def test_same_bar_both_hit_is_conservative():
    """同一根里上下都碰到,算止损(实盘没法保证先成交哪个)。"""
    lab, _ = lb.triple_barrier([10.8], [9.5], [10.0], 10.0, 0.05, 0.03, 1)
    assert lab == -1


def test_label_panel_shape_and_values():
    n, k = 30, 5
    rng = np.random.default_rng(3)
    c = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, (n, k)), axis=0))
    panel = {"close": __import__("pandas").DataFrame(c),
             "high": __import__("pandas").DataFrame(c * 1.02),
             "low": __import__("pandas").DataFrame(c * 0.98)}
    out = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    assert out["label"].shape == (n, k)
    assert set(np.unique(out["label"])) <= {-1.0, 0.0, 1.0}
    # 最后 horizon 行没有完整未来,标签应为 0
    assert (out["label"][-1] == 0).all()
