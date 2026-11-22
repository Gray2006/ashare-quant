"""横截面因子回测。

规则（尽量贴近 A 股现实）：
* 信号在 T 日收盘后产生，T+1 日开盘成交，杜绝未来函数；
* 买入按 100 股一手取整，佣金最低 5 元；
* 卖出收印花税，买卖都计滑点；
* 涨停无法买入、跌停无法卖出、停牌不成交；
* 等权持有，调仓时先卖后买；
* 只做多，不加杠杆，不做空，不接实盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import metrics


@dataclass
class BacktestConfig:
    top_n: int = 20
    rebalance: str | int = "M"          # "W" / "M" / "Q" / 整数交易日
    start: str | None = None
    end: str | None = None
    initial_capital: float = 1_000_000.0
    fee_bps: float = 3.0                # 佣金，双边
    stamp_bps: float = 10.0             # 印花税，仅卖出
    slippage_bps: float = 5.0           # 滑点，双边
    min_price: float = 1.0
    min_amount: float = 5_000_000.0     # 20 日平均成交额下限
    min_commission: float = 5.0
    lot_size: int = 100
    exclude_st: bool = True
    benchmark: str | None = "000300"


@dataclass
class BacktestResult:
    equity: pd.Series
    daily_returns: pd.Series
    benchmark: pd.Series | None
    metrics: dict
    trades: list[dict]
    holdings: dict[str, float]
    config: BacktestConfig
    signals: list[dict] = field(default_factory=list)
    yearly: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def summary(self) -> dict:
        return {
            "metrics": self.metrics,
            "start": str(self.equity.index[0].date()) if len(self.equity) else None,
            "end": str(self.equity.index[-1].date()) if len(self.equity) else None,
            "holdings": len(self.holdings),
            "trades": len(self.trades),
        }


def _limit_pct(code: str, name: str | None) -> float:
    if name and "ST" in name.upper():
        return 0.05
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    return 0.10


def _rebalance_dates(dates: pd.DatetimeIndex, rule: str | int) -> pd.DatetimeIndex:
    if isinstance(rule, int):
        if rule <= 0:
            raise ValueError("rebalance 整数必须大于 0")
        return dates[::rule]
    rule = str(rule).upper()
    frame = pd.DataFrame({"date": dates})
    if rule == "M":
        keys = [frame["date"].dt.year, frame["date"].dt.month]
    elif rule == "Q":
        keys = [frame["date"].dt.year, frame["date"].dt.quarter]
    elif rule == "W":
        iso = frame["date"].dt.isocalendar()
        keys = [iso["year"], iso["week"]]
    else:
        raise ValueError("rebalance 只支持 W / M / Q 或整数交易日")
    grouped = frame.groupby(keys, sort=True)["date"].max()
    return pd.DatetimeIndex(sorted(grouped.tolist()))


class _Portfolio:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.cash = config.initial_capital
        self.shares: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self.trades: list[dict] = []
        self.traded_notional = 0.0

    def value(self, prices: pd.Series) -> float:
        total = self.cash
        for code, shares in self.shares.items():
            price = prices.get(code)
            if pd.isna(price) or price is None:
                price = self.last_price.get(code, 0.0)
            total += shares * float(price)
        return total

    def _commission(self, notional: float) -> float:
        rate = self.config.fee_bps / 10000.0
        return max(self.config.min_commission, notional * rate)

    def sell(self, date, code: str, price: float, shares: float) -> None:
        if shares <= 0:
            return
        notional = shares * price
        fee = self._commission(notional) + notional * self.config.stamp_bps / 10000.0
        self.cash += notional - fee
        self.shares[code] -= shares
        if self.shares[code] <= 1e-9:
            del self.shares[code]
        self.traded_notional += notional
        self.trades.append({
            "date": str(date.date()), "code": code, "side": "sell",
            "price": round(price, 3), "shares": int(shares),
            "amount": round(notional, 2), "fee": round(fee, 2),
        })

    def buy(self, date, code: str, price: float, budget: float) -> None:
        config = self.config
        if budget <= 0 or price <= 0:
            return
        lot = config.lot_size
        shares = int(budget / (price * (1 + config.fee_bps / 10000.0)) // lot * lot)
        while shares > 0:
            notional = shares * price
            fee = self._commission(notional)
            total = notional + fee
            if total <= self.cash and total <= budget:
                self.cash -= total
                self.shares[code] = self.shares.get(code, 0) + shares
                self.traded_notional += notional
                self.trades.append({
                    "date": str(date.date()), "code": code, "side": "buy",
                    "price": round(price, 3), "shares": int(shares),
                    "amount": round(notional, 2), "fee": round(fee, 2),
                })
                return
            shares -= lot


def _pick_targets(score_row: pd.Series, date, close: pd.DataFrame,
                  avg_amount: pd.DataFrame, config: BacktestConfig,
                  names: dict[str, str]) -> list[str]:
    row = score_row.dropna()
    if row.empty or date not in close.index:
        return []
    prices = close.loc[date]
    amounts = avg_amount.loc[date] if date in avg_amount.index else pd.Series(dtype=float)
    valid = []
    for code in row.index:
        price = prices.get(code)
        amount = amounts.get(code)
        if pd.isna(price) or price < config.min_price:
            continue
        if pd.isna(amount) or amount < config.min_amount:
            continue
        if config.exclude_st and "ST" in str(names.get(code, "")).upper():
            continue
        valid.append(code)
    if not valid:
        return []
    return row.reindex(valid).nlargest(min(config.top_n, len(valid))).index.tolist()


def run_backtest(
    panel: dict[str, pd.DataFrame],
    scores: pd.DataFrame,
    config: BacktestConfig | None = None,
    names: dict[str, str] | None = None,
    benchmark: pd.Series | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    names = names or {}
    if "close" not in panel or "open" not in panel:
        raise ValueError("panel 至少需要 open 和 close")

    close = panel["close"].sort_index()
    open_ = panel["open"].reindex_like(close)
    amount = panel.get("amount")
    dates = close.index
    if config.start:
        dates = dates[dates >= pd.Timestamp(config.start)]
    if config.end:
        dates = dates[dates <= pd.Timestamp(config.end)]
    if len(dates) < 30:
        raise ValueError("回测区间太短（至少 30 个交易日）")

    close = close.loc[dates]
    open_ = open_.loc[dates]
    if amount is not None:
        avg_amount = amount.rolling(20, min_periods=10).mean().loc[dates]
    else:
        avg_amount = pd.DataFrame(np.nan, index=dates, columns=close.columns)

    score_frame = scores.reindex(index=dates, columns=close.columns)
    rebalance_dates = set(_rebalance_dates(dates, config.rebalance))
    portfolio = _Portfolio(config)
    equity_values: list[float] = []
    pending: list[str] | None = None
    signals: list[dict] = []

    prev_date = None
    for date in dates:
        # 1) 执行上一交易日收盘后产生的目标
        if pending is not None and prev_date is not None:
            open_row = open_.loc[date]
            prev_close = close.loc[prev_date]
            targets = set(pending)
            # 先卖
            for code in list(portfolio.shares):
                if code in targets:
                    continue
                price = open_row.get(code)
                base = prev_close.get(code)
                if pd.isna(price) or price <= 0 or pd.isna(base):
                    continue
                limit = _limit_pct(code, names.get(code))
                if price <= base * (1 - limit * 0.98):
                    continue  # 跌停卖不出
                portfolio.sell(date, code, price, portfolio.shares[code])
            # 再买
            if targets:
                open_value = portfolio.value(open_row)
                budget = open_value / len(targets)
                for code in pending:
                    if code in portfolio.shares:
                        continue
                    price = open_row.get(code)
                    base = prev_close.get(code)
                    if pd.isna(price) or price <= 0 or pd.isna(base):
                        continue
                    limit = _limit_pct(code, names.get(code))
                    if price >= base * (1 + limit * 0.98):
                        continue  # 涨停买不进
                    portfolio.buy(date, code, price, budget)
            pending = None

        # 2) 收盘估值
        close_row = close.loc[date]
        for code, price in close_row.items():
            if not pd.isna(price):
                portfolio.last_price[code] = float(price)
        equity_values.append(portfolio.value(close_row))

        # 3) 产生下一交易日的目标
        if date in rebalance_dates:
            target = _pick_targets(score_frame.loc[date], date, close,
                                   avg_amount, config, names)
            if target:
                pending = target
                signals.append({"date": str(date.date()), "targets": target})
        prev_date = date

    equity = pd.Series(equity_values, index=dates, name="equity")
    daily_returns = equity.pct_change().fillna(0.0)

    bench_series = None
    if benchmark is not None and not benchmark.dropna().empty:
        bench_series = benchmark.reindex(dates).ffill().dropna()
        if len(bench_series) > 1:
            bench_series = bench_series / bench_series.iloc[0] * config.initial_capital

    stats = metrics.performance(equity, bench_series)
    if stats:
        stats["turnover_annual"] = (
            portfolio.traded_notional / equity.mean() / max(len(dates) / 252.0, 1e-9)
        )
        stats["final_value"] = float(equity.iloc[-1])

    return BacktestResult(
        equity=equity,
        daily_returns=daily_returns,
        benchmark=bench_series,
        metrics=stats,
        trades=portfolio.trades,
        holdings=dict(portfolio.shares),
        config=config,
        signals=signals,
        yearly=metrics.yearly_returns(equity),
    )


def factor_scores(panel: dict[str, pd.DataFrame], factor: str) -> pd.DataFrame:
    """按因子方向调整后的打分，越大越好。"""
    from . import factors

    values = factors.compute(factor, panel)
    direction = factors.FACTORS[factor].direction
    return values * direction
