from __future__ import annotations

import numpy as np
import pandas as pd

from ashare_quant.engine import indicators


def test_sma_and_roc():
    series = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert indicators.sma(series, 3).iloc[-1] == 4
    assert indicators.roc(series, 2).iloc[-1] == 0.6666666666666667


def test_rsi_bounds():
    series = pd.Series(np.linspace(10, 20, 50))
    value = indicators.rsi(series, 14).dropna()
    assert not value.empty
    assert ((value >= 0) & (value <= 100)).all()


def test_max_drawdown():
    equity = pd.Series([100, 120, 60, 90])
    assert indicators.max_drawdown(equity) == -0.5


def test_atr_positive(panel):
    value = indicators.atr(panel["high"]["600000"], panel["low"]["600000"],
                           panel["close"]["600000"], 14).dropna()
    assert (value > 0).all()


def test_compute_all(panel):
    frame = indicators.compute_all(pd.DataFrame({
        "open": panel["open"]["600000"],
        "high": panel["high"]["600000"],
        "low": panel["low"]["600000"],
        "close": panel["close"]["600000"],
        "volume": panel["volume"]["600000"],
    }))
    for column in ("ma20", "rsi14", "macd_hist", "atr_pct", "boll_width", "vol_ratio"):
        assert column in frame.columns
