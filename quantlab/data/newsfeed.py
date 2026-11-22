# -*- coding: utf-8 -*-
"""资讯聚合:东财 7x24 快讯 + 本地 news_archive 档案。

两条来源的差别(决定页面怎么画):
  * 快讯:em.market_flash,带完整时间、来源、原文链接
  * 档案:data/newsdb 落的库,只有 kind/key/day/title,没有 URL、没有来源站点
    (newsdb 是为情绪因子设计的,不是为浏览设计的),所以这些行双击只能提示无原文

分类有哪几个:newsdb 只有 kind = policy / stock / notice 三种,
不虚构「宏观/行业/国际」这种它没有的维度。
"""
from __future__ import annotations

from . import em
from .store import store

# (显示名, key) —— 下拉框直接喂这个
CATEGORIES: list[tuple[str, str]] = [
    ("全部", "all"),
    ("快讯", "flash"),
    ("政策", "policy"),
    ("个股", "stock"),
    ("公告", "notice"),
]

KIND_LABEL = {"policy": "政策", "stock": "个股", "notice": "公告"}


def _flash(limit: int) -> list[dict]:
    """东财 7x24 快讯。失败返回空,不抛。"""
    try:
        items = em.market_flash(max(1, int(limit)))
    except Exception:
        return []
    out = []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({"time": it.get("time") or "", "cat": "快讯",
                    "title": title, "source": it.get("source") or "东财快讯",
                    "url": it.get("url") or "", "summary": it.get("summary") or ""})
    return out


def _archive(cat: str, limit: int) -> list[dict]:
    """本地资讯档案。cat='all' 时取全部 kind。"""
    kind = cat if cat in KIND_LABEL else None
    sql = "SELECT day,kind,key,title,late FROM news_archive"
    args: list = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY day DESC, late ASC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = store().conn.execute(sql, args).fetchall()
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        title = (r["title"] or "").strip()
        if not title:
            continue
        marker = (r["kind"], title)
        if marker in seen:          # 同一篇稿子被不同标的关键词搜到,去重
            continue
        seen.add(marker)
        src = "本地档案·" + KIND_LABEL.get(r["kind"], r["kind"] or "")
        if r["kind"] in ("stock", "notice") and r["key"]:
            src += f"({r['key']})"
        out.append({"time": (r["day"] or "")[:10], "cat": KIND_LABEL.get(r["kind"], "档案"),
                    "title": title, "source": src, "url": "", "summary": ""})
    return out


def feed(cat: str = "all", keyword: str = "", limit: int = 400) -> list[dict]:
    """按分类/关键词聚合资讯,时间倒序。

    关键词为空时不做过滤;命中与否由页面自己上色。
    """
    cat = cat or "all"
    kw = (keyword or "").strip()
    items: list[dict] = []
    if cat in ("all", "flash"):
        items += _flash(limit)
    if cat == "all" or cat in KIND_LABEL:
        items += _archive(cat, limit)
    if kw:
        low = kw.lower()
        items = [i for i in items if low in (i["title"] + " " + i.get("summary", "")).lower()]
    items.sort(key=lambda x: x.get("time") or "", reverse=True)
    return items[:max(1, int(limit))]


def stats() -> dict:
    """给页面顶部的说明用:本地档案各 kind 的条数。"""
    out = {}
    try:
        for r in store().conn.execute(
                "SELECT kind, COUNT(*) n FROM news_archive GROUP BY kind"):
            out[r["kind"]] = int(r["n"])
    except Exception:
        pass
    return out
