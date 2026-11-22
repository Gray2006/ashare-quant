# -*- coding: utf-8 -*-
"""交易规则:A 股(2026-07 现状)+ 加密现货,下单与回测共用同一份口径。

收录的规则:
  * 涨跌幅:沪深主板 10%、创业板/科创板 20%、北交所 30%;
    风险警示股(ST/*ST):沪深主板自 2026-07-06 起由 5% 放宽到 10%,
    创业板/科创板 ST 仍 20%,北交所 ST 30%
  * 新股:注册制下上市前 5 个交易日不设涨跌幅(第 6 日起按板块)
  * 申报单位:沪深主板/创业板买入 100 股整数倍;科创板 200 股起、
    超出部分 1 股递增;北交所 100 股起、1 股递增。
    卖出时不足最小单位的部分必须一次性卖出
  * 报价单位:0.01 元(基金/债券另有规定,这里只管股票)
  * 交易时段:9:15-9:25 开盘集合竞价(9:20-9:25 不可撤单)、
    9:30-11:30 与 13:00-15:00 连续竞价、14:57-15:00 收盘集合竞价、
    15:05-15:30 盘后固定价格交易(全部 A 股,北交所暂不实施)
  * T+1:当日买入次日才可卖
  * 临时停牌:科创板/创业板 盘中较开盘价涨跌 30%、60% 各停 10 分钟;
    主板新股上市首日较开盘价涨跌 10% 停 30 分钟、20% 停到 14:57
"""
from __future__ import annotations

import datetime as dt

from ..utils import board_of, normalize_code

# 各板块涨跌幅(2026-07 口径)
BOARD_LIMIT = {"沪主板": 0.10, "深主板": 0.10, "创业板": 0.20,
               "科创板": 0.20, "北交所": 0.30}
# 风险警示股涨跌幅(沪主板 2026-07-06 起 10%)
ST_LIMIT = {"沪主板": 0.10, "深主板": 0.10, "创业板": 0.20,
            "科创板": 0.20, "北交所": 0.30}

# 申报单位:(最小数量, 递增单位)
LOT = {"沪主板": (100, 100), "深主板": (100, 100), "创业板": (100, 100),
       "科创板": (200, 1), "北交所": (100, 1)}

PRICE_TICK = 0.01
NEW_STOCK_FREE_DAYS = 5      # 注册制新股前 5 个交易日不设涨跌幅

SESSION_PHASES = [
    ("09:15", "09:20", "开盘集合竞价", True, True, True),
    ("09:20", "09:25", "开盘集合竞价(不可撤单)", True, False, True),
    ("09:25", "09:30", "撮合与静止", False, False, False),
    ("09:30", "11:30", "上午连续竞价", True, True, True),
    ("11:30", "13:00", "午间休市", False, False, False),
    ("13:00", "14:57", "下午连续竞价", True, True, True),
    ("14:57", "15:00", "收盘集合竞价", True, False, True),
    ("15:05", "15:30", "盘后固定价格交易", True, False, True),
]


def is_st(name: str | None) -> bool:
    n = (name or "").upper().replace(" ", "")
    return "ST" in n


def limit_pct(code: str, name: str | None = None, is_st_flag: bool | None = None,
              is_new: bool = False) -> float:
    """涨跌幅限制。is_new = 上市前 5 个交易日(无限制,返回 inf)。"""
    if is_new:
        return float("inf")
    st = is_st(name) if is_st_flag is None else bool(is_st_flag)
    b = board_of(code)
    table = ST_LIMIT if st else BOARD_LIMIT
    return table.get(b, 0.10)


def limit_prices(pre_close: float, code: str, name: str | None = None,
                 is_new: bool = False) -> tuple[float | None, float | None]:
    """(涨停价, 跌停价),四舍五入到分。无涨跌幅限制时返回 (None, None)。"""
    pct = limit_pct(code, name, is_new=is_new)
    if pct == float("inf"):
        return None, None
    up = round(pre_close * (1 + pct) + 1e-9, 2)
    down = round(pre_close * (1 - pct) + 1e-9, 2)
    return up, down


def lot_of(code: str) -> tuple[int, int]:
    """(最小申报数量, 递增单位)。"""
    return LOT.get(board_of(code), (100, 100))


def min_qty(code: str) -> int:
    return lot_of(code)[0]


def valid_qty(code: str, qty: int, side: str = "buy",
              holding: int | None = None) -> tuple[bool, str]:
    """申报数量是否合规。卖出允许一次性清掉零股。"""
    base, step = lot_of(code)
    if qty <= 0:
        return False, "数量必须大于 0"
    if side == "sell" and holding is not None and qty == holding:
        return True, ""            # 清仓允许包含零股
    if qty < base:
        return False, f"数量不足最小申报单位 {base} 股(当前 {qty})"
    if step > 1 and qty % step != 0:
        return False, f"数量必须是 {step} 股的整数倍(当前 {qty})"
    return True, ""


def round_qty(code: str, qty: int) -> int:
    """按申报单位向下取整。"""
    base, step = lot_of(code)
    if qty < base:
        return 0
    return base + (int(qty) - base) // step * step if step > 1 else int(qty) // 100 * 100


def tick_price(px: float) -> float:
    return round(px / PRICE_TICK) * PRICE_TICK


def session_phase(t: dt.datetime | None = None) -> dict:
    """当前时段详情:名称、能否委托、能否撤单、是否连续竞价。"""
    t = t or dt.datetime.now()
    if t.weekday() >= 5:
        return {"phase": "休市", "can_order": False, "can_cancel": False,
                "continuous": False, "label": "休市"}
    tm = t.strftime("%H:%M")
    out = []
    for start, end, label, can_order, can_cancel, continuous in SESSION_PHASES:
        if start <= tm < end:
            out.append({"phase": label, "can_order": can_order,
                        "can_cancel": can_cancel, "continuous": continuous,
                        "label": label})
            break
    return out[0] if out else {"phase": "非交易时段", "can_order": False,
                               "can_cancel": False, "continuous": False,
                               "label": "非交易时段"}


# ---------------------------------------------------------------- 加密现货
# 与 A 股的根本差异:7×24 无休市、无涨跌停、无 T+1、数量可小数、
# 最小申报量/步长由交易对规格决定(instruments 的 lotSz/minSz/tickSz)。

CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "OKB")


def is_crypto(code: str) -> bool:
    """交易对形如 BTC-USDT / ETH-USDT-SWAP;A 股代码永远没有连字符。"""
    s = str(code or "").strip()
    return "-" in s and not s.isdigit()


def asset_market(code: str) -> str:
    return "crypto" if is_crypto(code) else "ashare"


def step_decimals(step: float | str | None) -> int:
    """步长对应小数位:0.0001 → 4。"""
    try:
        s = f"{float(step):.12f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0
    except (TypeError, ValueError):
        return 2


def floor_step(value: float, step: float | str | None) -> float:
    """按步长向下取整,用 Decimal 避免浮点尾巴(0.30000000000000004)。"""
    from decimal import Decimal, ROUND_DOWN

    try:
        st = Decimal(str(step or 0))
        if st <= 0:
            return float(value)
        d = Decimal(str(value))
        return float((d / st).to_integral_value(rounding=ROUND_DOWN) * st)
    except Exception:
        return float(value)


def crypto_valid_qty(qty: float, lot_sz: float, min_sz: float,
                     side: str = "buy") -> tuple[bool, str, float]:
    """加密数量是否合规。返回 (ok, 原因, 取整后的数量)。"""
    if qty is None or qty <= 0:
        return False, "数量必须大于 0", 0.0
    step = float(lot_sz or 0)
    adj = floor_step(qty, step) if step > 0 else float(qty)
    if adj <= 0:
        return False, f"数量小于最小步长 {step:g}", 0.0
    if min_sz and adj < float(min_sz):
        return False, f"数量 {adj:g} 低于最小申报量 {float(min_sz):g}", adj
    if step > 0 and abs(adj - float(qty)) > step * 1e-6:
        return False, (f"数量必须是 {step:g} 的整数倍;"
                       f"按步长最多 {adj:g}"), adj
    return True, "", adj


def crypto_phase() -> dict:
    """加密市场永远可交易(OKX 维护窗口除外)。"""
    return {"phase": "7×24 连续交易", "can_order": True, "can_cancel": True,
            "continuous": True, "label": "7×24"}


def crypto_describe() -> str:
    rows = [
        "时段:7×24 连续交易,无集合竞价、无休市",
        "涨跌幅:无涨跌停限制(现货)",
        "T+1:无,买入即可卖(可用余额内)",
        "申报:数量按交易对 lotSz 步长取整,不得低于 minSz;"
        "价格按 tickSz 取整",
        "费用:OKX 现货手续费按成交额收取,以成交回执为准",
        "最小名义额:各交易对不同,下单前以 instruments 为准",
    ]
    return "\n".join(rows)


def is_continuous(t: dt.datetime | None = None) -> bool:
    return bool(session_phase(t)["continuous"])


def describe() -> str:
    """给界面/文档用的一段规则说明。"""
    rows = [
        "涨跌幅:沪深主板 10% · 创业板/科创板 20% · 北交所 30%",
        "ST/*ST:沪深主板 10%(2026-07-06 起) · 创业板/科创板 20% · 北交所 30%",
        "新股:注册制上市前 5 个交易日不设涨跌幅",
        "申报:沪/深主板与创业板 100 股整数倍;科创板 200 股起、1 股递增;"
        "北交所 100 股起、1 股递增;卖出零股须一次性申报",
        "报价:0.01 元 · T+1:当日买入次日可卖",
        "时段:9:15-9:25 集合竞价(9:20 后不可撤)· 9:30-11:30 / 13:00-15:00 连续竞价"
        " · 14:57-15:00 收盘集合竞价 · 15:05-15:30 盘后固定价格(北交所暂无)",
        "加密现货(BTC-USDT 这类):7×24、无涨跌停、无 T+1,数量按 lotSz 取整",
    ]
    return "\n".join(rows)
