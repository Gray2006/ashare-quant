# -*- coding: utf-8 -*-
"""腾讯数据源的补充资金流:用分时(分钟)数据按 tick 规则近似主动买卖。

东财 f62 被 RST、新浪 MoneyFlow 也抽风时,这是第三条路。
原理很朴素:相邻两分钟的涨就把他那分钟的成交额算成主动买入,跌算主动卖出,
净额 = 主动买入额 - 主动卖出额。精度不如逐笔,但方向和量级够看。
"""
from __future__ import annotations

import time

from ..utils import normalize_code, sina_symbol
from .http import get_json, get_text

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 60.0


def minute_bars(code: str) -> list[tuple[str, float, float, float]]:
    """返回 [(时间, 价, 量(手), 额), ...]。腾讯分时接口。"""
    sym = sina_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    try:
        data = get_json(url, timeout=6.0, retries=1)
    except Exception:
        return []
    try:
        node = data["data"][sym]["data"]["data"]
    except Exception:
        return []
    out: list[tuple[str, float, float, float]] = []
    for line in node or []:
        parts = str(line).split()
        if len(parts) < 4:
            continue
        try:
            out.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
        except (TypeError, ValueError):
            continue
    return out


def moneyflow(code: str, use_cache: bool = True) -> dict:
    """按 tick 规则估主动买卖。返回 {main, super, big, mid, small, pct, source}。"""
    code = normalize_code(code)
    if use_cache:
        hit = _CACHE.get(code)
        if hit and time.monotonic() - hit[0] < _TTL:
            return hit[1]
    bars = minute_bars(code)
    if len(bars) < 5:
        return {}
    # 腾讯分时的「额」是当日累计值,必须差分才是每分钟成交额,否则会重复累加
    amounts: list[float] = []
    prev_amt = 0.0
    cumulative = all(bars[i][3] >= bars[i - 1][3] for i in range(1, len(bars)))
    for i, (_t, _px, _vol, amt) in enumerate(bars):
        if cumulative:
            step = max(0.0, amt - prev_amt)
            prev_amt = amt
        else:
            step = amt
        amounts.append(step if i else 0.0)

    buy = sell = 0.0
    big_buy = big_sell = 0.0
    mid_buy = mid_sell = 0.0
    for i in range(1, len(bars)):
        px = bars[i][1]
        prev = bars[i - 1][1]
        amt = amounts[i]
        if px > prev:
            buy += amt
            if amt >= 1_000_000:      # 单分钟百万以上算大单
                big_buy += amt
            else:
                mid_buy += amt
        elif px < prev:
            sell += amt
            if amt >= 1_000_000:
                big_sell += amt
            else:
                mid_sell += amt
    total = buy + sell
    main = buy - sell
    out = {
        "main": main,
        "super": big_buy - big_sell,
        "big": (buy - big_buy) - (sell - big_sell),
        "mid": mid_buy - mid_sell,
        "small": 0.0,
        "pct": (main / total * 100) if total else None,
        "buy_amount": buy, "sell_amount": sell,
        "source": "tx-minute",
        "asof": bars[-1][0],
    }
    _CACHE[code] = (time.monotonic(), out)
    return out


def intraday_flow(code: str) -> dict:
    """分时资金流曲线,结构与东财 moneyflow_intraday 对齐。

    东财 push2 的 fflow 接口经常 RST,这里用腾讯分时自己算:
    每分钟按涨跌把该分钟成交额归入主动买卖,再累计成曲线。
    """
    code = normalize_code(code)
    bars = minute_bars(code)
    if len(bars) < 5:
        return {"name": None, "bars": []}
    cumulative = all(bars[i][3] >= bars[i - 1][3] for i in range(1, len(bars)))
    out: list[dict] = []
    acc_main = acc_big = acc_super = 0.0
    prev_amt = 0.0
    for i in range(1, len(bars)):
        t, px, _vol, amt = bars[i]
        if cumulative:
            step = max(0.0, amt - bars[i - 1][3])
        else:
            step = amt
        delta = 0.0
        if px > bars[i - 1][1]:
            delta = step
        elif px < bars[i - 1][1]:
            delta = -step
        acc_main += delta
        if step >= 5_000_000:          # 超大单
            acc_super += delta
        elif step >= 1_000_000:        # 大单
            acc_big += delta
        out.append({"t": f"{t[:2]}:{t[2:]}", "main": acc_main, "big": acc_big,
                    "super": acc_super, "mid": 0.0, "small": 0.0})
    return {"name": None, "bars": out, "source": "tx-minute"}


def quote_with_money(code: str) -> dict:
    """给实时行情补一个 main_inflow 字段,取不到就返回空。"""
    d = moneyflow(code)
    if not d:
        return {}
    return {"main_inflow": d.get("main"), "main_ratio": d.get("pct")}


def moneyflow_rank(codes: list[str], top: int = 30) -> list[dict]:
    """按代码列表算净流入排行(本地算,不依赖排行接口)。"""
    rows = []
    for c in codes:
        d = moneyflow(c)
        if d:
            rows.append({"code": normalize_code(c), "main_inflow": d.get("main"),
                         "main_ratio": d.get("pct")})
    rows.sort(key=lambda r: r.get("main_inflow") or 0, reverse=True)
    return rows[:top]
