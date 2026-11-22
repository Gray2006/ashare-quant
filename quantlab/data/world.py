# -*- coding: utf-8 -*-
"""外盘行情与国际新闻时政。

行情分两套解析:腾讯的美股/港股字段同 A 股(v_ 格式,~ 分隔),
新浪的国际指数(gb_*)、期货/贵金属(hf_*)、汇率(fx_*)各是一套逗号格式,
统一成 {code,name,price,pre_close,change,change_pct,high,low,time,kind}。

新闻做三级级联:东财 7x24 → 新浪直播 → 金十;统一成
{time,title,summary,url,source,tag},按时间倒序、标题去重。
"""
from __future__ import annotations

import re
import time

from ..utils import safe_float
from . import http

TX_HQ = "https://qt.gtimg.cn/q="
SINA_HQ = "https://hq.sinajs.cn/list="
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
EM_724 = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
SINA_ZB = "https://zhibo.sina.com.cn/api/zhibo/feed"
JIN10 = "https://flash-api.jin10.com/get_flash_list"
JIN10_HEADERS = {"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0",
                 "Origin": "https://www.jin10.com", "Referer": "https://www.jin10.com/"}

# code: (源, 符号, 名称, 类别)
#   源 tx     —— 腾讯 v_ 格式(~ 分隔,美股/港股)
#   源 gb     —— 新浪国际指数 gb_$xxx
#   源 hf     —— 新浪期货/贵金属 hf_xxx
#   源 fx     —— 新浪外汇 fx_xxx
#   源 hk     —— 新浪港股 hkXXX
OVERSEAS: dict[str, tuple[str, str, str, str]] = {
    "dji": ("tx", "usDJI", "道琼斯", "美股"),
    "ixic": ("tx", "usIXIC", "纳斯达克", "美股"),
    "spx": ("tx", "usSPX", "标普500", "美股"),
    "ndx": ("tx", "usNDX", "纳指100", "美股"),
    "vix": ("gb", "gb_$vix", "VIX恐慌", "美股"),
    "hsi": ("tx", "hkHSI", "恒生指数", "港股"),
    "hstech": ("tx", "hkHSTECH", "恒生科技", "港股"),
    "hscai": ("tx", "hkHSCEI", "国企指数", "港股"),
    "nikkei": ("gb", "gb_$n225", "日经225", "亚太"),
    "kospi": ("gb", "gb_$kospi", "韩国综合", "亚太"),
    "ftse": ("gb", "gb_$ftse", "英国富时", "欧洲"),
    "dax": ("gb", "gb_$dax", "德国DAX", "欧洲"),
    "gold": ("hf", "hf_GC", "COMEX黄金", "商品"),
    "xau": ("hf", "hf_XAU", "伦敦金现", "商品"),
    "silver": ("hf", "hf_SI", "COMEX白银", "商品"),
    "oil": ("hf", "hf_CL", "WTI原油", "商品"),
    "brent": ("hf", "hf_OIL", "布伦特原油", "商品"),
    "copper": ("hf", "hf_CAD", "LME铜", "商品"),
    "usdcny": ("fx", "fx_susdcny", "美元/人民币", "汇率"),
    "usdcnh": ("fx", "fx_susdcnh", "美元/离岸人民币", "汇率"),
    "usdjpy": ("fx", "fx_susdjpy", "美元/日元", "汇率"),
    "eurusd": ("fx", "fx_seurusd", "欧元/美元", "汇率"),
    "dxy": ("fx", "fx_sdxy", "美元指数", "汇率"),
}

# 腾讯能查的美股个股(按需扩展)
US_STOCKS = ["usTSLA", "usAAPL", "usNVDA", "usMSFT", "usAMD"]

_NEWS_TTL = 60.0
_QUOTE_TTL = 3.0


# ------------------------------------------------------------------ 行情

def _fmt_pct(price: float | None, pre: float | None) -> tuple[float | None, float | None]:
    """(涨跌额, 涨跌幅%)。统一从现价/昨收推导,不依赖接口的字段位置。"""
    if not price or not pre:
        return None, None
    return price - pre, (price / pre - 1) * 100


def _tx_parse(txt: str) -> dict[str, dict]:
    """腾讯 v_ 格式:~ 分隔。索引 1 名称 / 3 现价 / 4 昨收 / 5 今开 / 33 最高 / 34 最低。"""
    out: dict[str, dict] = {}
    for line in txt.split(";"):
        line = line.strip()
        if '="' not in line:
            continue
        sym = line.split("v_", 1)[-1].split("=", 1)[0]
        payload = line.split('="', 1)[1].rsplit('"', 1)[0]
        p = payload.split("~")
        if len(p) < 6:
            continue
        price, pre = safe_float(p[3]), safe_float(p[4])
        chg, pct = _fmt_pct(price, pre)
        if pct is None and len(p) > 32:
            pct, chg = safe_float(p[32]), safe_float(p[31])
        out[sym] = {
            "symbol": sym, "name": p[1], "price": price, "pre_close": pre,
            "open": safe_float(p[5]),
            "high": safe_float(p[33]) if len(p) > 33 else None,
            "low": safe_float(p[34]) if len(p) > 34 else None,
            "change": chg, "change_pct": pct,
            "time": p[30] if len(p) > 30 else "", "source": "tx",
        }
    return out


def _gb_parse(p: list[str]) -> dict | None:
    """新浪国际指数:0 名称 1 现价 2 涨跌幅 3 时间 4 涨跌额 6 最高 7 最低 26 昨收。"""
    if len(p) < 5 or not p[1]:
        return None
    price = safe_float(p[1])
    chg = safe_float(p[4])
    pre = safe_float(p[26]) if len(p) > 26 else (price - chg if chg is not None else None)
    _c, pct = _fmt_pct(price, pre)
    if pct is None:
        pct = safe_float(p[2])
    return {
        "name": p[0], "price": price, "pre_close": pre, "open": None,
        "high": safe_float(p[6]) if len(p) > 6 else None,
        "low": safe_float(p[7]) if len(p) > 7 else None,
        "change": chg, "change_pct": pct,
        "time": p[3] if len(p) > 3 else "", "source": "sina",
    }


def _hf_parse(p: list[str]) -> dict | None:
    """新浪期货:0 现价 1 涨跌 4 最高 5 最低 6 时间 7 昨结 8 昨收 12 日期 13 名称。"""
    if len(p) < 9:
        return None
    price = safe_float(p[0])
    pre = safe_float(p[8]) or safe_float(p[7])
    chg, pct = _fmt_pct(price, pre)
    return {
        "name": p[13] if len(p) > 13 else "", "price": price, "pre_close": pre,
        "open": None, "high": safe_float(p[4]), "low": safe_float(p[5]),
        "change": chg, "change_pct": pct,
        "time": ("%s %s" % (p[12], p[6]) if len(p) > 12 else (p[6] if len(p) > 6 else "")),
        "source": "sina",
    }


def _fx_parse(p: list[str]) -> dict | None:
    """新浪外汇:0 时间 1 买价 2 卖价 5 昨收 6 今开 7 最低 8 最高 9 名称。"""
    if len(p) < 9:
        return None
    price = safe_float(p[2]) or safe_float(p[1])
    pre = safe_float(p[5])
    chg, pct = _fmt_pct(price, pre)
    return {
        "name": p[9] if len(p) > 9 else "", "price": price, "pre_close": pre,
        "open": safe_float(p[6]), "high": safe_float(p[8]), "low": safe_float(p[7]),
        "change": chg, "change_pct": pct,
        "time": p[0] if p[0] else "", "source": "sina",
    }


def _hk_parse(p: list[str]) -> dict | None:
    """新浪港股:0 代码 1 名称 2 今开 3 昨收 4 最高 5 最低 6 现价 7 涨跌 8 涨跌幅。"""
    if len(p) < 9 or not p[6]:
        return None
    price, pre = safe_float(p[6]), safe_float(p[3])
    chg, pct = _fmt_pct(price, pre)
    if pct is None:
        chg, pct = safe_float(p[7]), safe_float(p[8])
    return {
        "name": p[1], "price": price, "pre_close": pre, "open": safe_float(p[2]),
        "high": safe_float(p[4]), "low": safe_float(p[5]),
        "change": chg, "change_pct": pct,
        "time": ("%s %s" % (p[17], p[18]) if len(p) > 18 else ""), "source": "sina",
    }


_PARSERS = {"gb": _gb_parse, "hf": _hf_parse, "fx": _fx_parse, "hk": _hk_parse}


def _sina_parse(txt: str) -> dict[str, dict]:
    """新浪多品类:按符号前缀分派解析器(fx_ 行绝不能被 hf_ 规则吃掉)。"""
    out: dict[str, dict] = {}
    for line in txt.split(";"):
        line = line.strip()
        if '="' not in line:
            continue
        sym = line.split("hq_str_", 1)[-1].split("=", 1)[0]
        payload = line.split('="', 1)[1].rsplit('"', 1)[0]
        if not payload:
            continue
        p = payload.split(",")
        if sym.startswith("gb_"):
            fn = _PARSERS["gb"]
        elif sym.startswith("hf_"):
            fn = _PARSERS["hf"]
        elif sym.startswith("fx_"):
            fn = _PARSERS["fx"]
        elif sym.startswith("hk") or sym.startswith("rt_hk"):
            fn = _PARSERS["hk"]
        else:
            continue
        row = fn(p)
        if row:
            row["symbol"] = sym
            out[sym] = row
    return out


def overseas(codes: list[str] | None = None) -> list[dict]:
    """外盘快照。腾讯抓美股/港股,新浪抓国际指数/期货/汇率。"""
    keys = [k for k in (codes or list(OVERSEAS)) if k in OVERSEAS]
    tx_syms = [OVERSEAS[k][1] for k in keys if OVERSEAS[k][0] == "tx"]
    sina_syms = [OVERSEAS[k][1] for k in keys if OVERSEAS[k][0] != "tx"]

    found: dict[str, dict] = {}
    if tx_syms:
        try:
            found.update(_tx_parse(http.get_text(TX_HQ + ",".join(tx_syms),
                                                 encoding="gbk", retries=2, timeout=6)))
        except Exception:
            pass
    if sina_syms:
        try:
            found.update(_sina_parse(http.get_text(SINA_HQ + ",".join(sina_syms),
                                                   headers=SINA_HEADERS, encoding="gbk",
                                                   retries=2, timeout=6)))
        except Exception:
            pass

    out = []
    for k in keys:
        _src, sym, name, kind = OVERSEAS[k]
        r = found.get(sym)
        if not r:
            continue
        it = dict(r)
        it["code"] = k
        it["name"] = name
        it["kind"] = kind
        out.append(it)
    return out


def us_stocks(symbols: list[str] | None = None) -> list[dict]:
    """美股个股快照(腾讯 us*)。"""
    syms = symbols or US_STOCKS
    if not syms:
        return []
    try:
        txt = http.get_text(TX_HQ + ",".join(syms), encoding="gbk", retries=2, timeout=6)
    except Exception:
        return []
    rows = list(_tx_parse(txt).values())
    for r in rows:
        r["code"] = r["symbol"].replace("us", "").upper()
        r["kind"] = "美股个股"
    return rows


def overseas_cached(codes: list[str] | None = None) -> list[dict]:
    return http.cached("world:overseas:%s" % (",".join(codes or []) or "all"),
                       _QUOTE_TTL, lambda: overseas(codes))


# ------------------------------------------------------------------ 新闻

_EM_TAG = re.compile(r"<[^>]+>")
_KEEP_TAGS = [
    ("美联储", ("美联储", "鲍威尔", "议息", "加息", "降息", "FOMC")),
    ("关税贸易", ("关税", "贸易", "出口管制", "反倾销", "制裁")),
    ("地缘政治", ("俄乌", "中东", "以色列", "伊朗", "冲突", "停火", "大选", "北约")),
    ("宏观数据", ("CPI", "PPI", "非农", "GDP", "PMI", "社融", "降准", "MLF", "LPR")),
    ("A股市场", ("A股", "沪深", "创业板", "科创板", "北向", "证监会", "印花税")),
    ("原油能源", ("原油", "OPEC", "油价", "天然气", "成品油")),
    ("黄金贵金属", ("黄金", "金价", "白银", "贵金属")),
    ("美股", ("美股", "纳斯达克", "标普", "道琼斯", "英伟达", "特斯拉")),
    ("港股", ("港股", "恒生", "恒指", "南向")),
]


def _tag_of(text: str) -> str:
    for tag, kws in _KEEP_TAGS:
        for kw in kws:
            if kw.lower() in text.lower():
                return tag
    return "综合"


def _clean(s: str) -> str:
    s = _EM_TAG.sub("", s or "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def _news_em(limit: int) -> list[dict]:
    txt = http.get_text(EM_724, timeout=8, retries=2)
    js = http.parse_jsonp(txt)
    items = (js or {}).get("LivesList") or []
    out = []
    for it in items[:limit]:
        title = _clean(it.get("title") or it.get("digest") or "")
        body = _clean(it.get("digest") or "")
        if not title:
            continue
        out.append({
            "time": it.get("showtime") or it.get("sort") or "",
            "title": title[:120],
            "summary": body[:200],
            "url": it.get("url_w") or it.get("url_m") or "",
            "source": "东财7x24",
        })
    return out


def _news_sina(limit: int) -> list[dict]:
    js = http.get_json(SINA_ZB, params={"page": 1, "page_size": min(limit, 50),
                                        "zhibo_id": 152, "tag_id": 0,
                                        "dire": "f", "dpc": 1},
                       headers=SINA_HEADERS, timeout=8, retries=2)
    items = (((js or {}).get("result") or {}).get("data") or {}).get("feed", {}).get("list") or []
    out = []
    for it in items[:limit]:
        text = _clean(it.get("rich_text") or "")
        if not text:
            continue
        out.append({
            "time": it.get("create_time") or "",
            "title": text[:120],
            "summary": text[:240],
            "url": it.get("docurl") or "",
            "source": "新浪直播",
        })
    return out


def _news_jin10(limit: int) -> list[dict]:
    js = http.get_json(JIN10, params={"channel": -8200, "vip": 1},
                       headers=JIN10_HEADERS, timeout=8, retries=2)
    items = (js or {}).get("data") or []
    out = []
    for it in items[:limit]:
        d = it.get("data") or {}
        text = _clean(d.get("content") or d.get("title") or "")
        if not text:
            continue
        out.append({
            "time": it.get("time") or "",
            "title": text[:120],
            "summary": text[:240],
            "url": d.get("link") or "https://www.jin10.com/",
            "source": "金十",
        })
    return out


def news(limit: int = 30, sources: tuple[str, ...] = ("em", "sina", "jin10")) -> list[dict]:
    """国际/宏观新闻,按时间倒序。任一源失败不影响其它源。"""
    per = max(int(limit), 10)
    rows: list[dict] = []
    if "em" in sources:
        try:
            rows += _news_em(per)
        except Exception:
            pass
    if "sina" in sources:
        try:
            rows += _news_sina(per)
        except Exception:
            pass
    if "jin10" in sources and len(rows) < limit:
        try:
            rows += _news_jin10(per)
        except Exception:
            pass

    seen: set[str] = set()
    out = []
    for r in rows:
        key = re.sub(r"\W+", "", r["title"])[:40]
        if not key or key in seen:
            continue
        seen.add(key)
        r["tag"] = _tag_of(r["title"] + " " + (r.get("summary") or ""))
        out.append(r)
    out.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return out[:limit]


def news_cached(limit: int = 30) -> list[dict]:
    return http.cached(f"world:news:{limit}", _NEWS_TTL, lambda: news(limit))


def headline() -> str:
    """给界面/代理用的一句话摘要。"""
    try:
        rows = news_cached(10)
    except Exception:
        return ""
    if not rows:
        return ""
    return " · ".join(f"[{r['tag']}] {r['title'][:40]}" for r in rows[:3])
