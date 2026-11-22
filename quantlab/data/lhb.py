# -*- coding: utf-8 -*-
"""龙虎榜:东财 datacenter 每日榜单 + 买卖席位明细。

接口(2026-11 实测可用):
  RPT_DAILYBILLBOARD_DETAILSNEW   每日上榜个股,按 TRADE_DATE 过滤
  RPT_BILLBOARD_DAILYDETAILSBUY   买入席位(该行的 SELL 常为 null)
  RPT_BILLBOARD_DAILYDETAILSSELL  卖出席位(该行的 BUY 可能有值)

为什么席位要合并:
  同一营业部可能同时出现在买入榜和卖出榜。两份报表里 BUY / SELL 是
  该席位当日的全量金额(不是本榜金额),直接相加会翻倍,所以按
  (营业部代码, 营业部名) 合并并取较大值,净额 = 买 - 卖。
"""
from __future__ import annotations

import datetime as dt

from ..utils import normalize_code, safe_float
from . import http

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HDR = {"Referer": "https://data.eastmoney.com/stock/tradedetail.html"}


def _get(params: dict) -> dict:
    base = {"columns": "ALL", "pageNumber": "1", "pageSize": "500",
            "source": "WEB", "client": "WEB"}
    base.update(params)
    try:
        js = http.get_json(_URL, params=base, headers=_HDR)
    except Exception:
        js = http.get_json_curl(_URL, params=base, headers=_HDR)
    return js if isinstance(js, dict) else {}


def _rows(js: dict) -> list[dict]:
    res = (js or {}).get("result") or {}
    return res.get("data") or []


# ---------------------------------------------------------------- 交易日

def recent_days(n: int = 5, end: dt.date | None = None) -> list[str]:
    """最近 n 个工作日(新→旧),跳过周末。

    只跳周末,不查法定节假日 —— 所以页面对「空数据」必须给提示;
    节假日那天接口返回空,不是错误。
    """
    d = end or dt.date.today()
    out: list[str] = []
    guard = 0
    while len(out) < max(1, n) and guard < 400:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
        guard += 1
    return out


# ---------------------------------------------------------------- 榜单

def board(day: str, limit: int = 300) -> list[dict]:
    """某日龙虎榜明细。day 形如 '2026-11-05';非交易日返回 []。"""
    day = str(day or "")[:10]
    if len(day) != 10:
        return []
    js = _get({"reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
               "filter": f"(TRADE_DATE='{day}')",
               "pageSize": str(max(1, min(500, int(limit)))),
               "sortColumns": "BILLBOARD_NET_AMT", "sortTypes": "-1"})
    out = []
    for r in _rows(js):
        # 同一只票可能因多个「上榜原因」出现多行,页面照实展示,不去重
        out.append({
            "code": r.get("SECURITY_CODE") or "",
            "name": r.get("SECURITY_NAME_ABBR") or "",
            "close": safe_float(r.get("CLOSE_PRICE")),
            "change_pct": safe_float(r.get("CHANGE_RATE")),
            "reason": r.get("EXPLANATION") or r.get("EXPLAIN") or "",
            "net": safe_float(r.get("BILLBOARD_NET_AMT")),
            "buy": safe_float(r.get("BILLBOARD_BUY_AMT")),
            "sell": safe_float(r.get("BILLBOARD_SELL_AMT")),
            "turnover": safe_float(r.get("TURNOVERRATE")),
            "amount": safe_float(r.get("ACCUM_AMOUNT")),
            "market": r.get("TRADE_MARKET") or r.get("MARKET") or "",
            "date": (r.get("TRADE_DATE") or day)[:10],
        })
    return out


# ---------------------------------------------------------------- 席位

def seats(code: str, day: str) -> list[dict]:
    """某只票某日的买卖席位(已合并)。返回 [{'name','buy','sell','net'}]。"""
    code = normalize_code(code)
    day = str(day or "")[:10]
    if not code or len(day) != 10:
        return []
    merged: dict[tuple, dict] = {}
    for report in ("RPT_BILLBOARD_DAILYDETAILSBUY",
                   "RPT_BILLBOARD_DAILYDETAILSSELL"):
        try:
            js = _get({"reportName": report,
                       "filter": f"(TRADE_DATE='{day}')(SECURITY_CODE=\"{code}\")",
                       "pageSize": "100"})
        except Exception:
            continue
        for r in _rows(js):
            name = (r.get("OPERATEDEPT_NAME") or "").strip() or "未知席位"
            key = (r.get("OPERATEDEPT_CODE") or "", name)
            buy = safe_float(r.get("BUY")) or 0.0
            sell = safe_float(r.get("SELL")) or 0.0
            cur = merged.get(key)
            if cur is None:
                merged[key] = {"name": name, "buy": buy, "sell": sell}
            else:
                cur["buy"] = max(cur["buy"], buy)
                cur["sell"] = max(cur["sell"], sell)
    out = []
    for v in merged.values():
        out.append({"name": v["name"], "buy": v["buy"], "sell": v["sell"],
                    "net": v["buy"] - v["sell"]})
    out.sort(key=lambda x: abs(x["net"]), reverse=True)
    return out


def empty_hint(day: str) -> str:
    """空数据时给用户看的明确原因。"""
    return (f"东方财富接口返回空:{day} 可能不是交易日(节假日/停市),"
            "或接口变更/限流。换个日期或点刷新重试。")
