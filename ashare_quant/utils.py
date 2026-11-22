"""代码、市场与日期的小工具。"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

_CODE_RE = re.compile(r"(\d{6})")

# 指数：统一用 6 位代码，内部靠 INDEX_MARKET 区分，避免和平安银行(000001)串号。
INDEX_MARKET = {
    "000001": "SH",  # 上证指数
    "000300": "SH",  # 沪深300
    "000905": "SH",  # 中证500
    "000852": "SH",  # 中证1000
    "399001": "SZ",  # 深证成指
    "399006": "SZ",  # 创业板指
    "399005": "SZ",  # 中小100
    "899050": "BJ",  # 北证50
}

INDEX_NAMES = {
    "000001": "上证指数",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "399001": "深证成指",
    "399006": "创业板指",
    "899050": "北证50",
}


def normalize_code(value: str) -> str:
    """把 sh600519 / 600519.SH / 600519 统一成 6 位数字。"""
    if value is None:
        return ""
    text = str(value).strip().upper()
    match = _CODE_RE.search(text)
    return match.group(1) if match else text


def market_of(code: str) -> str:
    """按股票代码判断交易所。注意 000001 是平安银行，不是上证指数。"""
    code = normalize_code(code)
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def is_index(code: str) -> bool:
    return normalize_code(code) in INDEX_MARKET


def secid(code: str, index: bool = False) -> str:
    """东方财富 secid：沪市 1.xxxxxx，深市/北交所 0.xxxxxx。

    index=True 时按指数表取市场，避免 000001 与平安银行串号。
    """
    code = normalize_code(code)
    market = INDEX_MARKET[code] if index else market_of(code)
    return f"{'1' if market == 'SH' else '0'}.{code}"


def tx_symbol(code: str, index: bool = False) -> str:
    code = normalize_code(code)
    market = INDEX_MARKET[code] if index else market_of(code)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market]
    return prefix + code


def safe_float(value) -> float | None:
    try:
        if value in (None, "", "-", "--"):
            return None
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10] if "-" in text or "/" in text else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def date_str(value: date | datetime | str) -> str:
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else ""


def prev_weekday(value: date) -> date:
    cursor = value - timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def is_weekday(value: date) -> bool:
    return value.weekday() < 5


def pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.{digits}f}%"


def human_number(value: float | None) -> str:
    if value is None:
        return "-"
    absolute = abs(value)
    for unit, scale in (("亿", 1e8), ("万", 1e4)):
        if absolute >= scale:
            return f"{value / scale:.2f}{unit}"
    return f"{value:.2f}"
