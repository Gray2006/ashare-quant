# -*- coding: utf-8 -*-
"""腾讯行情源(主源):一次请求拿全部自选股,单次往返通常 60~400ms。

为什么把它放主源而不是东财 push2:
  * push2 在高频请求后会直接 RST,且重试退避会把一次刷新拖到 3~5 秒;
  * 腾讯 qt.gtimg.cn 是纯文本快照,字段比新浪全(换手/量比/PE/PB/市值/涨跌停都有);
  * 指数用 s_ 前缀单独取,不会和个股代码串号(000001 上证 vs 平安银行)。

字段顺序(以 `~` 分隔,实测 88 段):
  1 名称 2 代码 3 现价 4 昨收 5 今开 6 成交量(手) 7 外盘 8 内盘
  9~18 买一~买五(价,量交替) 19~28 卖一~卖五
  30 时间(YYYYMMDDHHMMSS) 31 涨跌额 32 涨跌幅% 33 最高 34 最低
  36 成交量(手) 37 成交额(万元) 38 换手率% 39 市盈率(动) 43 振幅%
  44 流通市值(亿) 45 总市值(亿) 46 市净率 47 涨停价 48 跌停价
  49 量比 51 均价

指数(s_ 前缀)字段:1 名称 2 代码 3 现价 4 涨跌额 5 涨跌幅% 6 成交量(手)
  7 成交额(万元) 9 总市值(亿)
"""
from __future__ import annotations

from ..utils import (index_symbol, normalize_code, safe_float, sina_symbol)
from . import http

BASE = "https://qt.gtimg.cn/q="
HEADERS = {"Referer": "https://gu.qq.com/"}
BATCH = 60


def _at(p: list[str], i: int, default=None):
    return p[i] if i < len(p) else default


def _f(p: list[str], i: int, scale: float = 1.0):
    v = safe_float(_at(p, i))
    return None if v is None else v * scale


def _parse_stock(payload: str) -> dict | None:
    p = payload.split("~")
    if len(p) < 40 or not p[2]:
        return None
    price = _f(p, 3)
    pre = _f(p, 4)
    if not price or not pre:
        return None
    bids, asks = [], []
    for i in range(5):
        bp, bv = _f(p, 9 + i * 2), _f(p, 10 + i * 2)
        ap, av = _f(p, 19 + i * 2), _f(p, 20 + i * 2)
        if bp:
            bids.append((bp, (bv or 0) * 100))     # 手 → 股,和 sina 口径一致
        if ap:
            asks.append((ap, (av or 0) * 100))
    return {
        "code": normalize_code(p[2]),
        "name": p[1],
        "price": price,
        "pre_close": pre,
        "open": _f(p, 5),
        "high": _f(p, 33),
        "low": _f(p, 34),
        "change": _f(p, 31),
        "change_pct": _f(p, 32),
        "volume": _f(p, 6),                          # 手
        "amount": _f(p, 37, 1e4),                    # 万元 → 元
        "turnover": _f(p, 38),
        "pe_ttm": _f(p, 39),
        "amplitude": _f(p, 43),
        "float_cap": _f(p, 44, 1e8),                 # 亿 → 元
        "market_cap": _f(p, 45, 1e8),
        "pb": _f(p, 46),
        "limit_up": _f(p, 47),
        "limit_down": _f(p, 48),
        "volume_ratio": _f(p, 49),
        "avg_price": _f(p, 51),
        "bids": bids,
        "asks": asks,
        "source": "tx",
    }


def _parse_index(payload: str) -> dict | None:
    p = payload.split("~")
    if len(p) < 9 or not p[2]:
        return None
    price = _f(p, 3)
    if not price:
        return None
    return {
        "code": normalize_code(p[2]),
        "name": p[1],
        "price": price,
        "change": _f(p, 4),
        "change_pct": _f(p, 5),
        "volume": _f(p, 6),
        "amount": _f(p, 7, 1e4),
        "market_cap": _f(p, 9, 1e8),
        "source": "tx",
    }


def _fetch(symbols: list[str], parser, timeout: float, retries: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            txt = http.get_text(BASE + ",".join(chunk), headers=HEADERS,
                                encoding="gbk", timeout=timeout, retries=retries)
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            payload = line.split('="', 1)[1].rsplit('"', 1)[0]
            if not payload:
                continue
            q = parser(payload)
            if q:
                out[q["code"]] = q
    return out


def quotes(codes: list[str], timeout: float = 4.0, retries: int = 1) -> dict[str, dict]:
    """批量个股快照。code 为 [{'code','name',...}] 同东财口径。"""
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return {}
    syms = [sina_symbol(c) for c in codes]
    out = _fetch(syms, _parse_stock, timeout, retries)
    return {c: out[c] for c in out}


def index_quotes(codes: list[str], timeout: float = 3.0, retries: int = 1) -> dict[str, dict]:
    """指数快照,用 s_ 前缀,避免和个股撞号。"""
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return {}
    syms = ["s_" + index_symbol(c) for c in codes]
    return _fetch(syms, _parse_index, timeout, retries)
