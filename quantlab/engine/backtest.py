# -*- coding: utf-8 -*-
"""A 股日频回测引擎。

刻意保守的假设(宁可比实盘差,不要好看):
  * t 日收盘出信号 → t+1 开盘成交(信号不能穿越)
  * T+1:当日买入的票当天不可卖
  * 涨停不可买、跌停不可卖(用开盘价是否触及涨跌停价判定)
  * 100 股整手、佣金最低 5 元、卖出印花税、双边过户费、按比例滑点
  * 每日盯市计算净值(同时保留「按平仓归集」的另一套口径,见 audit)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data import kline as kl
from ..utils import limit_pct, normalize_code
from . import exchange, indicators as ind
from .models import Trade
from .strategy import BaseStrategy


@dataclass
class CostModel:
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001
    slippage: float = 0.001
    # 冲击成本:平方根模型,单笔占当日成交额比例越大,价格越差。
    # 0.1 表示占成交额 1% 时约多付 0.1% 的价格。
    impact_coef: float = 0.1

    def buy_fee(self, amount: float) -> float:
        return max(amount * self.commission, self.min_commission) + amount * self.transfer_fee

    def sell_fee(self, amount: float) -> float:
        return (max(amount * self.commission, self.min_commission)
                + amount * self.transfer_fee + amount * self.stamp_tax)


@dataclass
class BTParams:
    start: str = "2023-01-01"
    end: str = ""
    initial_cash: float = 1_000_000.0
    max_positions: int = 10
    per_position_pct: float = 0.09
    cost: CostModel = field(default_factory=CostModel)
    benchmark: str = "000300"
    # 单笔成交额不超过当日成交额的比例。0.1 = 10%,超过部分按比例缩量。
    max_volume_pct: float = 0.1


@dataclass
class BTResult:
    equity: pd.DataFrame
    trades: list[Trade]
    params: dict
    logs: list[str] = field(default_factory=list)

    def trade_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.to_dict() for t in self.trades])


def load_universe_data(codes: list[str], start: str, end: str, count: int = 400,
                       progress=None) -> dict[str, pd.DataFrame]:
    """批量拉日线(带 SQLite 缓存),返回 {code: df} 且统一到指标列齐备。"""
    from ..data.store import store
    st = store()
    out = {}
    for i, item in enumerate(codes):
        # universe.build 返回的是 [{code, name, ...}],这里两种都吃
        raw = item.get("code") if isinstance(item, dict) else item
        code = normalize_code(str(raw or ""))
        if not code:
            continue
        try:
            bars, _src = kl.get_kline(code, 101, count=count)
        except Exception:
            bars = []
        if not bars:
            bars = st.load_kline(code, 101, 1, count)
        if not bars:
            continue
        df = ind.bars_to_df(bars)
        df = df[(df["datetime"] >= pd.Timestamp(start)) & (df["datetime"] <= pd.Timestamp(end or "2100-01-01"))]
        if len(df) < 30:
            continue
        out[code] = df.reset_index(drop=True)
        if i % 20 == 0:
            try:
                st.save_kline(code, 101, 1, bars)
            except Exception:
                pass
        if progress:
            progress(i + 1, len(codes))
    return out


def load_benchmark(code: str, start: str, end: str) -> pd.DataFrame:
    try:
        bars, _ = kl.get_kline(code, 101, count=1200, is_index=True)
    except Exception:
        bars = []
    df = ind.bars_to_df(bars)
    if df.empty:
        return df
    return df[(df["datetime"] >= pd.Timestamp(start)) &
              (df["datetime"] <= pd.Timestamp(end or "2100-01-01"))].reset_index(drop=True)


def _limit_price(pre_close: float, pct: float, up: bool) -> float:
    raw = pre_close * (1 + pct) if up else pre_close * (1 - pct)
    return round(raw + 1e-9, 2)


def _amt_at(amt_df: pd.DataFrame, d, code: str) -> float:
    """当日成交额。缺失/停牌/None 一律当 0,别让 float(None) 把回测打断。"""
    try:
        if d in amt_df.index and code in amt_df.columns:
            v = amt_df.at[d, code]
            if pd.notna(v):
                return float(v)
    except (TypeError, ValueError):
        pass
    return 0.0


class Backtester:
    def __init__(self, strategy: BaseStrategy, params: BTParams, data: dict[str, pd.DataFrame],
                 names: dict[str, str] | None = None):
        self.st = strategy
        self.p = params
        self.data = data
        self.names = names or {}

    # ------------------------------------------------------------ 主循环
    def run(self, progress=None) -> BTResult:
        p, cost = self.p, self.p.cost
        data = self.data
        if not data:
            raise ValueError("没有可回测的数据(先检查代码池/日期范围)")

        scores = self.st.factor_scores(data)
        if scores is None or scores.empty:
            raise ValueError("策略没有产出因子分数(数据太短或全被过滤)")
        # 目标持仓:策略内部仍是因果的;这里统一延后一天生效
        targets = self.st.select(scores)
        dates = sorted(set().union(*[set(df["datetime"]) for df in data.values()]))
        dates = [d for d in dates if d >= pd.Timestamp(p.start)]
        if p.end:
            dates = [d for d in dates if d <= pd.Timestamp(p.end)]
        if not dates:
            raise ValueError("日期区间内无交易日")

        # 预取每日开/收盘/前收/成交额
        open_m, close_m, preclose_m, amt_m, high_m, low_m = {}, {}, {}, {}, {}, {}
        for code, df in data.items():
            dd = df.set_index("datetime")
            dd = dd[~dd.index.duplicated(keep="last")]
            open_m[code] = dd["open"]
            close_m[code] = dd["close"]
            high_m[code] = dd["high"]
            low_m[code] = dd["low"]
            preclose_m[code] = dd["close"].shift(1)
            amt_m[code] = dd["amount"]
        amt_df = pd.DataFrame(amt_m).reindex(dates)
        open_df = pd.DataFrame(open_m).reindex(dates)
        close_df = pd.DataFrame(close_m).reindex(dates)
        pre_df = pd.DataFrame(preclose_m).reindex(dates)
        high_df = pd.DataFrame(high_m).reindex(dates)
        low_df = pd.DataFrame(low_m).reindex(dates)

        cash = float(p.initial_cash)
        pos: dict[str, dict] = {}          # code -> {qty, avail, cost, open_date, open_i}
        trades: list[Trade] = []
        equity_rows = []
        pending: dict[str, float] = {}     # code -> target weight(来自上一日收盘)
        logs: list[str] = []
        rebalance = int(self.st.params.get("rebalance", 1) or 1)
        min_hold = int(self.st.params.get("min_hold", 0) or 0)
        max_pos = int(p.max_positions)

        for i, d in enumerate(dates):
            # ---------- 开盘:执行昨日收盘信号 ----------
            if pending and i > 0:
                self._execute_open(i, d, pending, pos, cash, open_df, pre_df, amt_df,
                                   cost, trades, max_pos)
                cash = self._cash  # _execute_open 写回
            # ---------- 收盘:盯市 + 生成明日信号 ----------
            mv = 0.0
            for code, ps in pos.items():
                px = close_df.at[d, code] if code in close_df.columns else np.nan
                if pd.notna(px):
                    ps["mark"] = float(px)
                    ps["avail"] = ps["qty"]          # 隔日可卖(T+1)
                mv += ps.get("mark", ps["cost"]) * ps["qty"]
            total = cash + mv
            equity_rows.append({"date": d, "equity": total, "cash": cash, "market_value": mv})

            # 调仓信号:每 rebalance 天计算一次;只用 d 及之前的数据
            if i % rebalance == 0 and i < len(dates) - 1:
                self._equity_ref = total
                sel = targets.loc[targets.index <= d].iloc[-1] if len(targets.index) else None
                picked = [c for c, v in (sel.items() if sel is not None else []) if v == 1]
                pending = self._plan(picked, pos, cash + mv, max_pos, min_hold, i, d)
            if progress and i % 20 == 0:
                progress(i, len(dates))

        # 收尾:最后一根 K 线强制平仓(口径记得单列,EP007 的教训)
        if pos:
            last = dates[-1]
            for code in list(pos):
                px = close_df.at[last, code] if code in close_df.columns else np.nan
                if pd.notna(px):
                    self._close_position(code, float(px), str(last.date()), pos, cash,
                                         trades, cost, "回测结束强平")
            cash = self._cash
            eq = equity_rows[-1]
            eq["cash"] = cash
            eq["market_value"] = 0.0
            eq["equity"] = cash

        eq_df = pd.DataFrame(equity_rows)
        eq_df["ret"] = eq_df["equity"].pct_change().fillna(0.0)
        logs.append(f"回测 {len(dates)} 个交易日,成交 {len(trades)} 笔,"
                    f"期末净值 {eq_df['equity'].iloc[-1]:,.0f}")
        return BTResult(equity=eq_df, trades=trades,
                        params={"strategy": self.st.name, **self.st.params,
                                "start": p.start, "end": p.end,
                                "initial_cash": p.initial_cash,
                                "max_positions": p.max_positions,
                                "per_position_pct": p.per_position_pct},
                        logs=logs)

    # ------------------------------------------------------------ 执行细节
    def _plan(self, picked: list[str], pos: dict, total_value: float, max_pos: int,
              min_hold: int, i: int, d) -> dict[str, float]:
        """算出目标持仓集合(其余清仓)。"""
        hold = {}
        for code, ps in pos.items():
            held_days = i - ps.get("open_i", i)
            if code in picked or held_days < min_hold:
                hold[code] = 1.0
            else:
                hold[code] = 0.0
        slots = max(0, max_pos - sum(1 for c, w in hold.items() if w > 0 and c in pos))
        adds = [c for c in picked if c not in hold][:slots]
        for c in adds:
            hold[c] = 1.0
        return hold

    def _execute_open(self, i: int, d, pending: dict[str, float], pos: dict, cash: float,
                      open_df: pd.DataFrame, pre_df: pd.DataFrame, amt_df: pd.DataFrame,
                      cost: CostModel, trades: list[Trade], max_pos: int) -> None:
        self._cash = cash
        rows = open_df.loc[d] if d in open_df.index else None
        pre = pre_df.loc[d] if d in pre_df.index else None
        if rows is None or pre is None:
            return
        p = self.p

        # 先卖(释放资金)
        for code in list(pos):
            if pending.get(code, 0) == 0:
                px = rows.get(code, np.nan)
                if pd.isna(px) or px <= 0:
                    continue
                pc = pre.get(code, np.nan)
                if pd.notna(pc) and px <= _limit_price(float(pc), limit_pct(code), up=False) + 1e-6:
                    continue  # 跌停卖不出
                amt = _amt_at(amt_df, d, code)
                qty = pos[code]["qty"]
                impact = cost.impact_coef * ((qty * px / amt) ** 0.5) if amt > 0 else 0.0
                self._close_position(code, float(px) * (1 - cost.slippage - impact),
                                     str(d.date()), pos, self._cash, trades, cost, "信号卖出")
        # 再买
        want = [c for c, w in pending.items() if w > 0 and c not in pos]
        slots = max(0, max_pos - len(pos))
        want = want[:slots]
        if not want:
            return
        budget = min(float(p.per_position_pct) * float(getattr(self, "_equity_ref", self._cash)),
                     self._cash * 0.98)
        for code in want:
            px = rows.get(code, np.nan)
            pc = pre.get(code, np.nan)
            if pd.isna(px) or px <= 0:
                continue
            if pd.notna(pc) and px >= _limit_price(float(pc), limit_pct(code), up=True) - 1e-6:
                continue  # 涨停买不到
            amt = _amt_at(amt_df, d, code)
            cash_available = min(budget, self._cash * 0.98)
            # 按板块申报单位取整:主板/创业板 100 股整数倍,科创板 200 股起
            base_qty = exchange.lot_of(code)[0]
            lot = exchange.round_qty(code, int(cash_available / px))
            if lot < base_qty:
                continue
            # 成交量限制:单笔不超过当日成交额的 max_volume_pct,超过按比例缩量
            if amt > 0:
                max_lot = int(amt * p.max_volume_pct / px / base_qty) * base_qty
                lot = min(lot, max_lot)
                if lot < base_qty:
                    continue
            # 冲击成本:平方根模型,占成交额比例越大价格越差
            impact = cost.impact_coef * ((lot * px / amt) ** 0.5) if amt > 0 else 0.0
            exec_px = float(px) * (1 + cost.slippage + impact)
            amount = exec_px * lot
            fee = cost.buy_fee(amount)
            if amount + fee > self._cash:
                lot -= base_qty
                if lot < base_qty:
                    continue
                amount = exec_px * lot
                fee = cost.buy_fee(amount)
            self._cash -= amount + fee
            pos[code] = {"qty": lot, "avail": 0, "cost": exec_px + fee / lot,
                         "open_date": str(d.date()), "open_i": i, "mark": exec_px}

    def _close_position(self, code: str, px: float, date_str: str, pos: dict, cash: float,
                        trades: list[Trade], cost: CostModel, reason: str) -> None:
        ps = pos.get(code)
        if not ps:
            return
        qty = ps["qty"]
        if qty <= 0:
            pos.pop(code, None)
            return
        amount = px * qty
        fee = cost.sell_fee(amount)
        self._cash = self._cash + amount - fee
        profit = (px - ps["cost"]) * qty - fee
        trades.append(Trade(
            code=code, name=self.names.get(code, ""), open_date=ps.get("open_date", ""),
            close_date=date_str, open_price=round(ps["cost"], 3),
            close_price=round(px, 3), qty=qty, fee=round(fee, 2),
            profit=round(profit, 2),
            profit_pct=round((px / ps["cost"] - 1) * 100, 3) if ps["cost"] else 0.0,
            reason=reason))
        pos.pop(code, None)
