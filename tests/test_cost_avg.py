# -*- coding: utf-8 -*-
"""成本摊薄的双向逻辑:涨到阈值先卖、跌到阈值先买,一轮平掉后能再开。"""
from quantlab.engine.autotrader import AutoTrader


class _P:
    def __init__(self, code, qty, avail, cost):
        self.code, self.qty, self.avail_qty, self.cost = code, qty, avail, cost
        self.name = code
        self.last_price = cost

    @property
    def market_value(self):
        return self.qty * self.last_price


class _GW:
    def __init__(self):
        self.orders = []

    def account(self):
        return {"cash": 100000.0, "total": 200000.0}

    def positions(self):
        return []

    def place_order(self, code, side, price, qty, name=""):
        self.orders.append((code, side, price, qty))
        class _O:
            id, status, remark = "o1", "FILLED", ""
        return _O()


def _trader(gw):
    t = AutoTrader(gw, dry_run=False, cost_avg=True, cost_avg_band=0.05,
                   cost_avg_take=0.02, cost_avg_ratio=0.3, per_position_pct=0.1)
    return t


def test_high_sell_then_buy_back():
    gw = _GW()
    t = _trader(gw)
    p = _P("600000", 1000, 1000, 10.0)
    pos = {"600000": p}

    # 涨 10% → 先卖
    t._cost_average(type("P", (), {"sells": [], "buys": [], "skipped": []})(), pos,
                    {"600000": {"price": 11.0}}, gw.account())
    assert gw.orders and gw.orders[0][1] == "sell"
    leg = t._t_pending["600000"]
    assert leg["side"] == "sell"

    # 回落 2% 以上 → 买回
    t._cost_average(type("P", (), {"sells": [], "buys": [], "skipped": []})(), pos,
                    {"600000": {"price": 10.5}}, gw.account())
    assert gw.orders[-1][1] == "buy"
    assert "600000" not in t._t_pending


def test_low_buy_then_sell():
    gw = _GW()
    t = _trader(gw)
    p = _P("600000", 1000, 1000, 10.0)
    pos = {"600000": p}

    # 跌 10% → 先买
    t._cost_average(type("P", (), {"sells": [], "buys": [], "skipped": []})(), pos,
                    {"600000": {"price": 9.0}}, gw.account())
    assert gw.orders and gw.orders[0][1] == "buy"
    assert t._t_pending["600000"]["side"] == "buy"

    # 反弹 2% 以上 → 卖出(卖原底仓)
    t._cost_average(type("P", (), {"sells": [], "buys": [], "skipped": []})(), pos,
                    {"600000": {"price": 9.5}}, gw.account())
    assert gw.orders[-1][1] == "sell"
    assert "600000" not in t._t_pending


def test_no_action_inside_band():
    gw = _GW()
    t = _trader(gw)
    p = _P("600000", 1000, 1000, 10.0)
    t._cost_average(type("P", (), {"sells": [], "buys": [], "skipped": []})(),
                    {"600000": p}, {"600000": {"price": 10.2}}, gw.account())
    assert gw.orders == []


def test_can_run_multiple_rounds_same_day():
    gw = _GW()
    t = _trader(gw)
    p = _P("600000", 1000, 1000, 10.0)
    pos = {"600000": p}
    Plan = type("P", (), {"sells": [], "buys": [], "skipped": []})

    for px in (11.0, 10.5, 11.0, 10.5):     # 卖/买/卖/买 —— 不限轮次
        t._cost_average(Plan(), pos, {"600000": {"price": px}}, gw.account())
    sides = [o[1] for o in gw.orders]
    assert sides == ["sell", "buy", "sell", "buy"]
