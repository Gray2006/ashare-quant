from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_quant.engine import screener


@pytest.fixture()
def frame():
    return pd.DataFrame({
        "code": ["600000", "000001", "300750"],
        "name": ["浦发银行", "平安银行", "宁德时代"],
        "close": [10.0, 12.0, 200.0],
        "pct_chg": [1.2, -0.8, 3.5],
        "turnover": [2.0, 1.0, 4.0],
        "amount": [1e8, 2e8, 5e9],
    })


def test_basic_filter(frame):
    result = screener.screen(frame, "pct_chg > 1 and between(turnover, 1, 5)")
    assert list(result["code"]) == ["600000", "300750"]


def test_rank_and_abs(frame):
    result = screener.screen(frame, "rank(close) >= 0.5 and abs(pct_chg) > 0.5")
    assert len(result) == 2


def test_missing_field(frame):
    with pytest.raises(screener.ScreenError, match="未知字段"):
        screener.screen(frame, "not_a_field > 1")


def test_rejects_code_execution(frame):
    for expr in (
        "__import__('os').system('echo hi')",
        "close.__class__",
        "close[0]",
        "lambda x: x",
        "open('x')",
    ):
        with pytest.raises(screener.ScreenError):
            screener.screen(frame, expr)


def test_rejects_bad_syntax(frame):
    with pytest.raises(screener.ScreenError):
        screener.screen(frame, "close >")


def test_limit_and_sort(frame):
    result = screener.screen(frame, "close > 0", limit=2)
    assert len(result) == 2
