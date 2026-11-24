# -*- coding: utf-8 -*-
"""市场热点:情绪、概念/行业涨幅榜、强势个股、热点快讯。

组合现有数据源,不新造接口:
  · 情绪:engine/limitup.snapshot(涨停/跌停/炸板率/最高板/晋级率/主线)
  · 板块:data/sector.boards("gn"/"hy")(新浪,含涨幅、主力净流入、领涨股)
  · 个股:data/sina.market_list(涨幅榜;push2 的 clist 最近被拒,新浪这条还通)
  · 快讯:data/newsfeed.feed

每块独立 try:某一块挂了只记 errors,页面显示其它块并说明缺哪块。
"""
from __future__ import annotations

import time

from ..data import newsfeed, sector, sina
from . import limitup

CACHE_TTL = 120.0
_cache: dict[str, tuple[float, dict]] = {}

# 强势股里剔掉北交所、新股(N/C 开头)和 ST:它们的涨幅会带偏榜单
_BJ_PREFIX = ("4", "8", "920")


def _rank_boards(rows: list[dict], top: int) -> list[dict]:
    rows = [r for r in rows if (r.get("change_pct") is not None)]
    rows.sort(key=lambda r: -r["change_pct"])
    out = []
    for r in rows[:top]:
        out.append({
            "name": r.get("name") or "",
            "pct": r.get("change_pct"),
            "main": r.get("main"),
            "main_ratio": r.get("main_ratio"),
            "lead_name": r.get("lead_name") or "",
            "lead_pct": r.get("lead_pct"),
            "amount": r.get("amount"),
        })
    return out


def _strong_stocks(rows: list[dict], top: int = 20) -> list[dict]:
    out = []
    for r in rows:
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        if not code or code.startswith(_BJ_PREFIX):
            continue
        if name.startswith(("N", "C")) or "ST" in name.upper():
            continue
        out.append({
            "code": code,
            "name": name,
            "price": r.get("price"),
            "pct": r.get("change_pct"),
            "turnover": r.get("turnover"),
            "amount": r.get("amount"),
            "market_cap": r.get("market_cap"),
            "pe_ttm": r.get("pe_ttm"),
        })
        if len(out) >= top:
            break
    return out


def _news(rows: list[dict], top: int = 18) -> list[dict]:
    rows = sorted(rows, key=lambda r: str(r.get("time") or ""), reverse=True)
    return [{
        "time": (r.get("time") or "")[5:16],
        "title": (r.get("title") or "").strip(),
        "source": r.get("source") or "",
        "url": r.get("url") or "",
    } for r in rows[:top] if (r.get("title") or "").strip()]


def snapshot(force: bool = False) -> dict:
    """热点快照。取不到的块返回空列表并记 errors。"""
    hit = _cache.get("snap")
    if hit and not force and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    err: dict[str, str] = {}

    def grab(name, fn, default):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001
            err[name] = str(e).splitlines()[0][:90]
            return default

    mood = grab("mood", lambda: limitup.snapshot(), {})
    concepts = grab("concepts", lambda: sector.boards("gn"), [])
    industries = grab("industries", lambda: sector.boards("hy"), [])
    stocks = grab("stocks", lambda: sina.market_list(1, 40, "changepercent"), [])
    news = grab("news", lambda: newsfeed.feed("all", limit=80), [])

    an = (mood or {}).get("analysis") or {}
    counts = an.get("counts") or {}
    zt, zb = counts.get("zt"), counts.get("zb")
    zb_rate = (zb / (zt + zb)) if (zt is not None and zb is not None
                                   and (zt + zb) > 0) else None
    out = {
        "ok": True,
        "ts": time.strftime("%H:%M:%S"),
        "day": (mood or {}).get("day") or "",
        "mood": {
            "zt": zt,
            "dt": counts.get("dt"),
            "zb": zb,
            "zb_rate": zb_rate,
            "max_board": an.get("max_board"),
            "promote": (an.get("promotion") or {}).get("rate"),
            "mood": an.get("mood") or "",
            "leaders": (an.get("sectors") or [])[:3],
        },
        "concepts": _rank_boards(concepts, 10),
        "industries": _rank_boards(industries, 8),
        "stocks": _strong_stocks(stocks, 20),
        "news": _news(news, 18),
        "errors": err,
    }
    _cache["snap"] = (time.monotonic(), out)
    return out
