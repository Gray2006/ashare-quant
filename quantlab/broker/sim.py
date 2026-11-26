# -*- coding: utf-8 -*-
"""模拟交易网关:规则与回测一致(T+1、整手、费用、滑点),数据落 SQLite。

支持**多个模拟账户**:每个账户独立的现金、持仓、委托与净值曲线。
先把「选股 → 下单 → 持仓 → 盈亏」整条链路跑通,确认逻辑无误,再切实盘。
"""
from __future__ import annotations

import datetime as dt
import math
import time

from ..config import cfg
from ..data.store import store
from ..engine.models import (STATUS_CANCELLED, STATUS_FILLED, STATUS_REJECTED,
                             Order, Position, SIDE_BUY)
from ..utils import normalize_code
from .base import Gateway

DEFAULT_ACCOUNT = "默认账户"
_EQUITY_KEY = "sim_equity:{acct}"


class SimGateway(Gateway):
    name = "sim"
    display_name = "模拟盘"
    is_live = False
    need_confirm = False

    def __init__(self, config=None, account: str = DEFAULT_ACCOUNT,
                 strict_hours: bool = True):
        self.cfg = config or cfg()
        self.st = store()
        # 严格交易时段:非连续竞价(集合竞价/午休/盘后)拒绝下单。
        # 测试和回测可以传 False 绕过。
        self.strict_hours = bool(strict_hours)
        s = self.cfg.get("broker.sim", {}) or {}
        self.commission = float(s.get("commission_rate", 0.00025))
        self.min_commission = float(s.get("min_commission", 5.0))
        self.stamp_tax = float(s.get("stamp_tax_rate", 0.0005))
        self.transfer_fee = float(s.get("transfer_fee_rate", 0.00001))
        self.slippage = float(s.get("slippage_rate", 0.001))
        self.initial_cash = float(s.get("initial_cash", 1_000_000))
        self.account_name = account
        self.account_initial = self.initial_cash
        self.cash = 0.0
        self._positions: dict[str, Position] = {}
        self._trade_day = dt.date.today()
        self.ensure_account(account, self.initial_cash)
        self.switch_account(account)

    # ------------------------------------------------------------ 账户管理
    def ensure_account(self, name: str, initial_cash: float | None = None) -> None:
        names = {a["name"] for a in self.st.sim_accounts()}
        if name not in names:
            cash = float(initial_cash if initial_cash is not None else self.initial_cash)
            self.st.add_sim_account(name, cash)
            self.st.set_account(f"sim_cash:{name}", cash)

    def list_accounts(self) -> list[str]:
        names = [a["name"] for a in self.st.sim_accounts()]
        if not names:
            self.ensure_account(DEFAULT_ACCOUNT, self.initial_cash)
            names = [DEFAULT_ACCOUNT]
        return names

    def create_account(self, name: str, initial_cash: float) -> tuple[bool, str]:
        name = (name or "").strip()
        if not name:
            return False, "账户名不能为空"
        if name in self.list_accounts():
            return False, f"账户「{name}」已存在"
        self.st.add_sim_account(name, float(initial_cash))
        self.st.set_account(f"sim_cash:{name}", float(initial_cash))
        return True, f"已创建模拟账户「{name}」,初始资金 {float(initial_cash):,.0f}"

    def delete_account(self, name: str) -> tuple[bool, str]:
        """删一个模拟账户(含持仓/委托/资金)。

        删的正好是当前账户时,先切到别的账户再删 —— 以前的写法直接拒绝,
        而界面的「删除」按钮取的就是当前账户,所以永远删不掉。
        """
        name = (name or "").strip()
        accounts = self.list_accounts()
        if name not in accounts:
            return False, f"没有账户「{name}」"
        if len(accounts) <= 1:
            return False, "至少保留一个账户"
        moved = ""
        if name == self.account_name:
            target = DEFAULT_ACCOUNT if DEFAULT_ACCOUNT in accounts and name != DEFAULT_ACCOUNT \
                else next(a for a in accounts if a != name)
            self.switch_account(target)
            moved = f",已切到「{target}」"
        self.st.remove_sim_account(name)
        return True, f"已删除账户「{name}」(含持仓与委托){moved}"

    def switch_account(self, name: str) -> tuple[bool, str]:
        self.ensure_account(name, self.initial_cash)
        info = next((a for a in self.st.sim_accounts() if a["name"] == name), None)
        self.account_name = name
        self.account_initial = float((info or {}).get("initial_cash") or self.initial_cash)
        self.cash = float(self.st.get_account(f"sim_cash:{name}", self.account_initial))
        self._positions = {}
        self._load()
        return True, f"已切换到模拟账户「{name}」,可用 {self.cash:,.2f}"

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        today = dt.date.today()
        cross_day = False
        for r in self.st.load_positions(self.account_name):
            p = Position(code=r["code"], name=r.get("name") or "", qty=int(r["qty"]),
                         avail_qty=int(r["avail_qty"]), cost=float(r["cost"]),
                         opened_at=float(r.get("opened_at") or time.time()))
            # 跨日解锁 T+1:重启后 _trade_day 就是今天,_roll_t1 不会触发,
            # 隔夜持仓的 avail_qty 会一直停在 0 → 止损/换仓全被"不可卖"挡住。
            try:
                upd = dt.date.fromtimestamp(float(r.get("updated_at") or 0))
            except Exception:
                upd = today
            if upd < today and p.avail_qty < p.qty:
                p.avail_qty = p.qty
                cross_day = True
            self._positions[p.code] = p
        if cross_day:
            self._save()

    def _save(self) -> None:
        self.st.set_account(f"sim_cash:{self.account_name}", self.cash)
        self.st.save_positions([p.__dict__ for p in self._positions.values()],
                               account=self.account_name)
        self._snapshot_equity()

    def _roll_t1(self) -> None:
        today = dt.date.today()
        if today != self._trade_day:
            self._trade_day = today
            for p in self._positions.values():
                p.avail_qty = p.qty      # 隔日全部可卖
            self._save()

    # ------------------------------------------------------------ 净值曲线
    def equity_curve(self) -> list[dict]:
        return self.st.get_account(_EQUITY_KEY.format(acct=self.account_name), []) or []

    def _snapshot_equity(self) -> None:
        """每个交易日留一个点;当日已有则覆盖。"""
        try:
            curve = self.equity_curve()
            today = dt.date.today().isoformat()
            total = self.cash + sum(p.market_value for p in self._positions.values())
            if curve and curve[-1].get("date") == today:
                curve[-1]["equity"] = round(total, 2)
            else:
                curve.append({"date": today, "equity": round(total, 2)})
            self.st.set_account(_EQUITY_KEY.format(acct=self.account_name), curve[-600:])
            # 同一份权益也写进 sl_equity:复盘页的当日盈亏读的是那张表。
            # 以前只有自动交易跑完才写 sl_equity,手动下单后复盘页的基准
            # 还停在旧值,两套快照各写各的,页面上的数字就对不上。
            try:
                from ..engine import daily_review as dr
                dr.snapshot(self.account_name, equity=total, cash=self.cash,
                            market_value=total - self.cash)
            except Exception:  # noqa: BLE001
                pass
        except Exception:
            pass

    # ------------------------------------------------------------ 接口
    def connect(self) -> tuple[bool, str]:
        self._snapshot_equity()
        return True, (f"模拟盘「{self.account_name}」就绪,"
                      f"可用资金 {self.cash:,.2f}")

    def fees(self, side: str, amount: float) -> float:
        fee = max(amount * self.commission, self.min_commission) + amount * self.transfer_fee
        if side == "sell":
            fee += amount * self.stamp_tax
        return fee

    def place_order(self, code: str, side: str, price: float, qty: int,
                    name: str = "") -> Order:
        self._roll_t1()
        code = normalize_code(code)
        # 落库前最后一道:风控层已经拦过,这里再断言一次。
        # 任何一行 NaN 写进 SQLite 都是永久的 —— cash -= NaN 之后
        # 这个账户的每一笔盈亏、净值曲线都再也不会正常,只能重建。
        try:
            price_f = float(price)
            qty_f = float(qty)
        except (TypeError, ValueError):
            price_f = qty_f = float("nan")
        if not (math.isfinite(price_f) and price_f > 0
                and math.isfinite(qty_f) and qty_f > 0):
            o = Order(code=code, name=name, side=side, price=0.0,
                      qty=0, gateway=self.name)
            o.status = STATUS_REJECTED
            o.remark = "委托参数非法(NaN/Inf/非正数),已在落库前拒绝"
            rec = o.to_dict()
            rec["account"] = self.account_name
            rec.update(status=o.status, remark=o.remark)
            self.st.insert_order(rec)
            return o
        if self.strict_hours:
            from ..utils import is_trading_time
            if not is_trading_time():
                o = Order(code=code, name=name, side=side, price=float(price),
                          qty=int(qty), gateway=self.name)
                o.status = STATUS_REJECTED
                o.remark = "非交易时间,拒绝下单"
                rec = o.to_dict()
                rec["account"] = self.account_name
                rec.update(status=o.status, remark=o.remark)
                self.st.insert_order(rec)
                return o
        o = Order(code=code, name=name, side=side, price=float(price), qty=int(qty),
                  gateway=self.name)
        rec = o.to_dict()
        rec["account"] = self.account_name
        slip = self.slippage if side == SIDE_BUY else -self.slippage
        fill = round(float(price) * (1 + slip), 3)
        amount = fill * qty
        fee = self.fees(side, amount)
        if side == SIDE_BUY:
            if amount + fee > self.cash:
                o.status = STATUS_REJECTED
                o.remark = f"资金不足(需 {amount + fee:,.2f},可用 {self.cash:,.2f})"
                rec.update(status=o.status, remark=o.remark)
                self.st.insert_order(rec)
                return o
            self.cash -= amount + fee
            p = self._positions.get(code)
            if p:
                total_cost = p.cost * p.qty + amount + fee
                p.qty += qty
                p.cost = total_cost / p.qty
            else:
                self._positions[code] = Position(code=code, name=name, qty=qty, avail_qty=0,
                                                 cost=(amount + fee) / qty)
        else:
            p = self._positions.get(code)
            if not p or p.qty < qty:
                o.status = STATUS_REJECTED
                o.remark = "持仓不足"
                rec.update(status=o.status, remark=o.remark)
                self.st.insert_order(rec)
                return o
            # T+1:当日买入的份额 avail_qty 还是 0,网关自己也要拦一道。
            # 风控层拦过不代表这层可以不拦 —— 绕过风控的调用(自动交易、
            # 脚本、以后的接口)一样会走到这里,否则当天买入当天就能卖。
            if p.avail_qty < qty:
                o.status = STATUS_REJECTED
                o.remark = (f"可卖数量不足(持仓 {p.qty},可卖 {p.avail_qty},"
                            f"T+1 当日买入不可卖)")
                rec.update(status=o.status, remark=o.remark)
                self.st.insert_order(rec)
                return o
            self.cash += amount - fee
            p.qty -= qty
            p.avail_qty = max(0, p.avail_qty - qty)
            if p.qty == 0:
                self._positions.pop(code, None)
        o.status = STATUS_FILLED
        o.filled_price = fill
        o.filled_qty = qty
        o.fee = round(fee, 2)
        rec.update(status=o.status, filled_price=fill, filled_qty=qty, fee=o.fee)
        o.id = self.st.insert_order(rec)
        self._save()
        return o

    def cancel_order(self, order_id: int) -> tuple[bool, str]:
        """撤一笔未成交的委托。模拟盘即时成交,只有 submitted/partial 能撤。"""
        rows = self.st.orders(200, account=self.account_name)
        row = next((r for r in rows if int(r["id"]) == int(order_id)), None)
        if not row:
            return False, "找不到这笔委托"
        st = str(row.get("status") or "")
        if st == STATUS_FILLED:
            return False, "已成交,无法撤单"
        if st in (STATUS_CANCELLED, STATUS_REJECTED):
            return False, "订单已结束"
        self.st.update_order(int(order_id), status=STATUS_CANCELLED,
                             remark=(row.get("remark") or "") + " · 已撤单")
        return True, "已撤单"

    def positions(self) -> list[Position]:
        self._roll_t1()
        return list(self._positions.values())

    def set_mark_prices(self, quotes: dict[str, dict]) -> None:
        for p in self._positions.values():
            q = quotes.get(p.code)
            if q:
                p.last_price = q.get("price")
                p.name = p.name or q.get("name") or ""

    def account(self) -> dict:
        mv = sum(p.market_value for p in self._positions.values())
        total = self.cash + mv
        init = self.account_initial or 1.0
        return {"cash": self.cash, "frozen": 0.0, "market_value": mv, "total": total,
                "pnl": total - init, "pnl_pct": (total / init - 1) * 100 if init else 0.0,
                "account": self.account_name, "initial": init}

    def reset(self, initial_cash: float | None = None) -> None:
        cash = float(initial_cash if initial_cash is not None
                     else self.account_initial or self.initial_cash)
        self.cash = cash
        self.account_initial = cash
        self._positions.clear()
        self.st.add_sim_account(self.account_name, cash)
        self.st.set_account(_EQUITY_KEY.format(acct=self.account_name), [])
        self._save()
