# -*- coding: utf-8 -*-
"""通用小工具:股票代码、市场判定、交易时段、数值格式化。"""
from __future__ import annotations

import datetime as dt
import re

# ---------------------------------------------------------------- 代码与市场

_CODE_RE = re.compile(r"(\d{6})")


def normalize_code(raw: str) -> str:
    """把 '600519'、'sh600519'、'600519.SH'、'SH600519' 统一成 '600519'。"""
    if not raw:
        return ""
    m = _CODE_RE.search(str(raw))
    return m.group(1) if m else str(raw).strip()


def market_of(code: str) -> int:
    """东财 market: 1=沪市, 0=深市(含创业板/北交所按深市口径处理)。"""
    c = normalize_code(code)
    if c.startswith(("6", "9", "5", "7")):
        return 1
    return 0


def secid(code: str) -> str:
    """东财 secid,形如 '1.600519'。"""
    c = normalize_code(code)
    return f"{market_of(c)}.{c}"


def sina_symbol(code: str) -> str:
    """新浪行情代码,形如 'sh600519'。"""
    c = normalize_code(code)
    return ("sh" if market_of(c) == 1 else "sz") + c


def is_index(code: str) -> bool:
    c = normalize_code(code)
    return c.startswith(("000001", "399001", "399006", "000300", "000905", "000016", "880"))


# 指数与个股代码会撞号(000001 既是上证指数也是平安银行),需要显式标注
_SH_INDEX = {"000001", "000300", "000688", "000905", "000016", "000852", "000688"}


def index_symbol(code: str) -> str:
    """指数 → 新浪/腾讯代码。"""
    c = normalize_code(code)
    return ("sh" if c in _SH_INDEX or c.startswith("000") else "sz") + c


def index_secid(code: str) -> str:
    """指数 → 东财 secid。"""
    c = normalize_code(code)
    return ("1." if c in _SH_INDEX or c.startswith("000") else "0.") + c


def board_of(code: str) -> str:
    """板块归类,用于涨跌幅限制与筛选。"""
    c = normalize_code(code)
    if c.startswith("688"):
        return "科创板"
    if c.startswith("300") or c.startswith("301"):
        return "创业板"
    if c.startswith(("83", "87", "43", "92")):
        return "北交所"
    if c.startswith("6"):
        return "沪主板"
    if c.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "其他"


def limit_pct(code: str, is_st: bool = False) -> float:
    """涨跌幅限制比例。

    口径统一到 engine/exchange.py(2026-07 现状):
    沪深主板 10%、创业板/科创板 20%、北交所 30%;
    ST 股沪深主板 10%、创业板/科创板 20%、北交所 30%。
    """
    from .engine.exchange import BOARD_LIMIT, ST_LIMIT

    b = board_of(code)
    table = ST_LIMIT if is_st else BOARD_LIMIT
    return table.get(b, 0.10)


# ---------------------------------------------------------------- 交易时段

def now() -> dt.datetime:
    return dt.datetime.now()


def _t(h: int, m: int) -> dt.time:
    return dt.time(h, m)


def session_state(t: dt.datetime | None = None) -> str:
    """当前处于哪个时段: pre / call / trade / close_call / noon / after / after_hours / closed。

    A 股时段:
      9:15-9:25 开盘集合竞价(call)
      9:30-11:30 / 13:00-14:57 连续竞价(trade)
      14:57-15:00 收盘集合竞价(close_call)
      15:05-15:30 盘后固定价格交易(after_hours,科创板/创业板)
    """
    t = t or now()
    if t.weekday() >= 5:
        return "closed"
    tm = t.time()
    if tm < _t(9, 15):
        return "pre"
    if tm < _t(9, 25):
        return "call"
    if tm < _t(9, 30):
        return "pre"
    if tm <= _t(11, 30):
        return "trade"
    if tm < _t(13, 0):
        return "noon"
    if tm < _t(14, 57):
        return "trade"
    if tm <= _t(15, 0):
        return "close_call"
    if tm < _t(15, 5):
        return "after"
    if tm <= _t(15, 30):
        return "after_hours"
    return "after"


def parse_hhmm(text: str, default: str = "09:30") -> str:
    """把 '9.30' / '9:30' / '0930' / '9点30' / '930' 统一成 'HH:MM'。

    调度窗口是按字符串比较时间的,格式不统一就永远进不了窗口,
    这就是 config 里写进 '9.30' 后自动交易不触发的原因。
    """
    raw = str(text or "").strip()
    if not raw:
        return default
    nums = re.findall(r"\d+", raw)
    if not nums:
        return default
    if len(nums) >= 2:
        h, m = int(nums[0]), int(nums[1])
    else:
        digits = nums[0]
        if len(digits) <= 2:
            h, m = int(digits), 0
        elif len(digits) == 3:
            h, m = int(digits[:1]), int(digits[1:])
        else:
            h, m = int(digits[:2]), int(digits[2:4])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return default
    return f"{h:02d}:{m:02d}"


def is_trading_time(t: dt.datetime | None = None) -> bool:
    return session_state(t) == "trade"


SESSION_LABEL = {
    "pre": "盘前",
    "call": "集合竞价",
    "trade": "交易中",
    "close_call": "收盘竞价",
    "noon": "午休",
    "after": "盘后",
    "after_hours": "盘后定价",
    "closed": "休市",
}


# ---------------------------------------------------------------- 格式化

def fmt_amount(v: float | None) -> str:
    """成交额/资金额 → 亿/万。"""
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def fmt_price(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}"


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}"


def safe_float(v, default=None):
    try:
        if v is None or v == "-" or v == "":
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def pct_color(v: float | None) -> str:
    """A 股习惯:涨红跌绿。"""
    if v is None or v == 0:
        return "#c9d1d9"
    return "#ef4444" if v > 0 else "#22c55e"
