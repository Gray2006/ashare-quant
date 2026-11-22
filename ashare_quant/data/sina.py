"""新浪公开行情：只用于全市场股票列表，不抓实时行情。

hq.sinajs.cn 需要伪造 Referer 才能访问，本项目不使用；
这里用的是新浪财经公开的行情中心列表接口，无需登录、无需伪装请求头。
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from ..utils import normalize_code, safe_float
from . import http

LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
PAGE_SIZE = 100


def _row(item: dict) -> dict:
    code = normalize_code(item.get("code") or item.get("symbol"))
    return {
        "code": code,
        "name": str(item.get("name") or "").strip(),
        "price": safe_float(item.get("trade")),
        "pct_chg": safe_float(item.get("changepercent")),
        "volume": safe_float(item.get("volume")),
        "amount": safe_float(item.get("amount")),
        "pe_ttm": safe_float(item.get("per")),
        "pb": safe_float(item.get("pb")),
        "turnover": safe_float(item.get("turnoverratio")),
        "market_cap": (safe_float(item.get("mktcap")) or 0.0) * 1e4 or None,
        "float_cap": (safe_float(item.get("nmc")) or 0.0) * 1e4 or None,
        "source": "sina",
    }


def list_stocks(client: http.HttpClient | None = None,
                max_pages: int = 80) -> pd.DataFrame:
    """分页拉取沪深京 A 股列表，返回 code/name 及快照字段。"""
    client = client or http.HttpClient()
    rows: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        payload = client.get_json(
            LIST_URL,
            params={
                "page": page,
                "num": PAGE_SIZE,
                "sort": "symbol",
                "asc": 1,
                "node": "hs_a",
            },
        )
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict):
                continue
            parsed = _row(item)
            if parsed["code"]:
                rows.setdefault(parsed["code"], parsed)
        if len(payload) < PAGE_SIZE:
            break
    frame = pd.DataFrame(list(rows.values()))
    if not frame.empty:
        for column in ("price", "pct_chg", "volume", "amount", "pe_ttm", "pb",
                       "turnover", "market_cap", "float_cap"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame
