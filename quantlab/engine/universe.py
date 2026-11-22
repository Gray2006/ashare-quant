# -*- coding: utf-8 -*-
"""股票池:指数成分 / 全市场按成交额或市值筛选 / 自选股。

指数成分来自东财 datacenter 的 RPT_INDEX_TS_COMPONENT,TYPE 编码:
  1=沪深300  2=上证50  3=中证500  4=中证1000(未逐一验证,失败自动退化)
接口不可用时退化为「按市值取前 N」,并在来源里标注。
"""
from __future__ import annotations

import time

from ..config import cfg
from ..data import em, http
from ..utils import normalize_code

INDEX_TYPE = {
    "hs300": ("1", "沪深300"),
    "sz50": ("2", "上证50"),
    "zz500": ("3", "中证500"),
    "zz1000": ("4", "中证1000"),
}

DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 股票池缓存:run_once 每 10 秒跑一轮,每次都翻东财全市场太浪费。
# 30 秒 TTL,交易时段够用,又不会拿到隔夜旧数据。
_CACHE: dict = {}
_CACHE_TS: dict = {}
_TTL = 30.0


def index_members(kind: str = "hs300", limit: int = 500) -> list[dict]:
    key = ("idx", kind, int(limit))
    hit = _CACHE.get(key)
    # 指数成分变化慢,缓存 5 分钟
    if hit is not None and time.time() - _CACHE_TS.get(key, 0) < _TTL * 10:
        return list(hit)
    t = INDEX_TYPE.get(kind)
    if not t:
        return []
    rows: list[dict] = []
    page = 1
    while len(rows) < limit:
        params = {
            "reportName": "RPT_INDEX_TS_COMPONENT", "columns": "ALL",
            "filter": f'(TYPE="{t[0]}")', "pageNumber": str(page), "pageSize": "500",
            "source": "WEB", "client": "WEB",
        }
        try:
            js = http.get_json(DATACENTER, params=params,
                               headers={"Referer": "https://data.eastmoney.com/"})
        except Exception:
            break
        data = (js or {}).get("result") or {}
        batch = data.get("data") or []
        if not batch:
            break
        for r in batch:
            rows.append({
                "code": r.get("SECURITY_CODE"), "name": r.get("SECURITY_NAME_ABBR"),
                "price": r.get("CLOSE_PRICE"), "pct_chg": r.get("CHANGE_RATE"),
                "market_cap": (r.get("FREE_CAP") or 0) * 1e8 or None,
                "weight": r.get("WEIGHT"),
            })
        page += 1
        if page > 4:
            break
    _CACHE[key] = list(rows[:limit])
    _CACHE_TS[key] = time.time()
    return rows[:limit]


def top_by(kind: str = "amount", limit: int = 100,
           min_cap: float = 3e9, fetch: int | None = None) -> list[dict]:
    """按成交额(f6)或总市值(f20)取前 N 只(剔除 ST/退市/北交所小票)。

    fetch 是多抓的候选条数:过滤 ST/小市值会筛掉一部分,所以默认多抓 3 倍。
    全市场池自己知道要多少,显式传 fetch,避免 3 倍放大成 60 页请求。
    """
    key = ("top", kind, int(limit), float(min_cap), int(fetch or 0))
    hit = _CACHE.get(key)
    if hit is not None and time.time() - _CACHE_TS.get(key, 0) < _TTL:
        return list(hit)
    fid = "f20" if kind == "market_cap" else "f6"
    want = int(fetch) if fetch else max(limit * 3, 400)
    rows = em.market_list_all(fid, asc=False, limit=want)
    out = []
    for r in rows:
        name = r.get("name") or ""
        if "ST" in name or "退" in name or not r.get("price"):
            continue
        if (r.get("market_cap") or 0) < min_cap:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    _CACHE[key] = list(out)
    _CACHE_TS[key] = time.time()
    return out


def build(kind: str = "hs300", limit: int = 100) -> tuple[list[dict], str]:
    """返回 (股票列表, 来源说明)。"""
    kind = (kind or "hs300").lower()
    if kind in ("watchlist", "自选"):
        codes = cfg().watchlist
        data = em.quotes(codes)
        return ([{"code": c, "name": (data.get(c) or {}).get("name", "")} for c in codes],
                "自选股")
    if kind in INDEX_TYPE:
        rows = index_members(kind, limit=max(limit, 300))
        if rows:
            label = INDEX_TYPE[kind][1]
            return rows[:limit], f"{label}成分股(共 {len(rows)} 只可用)"
        rows = top_by("market_cap", limit)
        return rows, f"指数成分获取失败,退化为市值前 {len(rows)} 只"
    if kind in ("all", "全市场"):
        # 全市场池:不做市值门槛,按成交额从整个 A 股里取。
        # 策略只有 n_stocks 只的名额,所以取 limit×4 作为候选面(上限 2000),
        # 不真去翻 5400 只 —— 那要 50 多页请求,选股一次几十秒。
        want = min(max(limit * 4, 400), 2000)
        # fetch 就按 want 来,别再 ×3 —— 否则 2000 变成 6000,翻 60 页,选股几十秒
        rows = top_by("amount", want, min_cap=0, fetch=want)
        return rows, f"全市场成交额前 {len(rows)} 只(候选面)"
    rows = top_by("amount", limit)
    return rows, f"成交额前 {len(rows)} 只"


def codes_of(kind: str = "hs300", limit: int = 100) -> list[str]:
    rows, _ = build(kind, limit)
    return [normalize_code(r["code"]) for r in rows if r.get("code")]


def names_of(kind: str = "hs300", limit: int = 100) -> dict[str, str]:
    rows, _ = build(kind, limit)
    return {normalize_code(r["code"]): r.get("name", "") for r in rows if r.get("code")}
