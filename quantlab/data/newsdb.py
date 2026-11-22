# -*- coding: utf-8 -*-
"""新闻 / 政策 / 公告档案:落库 + 按日聚合成可校准的情绪序列。

为什么不直接算:预测要校准就得有历史。东财搜索接口能翻页取到带日期的
历史新闻(个股约 2~6 个月,公告约一年),但每次都要重抓,所以第一次抓到
就存进 SQLite,以后增量补。取不到历史的部分(政策关键词只能回溯约 10 天)
会返回空序列,上层自动降级成「无数据」,不会假装算过。

时序对齐:收盘价因子只能用收盘前已知的信息。一条 17:00 发的新闻当天收盘
时还不知道,所以记 late=1,归到下一个交易日。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time

from . import em, http
from .store import store

# ---------------------------------------------------------------- 情绪词表

BULL_WORDS = (
    "涨停", "大涨", "利好", "增持", "买入", "上调", "超预期", "创新高", "新高",
    "中标", "签约", "回购", "分红", "预增", "扭亏", "突破", "获批", "涨价",
    "扩产", "订单", "龙头", "放量", "净流入", "加仓", "推荐", "受益",
)
BEAR_WORDS = (
    "跌停", "大跌", "利空", "减持", "卖出", "下调", "低于预期", "创新低", "新低",
    "亏损", "预亏", "退市", "立案", "处罚", "违规", "质押", "违约", "商誉",
    "诉讼", "冻结", "解禁", "停产", "爆雷", "净流出", "减仓", "风险", "调查",
)

POLICY_WORDS = (
    "降准", "降息", "货币政策", "财政政策", "国常会", "政治局", "证监会", "央行",
    "财政部", "发改委", "印花税", "监管", "新规", "改革", "补贴", "专项债",
    "产业政策", "试点", "指导意见", "刺激", "稳增长", "扩内需", "并购重组",
)

# 公告标题里的强信号(比新闻更硬:增减持、解禁、业绩预告都是实打实的)
GOOD_NOTICE = ("增持", "回购", "预增", "扭亏", "分红", "中标", "签约", "获批",
               "重大合同", "战略性合作", "复牌")
BAD_NOTICE = ("减持", "解禁", "质押", "预亏", "立案", "处理", "处罚", "违规",
              "诉讼", "仲裁", "退市", "风险警示", "商誉减值", "终止", "停牌")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_archive (
    kind TEXT NOT NULL,      -- stock / policy / notice
    key  TEXT NOT NULL,      -- 股票代码 / 关键词 / 代码
    day  TEXT NOT NULL,      -- 发布日 YYYY-MM-DD
    late INTEGER DEFAULT 0,  -- 15:00 之后发的,归到下一个交易日
    sent REAL,               -- 情绪 -1~1
    title TEXT,
    uid  TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_news_lookup ON news_archive(kind, key, day);
CREATE TABLE IF NOT EXISTS news_meta (
    key TEXT PRIMARY KEY, ts REAL, note TEXT
);
"""

_LOCK = threading.Lock()
_READY = False
TTL = 6 * 3600          # 同一标的 6 小时内不重复抓
PAGE_SLEEP = 0.12


def _ensure() -> None:
    """建表。第一次调用付一次代价,之后直接返回。"""
    global _READY
    if _READY:
        return
    with _LOCK:
        if _READY:
            return
        with store().conn as c:
            c.executescript(_SCHEMA)
        _READY = True


# ---------------------------------------------------------------- 打分

def _hits(text: str, words) -> int:
    return sum(1 for w in words if w in text)


def score_text(text: str) -> float:
    """粗糙但可复现的情绪分:多空词命中数之差除以总数。"""
    t = text or ""
    b, s = _hits(t, BULL_WORDS), _hits(t, BEAR_WORDS)
    if b + s == 0:
        return 0.0
    return (b - s) / (b + s)


def score_notice(title: str) -> tuple[float, int]:
    """公告分 + 是否政策相关。"""
    t = title or ""
    good, bad = _hits(t, GOOD_NOTICE), _hits(t, BAD_NOTICE)
    pol = 1 if _hits(t, POLICY_WORDS) else 0
    if good + bad == 0:
        return 0.0, pol
    return (good - bad) / (good + bad), pol


def _is_late(ts: str) -> int:
    """15:00 之后发的新闻,当天收盘时还看不到。"""
    m = re.search(r"(\d{2}):(\d{2})", ts or "")
    if not m:
        return 0
    hh, mm = int(m.group(1)), int(m.group(2))
    return 1 if (hh, mm) > (15, 0) else 0


def _uid(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode("utf-8", "ignore")).hexdigest()


# ---------------------------------------------------------------- 抓取入库

def _save(rows: list[tuple]) -> int:
    if not rows:
        return 0
    _ensure()
    n = 0
    with store().conn as c:
        for kind, key, day, late, sent, title, uid in rows:
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO news_archive"
                    "(kind,key,day,late,sent,title,uid) VALUES (?,?,?,?,?,?,?)",
                    (kind, key, day, late, sent, title[:200], uid))
                n += int(cur.rowcount or 0)
            except Exception:
                pass
    return n


def _meta(key: str) -> float:
    _ensure()
    try:
        r = store().conn.execute("SELECT ts FROM news_meta WHERE key=?", (key,)).fetchone()
        return float(r["ts"]) if r else 0.0
    except Exception:
        return 0.0


def _stamp(key: str, note: str = "") -> None:
    _ensure()
    try:
        with store().conn as c:
            c.execute("INSERT OR REPLACE INTO news_meta(key,ts,note) VALUES (?,?,?)",
                      (key, time.time(), note[:80]))
    except Exception:
        pass


# 东财搜索:python-requests 会拿到降级结果,curl 才稳
_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"


def _search(kw: str, page: int, size: int = 100) -> list[dict]:
    payload = {
        "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": page, "pageSize": size,
                                       "preTag": "<em>", "postTag": "</em>"}},
    }
    params = {"cb": "cb", "param": json.dumps(payload, ensure_ascii=False)}
    headers = {"Referer": "https://so.eastmoney.com/"}
    try:
        txt = http.get_text_curl(_SEARCH_URL, params=params, headers=headers)
    except Exception:
        txt = http.get_text(_SEARCH_URL, params=params, headers=headers)
    js = http.parse_jsonp(txt) if txt and not txt.lstrip().startswith("{") else json.loads(txt or "{}")
    items = (((js or {}).get("result") or {}).get("cmsArticleWebOld")) or []
    out = []
    for it in items:
        out.append({"title": em._strip_tag(it.get("title")),
                    "summary": em._strip_tag(it.get("content")),
                    "time": it.get("date") or "",
                    "url": it.get("url") or ""})
    return out


def fetch_stock(code: str, pages: int = 2, force: bool = False) -> int:
    """个股新闻入库。返回新增条数。"""
    _ensure()
    mk = f"stock:{code}"
    if not force and time.time() - _meta(mk) < TTL:
        return 0
    rows = []
    for p in range(1, max(1, pages) + 1):
        try:
            items = _search(code, p)
        except Exception:
            break
        if not items:
            break
        for it in items:
            ts = it.get("time") or ""
            day = ts[:10]
            if len(day) != 10:
                continue
            sent = score_text(f"{it.get('title')} {it.get('summary')}")
            rows.append(("stock", code, day, _is_late(ts), sent,
                         it.get("title") or "", _uid("stock", code, it.get("url") or ts + (it.get("title") or ""))))
        time.sleep(PAGE_SLEEP)
    n = _save(rows)
    _stamp(mk, f"{len(rows)} 条")
    return n


def fetch_policy(pages: int = 8, force: bool = False) -> int:
    """政策关键词新闻入库。接口翻页上限约 10 页,历史不长。"""
    _ensure()
    mk = "policy:*"
    if not force and time.time() - _meta(mk) < TTL:
        return 0
    rows = []
    for kw in POLICY_WORDS[:10]:
        for p in range(1, max(1, pages) + 1):
            try:
                items = _search(kw, p)
            except Exception:
                break
            if not items:
                break
            for it in items:
                ts = it.get("time") or ""
                day = ts[:10]
                if len(day) != 10:
                    continue
                sent = score_text(f"{it.get('title')} {it.get('summary')}")
                # 同一篇稿子会被多个关键词搜到,uid 不带关键词才能真正去重
                rows.append(("policy", kw, day, _is_late(ts), sent,
                             it.get("title") or "", _uid("policy", it.get("url") or ts + (it.get("title") or ""))))
            time.sleep(PAGE_SLEEP)
    n = _save(rows)
    _stamp(mk, f"{len(rows)} 条 / {len(POLICY_WORDS[:10])} 关键词")
    return n


def fetch_notices(code: str, count: int = 100, force: bool = False) -> int:
    """公告入库(带日期,历史通常一年以上)。"""
    _ensure()
    mk = f"notice:{code}"
    if not force and time.time() - _meta(mk) < TTL:
        return 0
    try:
        items = em.announcements(code, count)
    except Exception:
        items = []
    rows = []
    for it in items:
        day = (it.get("date") or "")[:10]
        if len(day) != 10:
            continue
        title = it.get("title") or ""
        sent, pol = score_notice(title)
        rows.append(("notice", code, day, 0, sent, title,
                     _uid("notice", code, it.get("url") or day + title)))
    n = _save(rows)
    _stamp(mk, f"{len(rows)} 条")
    return n


# ---------------------------------------------------------------- 按日聚合

def _rows(kind: str, key: str, since: str) -> list[dict]:
    _ensure()
    try:
        cur = store().conn.execute(
            "SELECT day,late,sent FROM news_archive WHERE kind=? AND key=? AND day>=?",
            (kind, key, since))
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def daily(kind: str, key: str = "*", days: int = 800) -> dict[str, dict]:
    """按自然日聚合 → {day: {n, sent, late_n, late_sent}}。

    key='*' 表示该 kind 下所有关键词合并(政策用)。
    """
    since = (dt.date.today() - dt.timedelta(days=int(days * 1.6) + 10)).isoformat()
    if key == "*":
        _ensure()
        try:
            cur = store().conn.execute(
                "SELECT day,late,sent FROM news_archive WHERE kind=? AND day>=?",
                (kind, since))
            rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            rows = []
    else:
        rows = _rows(kind, key, since)
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["day"], {"n": 0, "sent": 0.0, "late_n": 0, "late_sent": 0.0})
        s = float(r.get("sent") or 0.0)
        if int(r.get("late") or 0):
            d["late_n"] += 1
            d["late_sent"] += s
        else:
            d["n"] += 1
            d["sent"] += s
    for d in out.values():
        if d["n"]:
            d["sent"] /= d["n"]
        if d["late_n"]:
            d["late_sent"] /= d["late_n"]
    return out


def coverage(kind: str, key: str = "*") -> tuple[int, str, str]:
    """有多少天数据、最早/最晚日期。用来判断够不够做因子。"""
    _ensure()
    try:
        if key == "*":
            r = store().conn.execute(
                "SELECT COUNT(DISTINCT day) n, MIN(day) a, MAX(day) b FROM news_archive WHERE kind=?",
                (kind,)).fetchone()
        else:
            r = store().conn.execute(
                "SELECT COUNT(DISTINCT day) n, MIN(day) a, MAX(day) b FROM news_archive "
                "WHERE kind=? AND key=?", (kind, key)).fetchone()
        return (int(r["n"] or 0), r["a"] or "", r["b"] or "") if r else (0, "", "")
    except Exception:
        return 0, "", ""


def stats() -> dict:
    _ensure()
    out = {}
    try:
        for r in store().conn.execute(
                "SELECT kind, COUNT(*) n, COUNT(DISTINCT key) k, MIN(day) a, MAX(day) b "
                "FROM news_archive GROUP BY kind"):
            out[r["kind"]] = {"n": r["n"], "keys": r["k"], "from": r["a"], "to": r["b"]}
    except Exception:
        pass
    return out


def clear() -> None:
    _ensure()
    with store().conn as c:
        c.execute("DELETE FROM news_archive")
        c.execute("DELETE FROM news_meta")


def prefetch(codes: list[str], pages: int = 3, notices: int = 60,
             workers: int = 4, progress=None) -> dict:
    """并发预抓一批票的新闻+公告。HTTP 是 IO 等待,串行抓 60 只太慢。

    返回 {code: (新闻新增, 公告新增)}。抓失败不抛,记为 0。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    codes = [c for c in dict.fromkeys(codes) if c]
    out: dict[str, tuple[int, int]] = {}
    if not codes:
        return out

    def one(code: str) -> tuple[str, int, int]:
        a = b = 0
        try:
            a = fetch_stock(code, pages=pages)
        except Exception:
            pass
        try:
            b = fetch_notices(code, notices)
        except Exception:
            pass
        return code, a, b

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futs = {ex.submit(one, c): c for c in codes}
        for fu in as_completed(futs):
            try:
                code, a, b = fu.result()
                out[code] = (a, b)
            except Exception:
                out[futs[fu]] = (0, 0)
            done += 1
            if progress:
                try:
                    progress(done, len(codes))
                except Exception:
                    pass
    return out

