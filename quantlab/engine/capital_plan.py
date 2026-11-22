# -*- coding: utf-8 -*-
"""按现有资金自动定交易方案。

输入只有账户的钱,输出一套能直接执行的方案:
    买几只、每只多少钱、首笔下多少、佣金占多少、够不够买一手。

为什么单独一个模块:自动交易里「单票比例 × 权益」是个静态公式,
它不知道一手要多少钱,也不知道最低佣金。10 万本金按 18% 算单票 1.8 万,
买 200 块的票只能买 90 股(不足一手),买 5 块的票能买 3600 股 ——
同一套参数,落到不同价位的票上,可行性完全不同。这里把这些算清楚。

口径:
    可投金额 = min(现金, 权益) - 现金底线
    单票预算 = min(权益 × 单票比例, 单笔上限)
    持仓只数 = min(目标只数, 可投金额 / 单票预算, 可投金额 / 一手成本)
"""
from __future__ import annotations

import math

# 分批阶梯(与 selflearn.TranchePlanner.LADDER 对齐,强度 0.5 落在中档)
FIRST_RATIO = 0.34
TRANCHES = 3


def _fee(amount: float, rate: float, min_fee: float,
         transfer: float = 0.0, stamp: float = 0.0, sell: bool = False) -> float:
    """一笔成交的费用。买入不收印花税,卖出收。"""
    if amount <= 0:
        return 0.0
    f = max(float(min_fee), amount * float(rate))
    f += amount * float(transfer)
    if sell:
        f += amount * float(stamp)
    return f


def plan(equity: float, cash: float, per_pct: float = 0.18,
         max_order: float = 50000.0, n_hold: int = 20,
         cash_floor_pct: float = 0.10, min_lot_cost: float = 0.0,
         commission_rate: float = 0.00025, min_commission: float = 5.0,
         stamp_tax: float = 0.0005, transfer_fee: float = 0.00001) -> dict:
    """算一套方案。min_lot_cost = 最便宜那只票的一手成本(价×100),0 表示不判。"""
    equity = max(0.0, float(equity or 0))
    cash = max(0.0, float(cash or 0))
    per_pct = max(0.0, min(1.0, float(per_pct or 0)))
    max_order = max(0.0, float(max_order or 0))
    n_hold = max(1, int(n_hold or 1))
    cash_floor_pct = max(0.0, min(1.0, float(cash_floor_pct or 0)))
    min_lot_cost = max(0.0, float(min_lot_cost or 0))

    cash_floor = equity * cash_floor_pct
    investable = max(0.0, min(cash, equity) - cash_floor)
    per_position = min(equity * per_pct, max_order) if max_order > 0 else equity * per_pct

    by_budget = int(investable // per_position) if per_position > 0 else 0
    by_lot = int(investable // min_lot_cost) if min_lot_cost > 0 else n_hold
    max_positions = max(0, min(n_hold, by_budget, by_lot))

    # 资金不够时把预算抬到「一手成本」,只数相应减少 —— 宁可少买几只,
    # 也不要每只都买不到一手、整轮空转。抬到刚好够一手,不抬到满仓。
    raised = False
    if min_lot_cost > 0 and per_position < min_lot_cost and investable >= min_lot_cost:
        per_position = min_lot_cost
        raised = True
        by_budget = int(investable // per_position) if per_position > 0 else 0
        by_lot = int(investable // min_lot_cost)
        max_positions = max(0, min(n_hold, by_budget, by_lot))
    elif max_positions == 0 and investable > 0 and (
            min_lot_cost <= 0 or investable >= min_lot_cost):
        # 预算比可投金额还大:压到可投金额,只买 1 只
        per_position = min(investable, max_order) if max_order > 0 else investable
        max_positions = 1

    first_amount = per_position * FIRST_RATIO
    buy_fee = _fee(first_amount, commission_rate, min_commission, transfer_fee)
    sell_fee = _fee(first_amount, commission_rate, min_commission, transfer_fee,
                    stamp_tax, sell=True)
    fee_pct = (buy_fee / first_amount) if first_amount > 0 else 0.0

    warnings: list[str] = []
    if equity <= 0:
        warnings.append("账户没有权益,先跑一轮自动交易记快照")
    if per_position <= 0:
        warnings.append("单票预算为 0,检查单票比例和单笔上限")
    if min_lot_cost > 0 and per_position < min_lot_cost:
        warnings.append(
            f"单票预算 {per_position:,.0f} 买不到一手({min_lot_cost:,.0f}),"
            f"要么提高单票比例到 {min_lot_cost / equity:.0%} 以上,要么换低价股")
    if raised:
        warnings.append(f"预算已抬到一手成本 {per_position:,.0f},"
                        f"实际单票占比 {per_position / equity:.1%}")
    if max_positions < n_hold:
        warnings.append(f"资金只够 {max_positions} 只,目标 {n_hold} 只")
    if fee_pct > 0.005:
        warnings.append(f"首笔佣金占比 {fee_pct:.2%},分批反而更贵,建议一次买完")
    if cash < equity * 0.5:
        warnings.append("现金不足权益一半,先卖后买,别指望本轮能加满仓")

    return {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "cash_floor": round(cash_floor, 2),
        "investable": round(investable, 2),
        "per_position": round(per_position, 2),
        "per_pct": round(per_pct, 4),
        "effective_per_pct": round(per_position / equity, 4) if equity > 0 else 0.0,
        "n_hold": n_hold,
        "max_positions": max_positions,
        "min_lot_cost": round(min_lot_cost, 2),
        "first_amount": round(first_amount, 2),
        "tranches": TRANCHES,
        "first_ratio": FIRST_RATIO,
        "buy_fee": round(buy_fee, 2),
        "sell_fee": round(sell_fee, 2),
        "fee_pct": round(fee_pct, 5),
        "feasible": max_positions >= 1 and per_position > 0,
        "warnings": warnings,
    }


def render(p: dict) -> str:
    """给人看的方案。数字缺失写「—」,不写 0。"""
    def money(v):
        return "—" if v is None else f"{v:,.0f}"

    lines = [
        "## 资金方案",
        "",
        f"- 账户权益 **{money(p.get('equity'))}** · 现金 **{money(p.get('cash'))}**",
        f"- 现金底线 {money(p.get('cash_floor'))} · 可投 **{money(p.get('investable'))}**",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 单票预算 | {money(p.get('per_position'))}"
        f"({float(p.get('effective_per_pct') or 0):.1%} 权益) |",
        f"| 建议持仓 | **{p.get('max_positions')} 只**"
        f"(目标 {p.get('n_hold')} 只) |",
        f"| 首笔金额 | {money(p.get('first_amount'))}"
        f"(共 {p.get('tranches')} 批,首笔 {float(p.get('first_ratio') or 0):.0%}) |",
        f"| 首笔佣金 | 买 {money(p.get('buy_fee'))} / 卖 {money(p.get('sell_fee'))}"
        f"({float(p.get('fee_pct') or 0):.2%}) |",
        f"| 一手成本 | {money(p.get('min_lot_cost'))} |",
        "",
    ]
    if p.get("warnings"):
        lines.append("### 注意")
        lines += [f"- {w}" for w in p["warnings"]]
        lines.append("")
    if p.get("feasible"):
        lines.append(f"> 按这个方案:每只 {money(p.get('per_position'))},"
                     f"首笔 {money(p.get('first_amount'))},最多 {p.get('max_positions')} 只")
    else:
        lines.append("> 当前资金下这套参数跑不动,先调单票比例或换低价股")
    return "\n".join(lines)


def one_line(p: dict) -> str:
    """给状态栏/日志用的一行。"""
    return (f"资金方案:{p.get('max_positions')} 只 × "
            f"{float(p.get('per_position') or 0):,.0f} 元,"
            f"首笔 {float(p.get('first_amount') or 0):,.0f} 元")
