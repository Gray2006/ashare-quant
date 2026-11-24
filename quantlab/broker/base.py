# -*- coding: utf-8 -*-
"""交易网关抽象。

三种实现:
  SimGateway —— 本地模拟撮合(默认,规则与回测一致)
  THSGateway —— 同花顺客户端界面自动下单(半自动,需本机校准控件)
  (券商柜台 API 可照同样接口扩展)
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..engine.models import Order, Position


class Gateway(ABC):
    name = "base"
    display_name = "抽象网关"
    is_live = False          # 是否真钱
    need_confirm = True      # 下单前是否要人工确认

    @abstractmethod
    def connect(self) -> tuple[bool, str]:
        """返回 (成功, 说明)。"""

    @abstractmethod
    def place_order(self, code: str, side: str, price: float, qty: int,
                    name: str = "") -> Order:
        """下单一笔。返回 Order(含状态)。不允许抛异常打断界面。"""

    @abstractmethod
    def positions(self) -> list[Position]:
        pass

    @abstractmethod
    def account(self) -> dict:
        """返回 {cash, frozen, market_value, total, pnl, pnl_pct}。"""

    def cancel_all(self) -> tuple[bool, str]:
        return False, "该网关不支持撤单"

    def cancel_order(self, order_id: int) -> tuple[bool, str]:
        """撤一笔委托。返回 (成功, 说明)。默认不支持。"""
        return False, "该网关不支持撤单"

    def sync(self) -> tuple[bool, str]:
        return True, ""

    def close(self) -> None:
        pass
