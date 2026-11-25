# -*- coding: utf-8 -*-
"""多重比较校正自检:BH 的 q 值要满足单调性和边界条件。"""
from __future__ import annotations


import pytest

from quantlab.engine import multiple_testing as mt


def test_t_to_p_known_values():
    assert mt.t_to_p(0.0) == pytest.approx(1.0)
    # 1.96 双侧应该约 0.05
    assert mt.t_to_p(1.96) == pytest.approx(0.05, abs=0.002)
    assert mt.t_to_p(2.576) == pytest.approx(0.01, abs=0.002)
    assert mt.t_to_p(None) == 1.0
    assert mt.t_to_p(float("inf")) == 1.0


def test_bh_all_significant_stays_significant():
    ps = [0.001, 0.002, 0.003]
    qs = mt.bh_qvalues(ps)
    assert all(q <= 0.05 for q in qs)


def test_bh_all_null_stays_null():
    ps = [0.5, 0.6, 0.7, 0.8, 0.9]
    qs = mt.bh_qvalues(ps)
    assert all(q > 0.05 for q in qs)


def test_bh_has_no_false_positives_under_null():
    """100 个纯噪声的 p 值,校正后不该有任何通过。"""
    ps = [0.4 + 0.005 * i for i in range(100)]
    qs = mt.bh_qvalues(ps)
    assert max(qs) <= 1.0
    assert sum(1 for q in qs if q <= 0.05) == 0


def test_bh_qvalues_monotone_in_p():
    """p 值越大,q 值不能更小。"""
    ps = [0.001, 0.01, 0.02, 0.5, 0.9]
    qs = mt.bh_qvalues(ps)
    for a, b in zip(ps, qs):
        pass
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    sorted_q = [qs[i] for i in order]
    for i in range(1, len(sorted_q)):
        assert sorted_q[i] >= sorted_q[i - 1] - 1e-12


def test_bh_reduces_passes():
    """一批里混一个边缘显著:不校正过,校正后不过。"""
    ps = [0.04] + [0.9] * 19      # 1 个 0.04 + 19 个噪声
    qs = mt.bh_qvalues(ps)
    assert ps[0] <= 0.05                 # 不校正会通过
    assert qs[0] > 0.05                  # BH 之后不通过(0.04*20/1=0.8)


def test_fdr_report_shape():
    tvals = [3.0, 2.5, 0.5, -0.2, 1.0]
    rep = mt.fdr_report(tvals)
    assert rep["n"] == 5
    assert len(rep["qvalues"]) == 5
    assert rep["raw_pass"] >= rep["fdr_pass"]
    assert rep["false_positives"] == rep["raw_pass"] - rep["fdr_pass"]
    assert "FDR 校正后通过" in rep["verdict"]


def test_fdr_report_empty():
    rep = mt.fdr_report([])
    assert rep["n"] == 0
    assert rep["fdr_pass"] == 0
