# -*- coding: utf-8 -*-
"""核心数据模型(纯 dataclass,不依赖 Qt)。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

SIDE_BUY = "buy"
SIDE_SELL = "sell"

STATUS_NEW = "new"
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_PARTIAL = "partial"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unverified"


@dataclass
class Order:
    code: str
    side: str
    price: float
    qty: float          # A 股是整数;加密现货是小数(0.001 BTC),统一按 float 存
    name: str = ""
    status: str = STATUS_NEW
    filled_price: float | None = None
    filled_qty: float = 0.0
    fee: float = 0.0
    gateway: str = "sim"
    remark: str = ""
    id: int | None = None
    ts: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    @property
    def amount(self) -> float:
        return (self.filled_price or self.price) * (self.filled_qty or self.qty)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    code: str
    name: str = ""
    qty: float = 0.0        # A 股整数股;加密小数币量
    avail_qty: float = 0.0
    cost: float = 0.0
    opened_at: float = field(default_factory=time.time)
    last_price: float | None = None

    @property
    def market_value(self) -> float:
        return (self.last_price or self.cost) * self.qty

    @property
    def pnl(self) -> float:
        return ((self.last_price or self.cost) - self.cost) * self.qty

    @property
    def pnl_pct(self) -> float:
        return ((self.last_price or self.cost) / self.cost - 1) * 100 if self.cost else 0.0


@dataclass
class Trade:
    """回测中的一笔完整交易。"""
    code: str
    name: str
    open_date: str
    close_date: str
    open_price: float
    close_price: float
    qty: int
    fee: float
    profit: float
    profit_pct: float
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    code: str
    name: str
    score: float
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Account:
    cash: float = 0.0
    frozen: float = 0.0
    market_value: float = 0.0
    total: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
