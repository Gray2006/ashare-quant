# -*- coding: utf-8 -*-
"""页面默认标的与游资数据接口的回归测试。

锁两件事:
  1. 页面默认标的一律走自选,不许再写死 600519;
  2. 游资那套「采集 → 预测 → 验证」的接口在空库/有库时都不炸。
"""
from __future__ import annotations

from quantlab.ui.common import default_code, watch_codes


def test_watch_codes_returns_clean_six_digit_codes():
    codes = watch_codes()
    assert isinstance(codes, list)
    for c in codes:
        assert isinstance(c, str) and len(c) == 6 and c.isdigit()


def test_watch_codes_limit_and_dedup():
    codes = watch_codes()
    assert len(watch_codes(2)) <= 2
    assert len(codes) == len(set(codes))


def test_default_code_is_empty_without_fallback():
    """没有自选时不能偷偷塞一只股票 —— 页面要自己提示去加自选。"""
    assert default_code("") in ("", *watch_codes(1))
    assert default_code("999999") == (watch_codes(1) or ["999999"])[0]


def test_hotmoney_readonly_interfaces_do_not_raise():
    from quantlab.engine import hotmoney as hm

    cov = hm.coverage(60)
    assert set(cov) >= {"n", "lo", "hi", "latest"}
    assert isinstance(cov["n"], int)

    acc = hm.accuracy(60)
    assert set(acc) >= {"n", "ok", "acc", "n_dir", "ok_dir", "acc_dir"}
    assert 0.0 <= acc["acc"] <= 1.0

    preds = hm.predict_day("")        # 空日期 → 自动取最新一天
    assert isinstance(preds, list)
    for p in preds:
        assert p["direction"] in ("up", "down", "flat")
        assert isinstance(p["score"], float)
