"""A 股行情数据：东方财富公开接口。

只使用公开的行情查询接口：
* 不需要登录、不需要 App 私钥、不破解签名；
* 不设置伪装 Referer，不附加私有请求头；
* 批量请求之间限速（见 http.py）。

数据仅用于本地研究与学习。行情数据版权归原数据提供方，本项目不分发、
不再打包、不对外提供数据下载。
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from ..config import get
from ..utils import normalize_code, secid
from . import http

QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

LIST_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f62,f100,f102"
)
QUOTE_FIELDS = LIST_FIELDS
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

# 沪深 A 股：主板、创业板、科创板、北交所
MARKET_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

_colmap = {
    "f12": "code",
    "f14": "name",
    "f2": "price",
    "f3": "pct_chg",
    "f4": "change",
    "f5": "volume",
    "f6": "amount",
    "f7": "amplitude",
    "f8": "turnover",
    "f9": "pe_ttm",
    "f10": "volume_ratio",
    "f15": "high",
    "f16": "low",
    "f17": "open",
    "f18": "pre_close",
    "f20": "market_cap",
    "f21": "float_cap",
    "f23": "pb",
    "f62": "main_inflow",
    "f100": "industry",
    "f102": "region",
}


def _client() -> http.HttpClient:
    return http.HttpClient()


def _frame(rows: Iterable[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    if frame.empty:
        return frame
    frame = frame.rename(columns=_colmap)
    frame["code"] = frame["code"].map(normalize_code)
    for column in ("price", "pct_chg", "change", "volume", "amount", "amplitude",
                   "turnover", "pe_ttm", "volume_ratio", "high", "low", "open",
                   "pre_close", "market_cap", "float_cap", "pb", "main_inflow"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" in frame.columns:
        frame["volume"] = frame["volume"] * 100.0  # 手 -> 股
    return frame


def list_stocks(page_size: int | None = None, client: http.HttpClient | None = None) -> pd.DataFrame:
    """全市场快照（含价格、估值、市值、换手、行业）。"""
    client = client or _client()
    size = int(page_size or get("sync.page_size", 200))
    rows: list[dict] = []
    page = 1
    while True:
        data = client.get_json(LIST_URL, params={
            "pn": page,
            "pz": size,
            "po": 0,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": MARKET_FILTER,
            "fields": LIST_FIELDS,
        })
        payload = (data or {}).get("data") or {}
        diff = payload.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        total = int(payload.get("total") or 0)
        if len(rows) >= total or len(diff) < size:
            break
        page += 1
    frame = _frame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates("code").sort_values("code").reset_index(drop=True)
    return frame


def quotes(codes: Iterable[str], client: http.HttpClient | None = None) -> pd.DataFrame:
    """按代码批量取快照。"""
    client = client or _client()
    wanted = [normalize_code(code) for code in codes if normalize_code(code)]
    if not wanted:
        return pd.DataFrame()
    rows: list[dict] = []
    for start in range(0, len(wanted), 100):
        chunk = wanted[start:start + 100]
        data = client.get_json(QUOTE_URL, params={
            "secids": ",".join(secid(code) for code in chunk),
            "fltt": 2,
            "invt": 2,
            "fields": QUOTE_FIELDS,
        })
        rows.extend(((data or {}).get("data") or {}).get("diff") or [])
    return _frame(rows)


def kline(
    code: str,
    start: str = "2019-01-01",
    end: str = "2050-01-01",
    adjust: str = "qfq",
    index: bool = False,
    client: http.HttpClient | None = None,
) -> pd.DataFrame:
    """日线。adjust: qfq 前复权 / hfq 后复权 / none 不复权。"""
    client = client or _client()
    fqt = {"qfq": 1, "hfq": 2, "none": 0}.get(adjust, 1)
    data = client.get_json(KLINE_URL, params={
        "secid": secid(code, index=index),
        "klt": 101,
        "fqt": fqt,
        "beg": start.replace("-", ""),
        "end": end.replace("-", ""),
        "fields1": KLINE_FIELDS1,
        "fields2": KLINE_FIELDS2,
    })
    payload = (data or {}).get("data") or {}
    lines = payload.get("klines") or []
    rows = []
    for line in lines:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        rows.append({
            "date": parts[0],
            "open": float(parts[1]),
            "close": float(parts[2]),
            "high": float(parts[3]),
            "low": float(parts[4]),
            "volume": float(parts[5]) * 100.0,  # 手 -> 股
            "amount": float(parts[6]),
            "amplitude": float(parts[7]),
            "pct_chg": float(parts[8]),
            "change": float(parts[9]),
            "turnover": float(parts[10]),
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame.insert(0, "code", normalize_code(code))
    return frame


def index_quotes(codes: Iterable[str], client: http.HttpClient | None = None) -> pd.DataFrame:
    client = client or _client()
    wanted = [normalize_code(code) for code in codes if normalize_code(code)]
    if not wanted:
        return pd.DataFrame()
    data = client.get_json(QUOTE_URL, params={
        "secids": ",".join(secid(code, index=True) for code in wanted),
        "fltt": 2,
        "invt": 2,
        "fields": "f2,f3,f4,f12,f14,f15,f16,f17,f18",
    })
    return _frame(((data or {}).get("data") or {}).get("diff") or [])
