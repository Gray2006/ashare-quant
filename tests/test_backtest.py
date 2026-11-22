from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ashare_quant.engine import backtest


def test_scores_shape(panel):
    scores = backtest.factor_scores(panel, "momentum_20")
    assert scores.shape == panel["close"].shape
    assert scores.index.equals(panel["close"].index)


def test_backtest_runs_without_lookahead(panel):
    scores = backtest.factor_scores(panel, "momentum_20")
    cfg = backtest.BacktestConfig(
        top_n=2, rebalance="M", start=str(panel["close"].index[60].date()),
        end=str(panel["close"].index[-1].date()), min_amount=0, min_price=0,
        slippage_bps=0.0,
    )
    result = backtest.run_backtest(panel, scores, cfg)
    assert not result.equity.empty
    assert result.equity.iloc[0] > 0
    assert result.metrics["total_return"] == pytest.approx(
        result.equity.iloc[-1] / cfg.initial_capital - 1)
    # 第一笔成交只能发生在信号日之后
    assert result.trades
    first_trade = min(t["date"] for t in result.trades)
    assert pd.Timestamp(first_trade) > panel["close"].index[60]


def test_no_signal_stays_cash(panel):
    scores = pd.DataFrame(np.nan, index=panel["close"].index, columns=panel["close"].columns)
    cfg = backtest.BacktestConfig(
        top_n=2, start=str(panel["close"].index[0].date()),
        end=str(panel["close"].index[-1].date()), min_amount=0, min_price=0,
    )
    result = backtest.run_backtest(panel, scores, cfg)
    assert result.equity.iloc[-1] == cfg.initial_capital
    assert result.metrics["total_return"] == 0


def test_benchmark_excess(panel):
    scores = backtest.factor_scores(panel, "momentum_20")
    bench = panel["close"]["600000"]
    cfg = backtest.BacktestConfig(
        top_n=1, start=str(panel["close"].index[60].date()),
        end=str(panel["close"].index[-1].date()), min_amount=0, min_price=0,
        slippage_bps=0.0,
    )
    result = backtest.run_backtest(panel, scores, cfg, benchmark=bench)
    assert result.benchmark is not None
    assert "excess_return" in result.metrics
