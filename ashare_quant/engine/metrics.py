"""绩效指标。年化按 252 个交易日，无风险利率默认 0。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _annualize_return(total: float, days: int, periods: int = 252) -> float:
    if days <= 0:
        return 0.0
    years = days / periods
    if years <= 0 or total <= -1:
        return 0.0
    return float((1.0 + total) ** (1.0 / years) - 1.0)


def performance(equity: pd.Series, benchmark: pd.Series | None = None,
                periods: int = 252) -> dict:
    equity = equity.dropna()
    if equity.empty:
        return {}
    returns = equity.pct_change().dropna()
    total = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    days = len(equity)
    annual_return = _annualize_return(total, days, periods)
    annual_vol = float(returns.std() * np.sqrt(periods)) if len(returns) > 1 else 0.0
    sharpe = float(returns.mean() / returns.std() * np.sqrt(periods)) if returns.std() else 0.0
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    max_dd = float(drawdown.min())
    calmar = annual_return / abs(max_dd) if max_dd else 0.0
    result = {
        "total_return": total,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "calmar": float(calmar),
        "win_rate": float((returns > 0).mean()) if len(returns) else 0.0,
        "trading_days": int(days),
    }
    if benchmark is not None and not benchmark.dropna().empty:
        bench = benchmark.reindex(equity.index).ffill().dropna()
        if len(bench) > 2:
            bench_returns = bench.pct_change().dropna()
            aligned = pd.concat([returns, bench_returns], axis=1, join="inner").dropna()
            if len(aligned) > 2:
                strategy_r, bench_r = aligned.iloc[:, 0], aligned.iloc[:, 1]
                beta = float(np.cov(strategy_r, bench_r)[0, 1] / np.var(bench_r)) \
                    if np.var(bench_r) else 0.0
                bench_total = float(bench.iloc[-1] / bench.iloc[0] - 1.0)
                bench_annual = _annualize_return(bench_total, len(bench), periods)
                excess = strategy_r - bench_r
                info_ratio = float(excess.mean() / excess.std() * np.sqrt(periods)) \
                    if excess.std() else 0.0
                result.update({
                    "benchmark_return": bench_total,
                    "benchmark_annual": bench_annual,
                    "excess_return": total - bench_total,
                    "beta": beta,
                    "alpha_annual": annual_return - beta * bench_annual,
                    "information_ratio": info_ratio,
                })
    return result


def yearly_returns(equity: pd.Series) -> pd.Series:
    equity = equity.dropna()
    if equity.empty:
        return pd.Series(dtype=float)
    grouped = equity.groupby(equity.index.year)
    out = {}
    for year, series in grouped:
        if len(series) < 2:
            continue
        out[int(year)] = float(series.iloc[-1] / series.iloc[0] - 1.0)
    return pd.Series(out, name="return")


def drawdown_series(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0
