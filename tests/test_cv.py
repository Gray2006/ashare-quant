# -*- coding: utf-8 -*-
"""cv.py 自检:purge 确实剔掉了重叠样本,walk-forward 不看未来。"""
from __future__ import annotations

from quantlab.engine import cv


def test_purged_kfold_basic_shape():
    folds = cv.purged_kfold(100, n_splits=5, horizon=5)
    assert len(folds) == 5
    for tr, te in folds:
        assert tr and te
        assert not (set(tr) & set(te)), "训练/测试不能重叠"


def test_purge_removes_overlapping_samples():
    """horizon=5 时,测试段前 5 个样本必须被剔出训练集。"""
    folds = cv.purged_kfold(100, n_splits=5, horizon=5)
    _, te = folds[2]                      # 第 3 折:索引 40..59
    lo = te[0]
    tr, _ = folds[2]
    leaked = [i for i in tr if i + 5 >= lo and i < lo]
    assert leaked == [], f"这些样本的标签窗口压到测试集:{leaked}"


def test_embargo_pushes_tail():
    a = cv.purged_kfold(100, n_splits=5, horizon=5, embargo=0)
    b = cv.purged_kfold(100, n_splits=5, horizon=5, embargo=10)
    # embargo 越大,训练集越小
    assert sum(len(tr) for tr, _ in b) < sum(len(tr) for tr, _ in a)


def test_walk_forward_never_looks_ahead():
    for tr, te in cv.walk_forward_splits(200, n_splits=4, horizon=5, min_train=40):
        assert max(tr) + 5 < min(te), "训练集的标签窗口压到了测试集"


def test_stability_flags_unstable():
    up = [0.01] * 100
    mixed = [0.02] * 40 + [-0.02] * 60
    s1 = cv.stability(up, horizon=5)
    s2 = cv.stability(mixed, horizon=5)
    assert s1["all_positive"] is True
    assert s2["positive_folds"] < s2["n_folds"]
    assert "不稳" in s2["verdict"]


def test_empty_inputs_are_safe():
    assert cv.purged_kfold(0) == []
    assert cv.purged_kfold(10, n_splits=1) == []
    assert cv.walk_forward_splits(10, min_train=40) == []
