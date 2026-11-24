# -*- coding: utf-8 -*-
"""公司分析:把 F10 原始数据算成「看得懂」的几块。

四块 + 一块事件:
  · 概况:公司资料(工商信息、主营范围、简介)
  · 主营构成:最新报告期按产品/行业/地区拆收入与毛利率
  · 财务趋势:最近 8 期营收/净利同比 + ROE,配「连续下滑/负债偏高」这类旗标
  · 估值与同业:PE(TTM)/PB/PS 与同二级行业中位数、分位、市值排名
  · 股东与事件:十大流通股东、股东户数、控股股东、机构家数、分红方案、近期公告

口径说明:
  · 估值数据来自东财 datacenter 每日估值表,不依赖 push2;按最新交易日收盘算。
  · 同业 = 同一 BOARD_NAME(东财二级行业)同一交易日;PE 分位只算 PE>0 的票,
    亏损股 PE 是负数,混进中位数会失真。
  · 主营构成只在最新一期里取,行业/产品/地区三组有的公司给不全,缺就缺。
"""
from __future__ import annotations

import time

from ..data import em
from ..utils import normalize_code, safe_float

CACHE_TTL = 600.0
_cache: dict[str, tuple[float, dict]] = {}


def _median(vals: list[float]) -> float | None:
    v = sorted(vals)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _pct_below(vals: list[float], x: float | None) -> float | None:
    """x 在同组里从低到高的分位(0~1);x 为空或列表空返回 None。"""
    if x is None:
        return None
    pool = [v for v in vals if v is not None and v > 0]
    if not pool or x <= 0:
        return None
    return round(sum(1 for v in pool if v < x) / len(pool), 4)


def _notes(fin: list[dict], val: dict, holders: dict, industry: str = "") -> list[str]:
    """几条能落地的基本面旗标。只陈述数据,不给买卖结论。"""
    out: list[str] = []
    if not fin:
        return out
    latest = fin[0]
    financial = any(k in (industry or "") for k in ("银行", "保险", "证券", "多元金融"))
    rev_yoy, prof_yoy = latest.get("revenue_yoy"), latest.get("profit_yoy")
    if rev_yoy is not None and prof_yoy is not None and rev_yoy < 0 and prof_yoy < 0:
        out.append("营收、净利同比双降")
    elif prof_yoy is not None and prof_yoy < -30:
        out.append(f"净利同比 {prof_yoy:.0f}%,下滑明显")
    elif prof_yoy is not None and prof_yoy > 30:
        out.append(f"净利同比 +{prof_yoy:.0f}%,增速较快")
    if len(fin) >= 4:
        # fin 是最新在前,「逐季走高」= 从旧到新递增 = 列表本身递减
        profits = [r.get("net_profit") for r in fin[:4]]
        if all(p is not None for p in profits) and profits == sorted(profits, reverse=True):
            out.append("近 4 期净利逐季走高")
    debt = latest.get("debt_ratio")
    if debt is not None and debt >= 70 and not financial:
        out.append(f"资产负债率 {debt:.0f}%,偏高")
    if financial:
        out.append("金融业负债率口径与实业不同,别横向比")
    roe = latest.get("roe")
    if roe is not None and roe >= 15:
        out.append(f"ROE {roe:.1f}%,盈利能力较强")
    gm = latest.get("gross_margin")
    if gm is not None and gm >= 50:
        out.append(f"毛利率 {gm:.0f}%,产品有溢价")
    if val:
        pe = val.get("pe_ttm")
        if pe is not None and pe < 0:
            out.append("PE 为负:最近 12 个月亏损")
    counts = holders.get("holder_counts") or []
    if counts and counts[0].get("change_pct") is not None:
        ch = counts[0]["change_pct"]
        if ch <= -10:
            out.append(f"股东户数环比 {ch:.0f}%,筹码集中")
        elif ch >= 20:
            out.append(f"股东户数环比 +{ch:.0f}%,筹码分散")
    return out


def _highlights(profile: dict, latest: dict, val: dict, vstat: dict,
                notes: list[str]) -> list[dict]:
    """公司分析页顶部的「投资要点」:行业地位 / 估值 / 成长 / 盈利 / 风险。

    只做事实陈述,颜色只表示方向(好/差/中性),不是买卖评级。
    """
    out: list[dict] = []

    board = (vstat or {}).get("board") or ""
    rank = (vstat or {}).get("cap_rank")
    n = (vstat or {}).get("peer_count")
    if board and rank and n:
        out.append({"tag": "行业", "tone": "flat",
                    "text": f"{board} 市值第 {rank}/{n}"})

    pe = (val or {}).get("pe_ttm")
    pct = (vstat or {}).get("pe_pct")
    if pct is not None:
        tone = "good" if pct <= 0.35 else ("bad" if pct >= 0.7 else "flat")
        out.append({"tag": "估值", "tone": tone,
                    "text": f"PE 低于 {pct * 100:.0f}% 同业"})
    elif pe is not None and pe < 0:
        out.append({"tag": "估值", "tone": "bad", "text": "PE 为负(亏损)"})

    rev = latest.get("revenue_yoy") if latest else None
    prof = latest.get("profit_yoy") if latest else None
    if rev is not None or prof is not None:
        tone = ("good" if (rev or 0) > 0 and (prof or 0) > 0
                else "bad" if (rev or 0) < 0 and (prof or 0) < 0 else "flat")
        out.append({"tag": "成长", "tone": tone,
                    "text": f"营收 {rev:+.1f}% / 净利 {prof:+.1f}%"
                            if rev is not None and prof is not None else
                            f"营收同比 {rev}" if rev is not None else
                            f"净利同比 {prof}"})

    roe = latest.get("roe") if latest else None
    gm = latest.get("gross_margin") if latest else None
    if roe is not None:
        out.append({"tag": "盈利",
                    "tone": "good" if roe >= 15 else ("bad" if roe < 0 else "flat"),
                    "text": f"ROE {roe:.1f}%" + (f" · 毛利率 {gm:.0f}%" if gm else "")})

    bad_words = ("负", "降", "偏高", "过高", "亏损", "分散", "双降", "下滑", "为负")
    bad_notes = [n for n in notes if any(k in n for k in bad_words)]
    if bad_notes:
        out.append({"tag": "风险", "tone": "bad", "text": bad_notes[0]})
    elif notes:
        out.append({"tag": "风险", "tone": "flat", "text": "未见明显数据旗标"})
    return out


def analyze(code: str, force: bool = False) -> dict:
    """一只票的完整公司分析。每块独立取数,某块失败只记 errors,不影响其它块。"""
    c = normalize_code(code or "")
    if not c.isdigit() or len(c) != 6:
        return {"ok": False, "code": code, "error": "股票代码要 6 位数字"}
    hit = _cache.get(c)
    if hit and not force and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    err: dict[str, str] = {}

    def grab(name: str, fn, default):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001
            err[name] = str(e).splitlines()[0][:100]
            return default

    profile = grab("profile", lambda: em.company_profile(c), {})
    business = grab("business", lambda: em.business_mix(c),
                    {"period": "", "groups": [], "scope": "", "review": ""})
    fin = grab("finance", lambda: em.finance_summary(c), [])
    val = grab("valuation", lambda: em.valuation_snapshot(c), {})

    peers: list[dict] = []
    if val.get("board") and val.get("date"):
        peers = grab("peers",
                     lambda: em.industry_valuation(val["board"], val["date"]), [])
    holders = grab("holders", lambda: em.shareholder_detail(c),
                   {"free_top10": [], "holder_counts": [], "controller": "",
                    "controller_ratio": None, "institutions": []})
    divs = grab("dividends", lambda: em.dividend_history(c), [])
    anns = grab("announcements", lambda: em.announcements(c, 6), [])

    # 同业估值:只拿 PE>0 的算中位与分位;市值排名含全部
    peers = [p for p in peers if p.get("code")]
    peers.sort(key=lambda p: -(p.get("total_cap") or 0))
    for i, p in enumerate(peers):
        p["cap_rank"] = i + 1
    pe_vals = [p.get("pe_ttm") for p in peers]
    pb_vals = [p.get("pb") for p in peers]
    pos_pe = [p["pe_ttm"] for p in peers if (p.get("pe_ttm") or 0) > 0]
    val_stats = {
        "board": val.get("board") or "",
        "date": val.get("date") or "",
        "pe_median": _median(pos_pe),
        "pb_median": _median([p["pb"] for p in peers if (p.get("pb") or 0) > 0]),
        "pe_pct": _pct_below(pe_vals, val.get("pe_ttm")),
        "pb_pct": _pct_below(pb_vals, val.get("pb")),
        "cap_rank": next((p["cap_rank"] for p in peers if p["code"] == c), None),
        "peer_count": len(peers),
    }

    latest = fin[0] if fin else {}
    trend = list(reversed(fin[:8]))
    notes = _notes(fin, val, holders, (profile or {}).get("industry") or "")
    highlights = _highlights(profile, latest, val, val_stats, notes)

    name = (profile or {}).get("name") or ""
    meaningful = any((profile or {}).get(k) for k in
                     ("name", "full_name", "industry", "list_date", "chairman"))
    if not name and (meaningful or fin or val):
        try:
            q = em.quotes([c]).get(c) or {}
            name = q.get("name") or c
        except Exception:                            # noqa: BLE001
            name = c
    if not meaningful and not fin and not val:
        return {"ok": False, "code": c, "name": name or c,
                "error": "查不到这只票的资料:代码不存在、已退市,或北交所(东财 F10 未覆盖)"}

    out = {
        "ok": True,
        "code": c,
        "name": name or c,
        "bs": time.strftime("%H:%M:%S"),
        "profile": profile,
        "business": business,
        "finance": fin,
        "latest": latest,
        "trend": trend,
        "valuation": val,
        "peers": peers,
        "val_stats": val_stats,
        "holders": holders,
        "dividends": divs,
        "announcements": anns,
        "notes": notes,
        "highlights": highlights,
        "errors": err,
    }
    _cache[c] = (time.monotonic(), out)
    return out
