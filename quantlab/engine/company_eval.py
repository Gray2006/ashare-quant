# -*- coding: utf-8 -*-
"""公司评估报告:把「财务 / 资金流 / 舆论 / 产品 / 风险」拼成一份可读、可导出的报告。

和 `engine/company.py` 的分工:
  · company.py 只负责把 F10 数据取回来、算好统计(主营构成、估值分位、股东结构);
  · 这里在它之上加两样东西:资金流、舆论情绪,然后做规则化打分与风险清单。

评分是**规则打分**,不是预测,也不是买卖建议。每个维度怎么来的都写在报告里:
  财务 40 / 资金 20 / 舆论 15 / 产品 15 / 治理风险 10,合计 100。
风险清单分高/中/低三档,只列能从数据里看到的;数据缺就明说,不编。

口径:
  · 舆论情绪 = 最近新闻标题的多空词命中差(复用 data/newsdb 的词表),粗糙但可复现;
  · 资金流用 data/moneyflow 的近 5 / 10 日主力净流入;
  · 解禁只提示未来 180 天内、占总股本 ≥5% 的;
  · PE 分位、同业比较沿用 company.py 的口径(只统计 PE>0 的同业)。
"""
from __future__ import annotations

import datetime as dt
import time

from ..data import em, moneyflow, newsdb
from . import company

CACHE_TTL = 300.0
_cache: dict[str, tuple[float, dict]] = {}

# 公告标题里能直接读出风险的词 → (档位, 说明)
NOTICE_RISKS = (
    ("立案", "高", "被立案调查"),
    ("退市", "高", "退市相关"),
    ("处罚", "中", "受到处罚"),
    ("问询", "中", "收到问询函"),
    ("监管函", "中", "收到监管函"),
    ("诉讼", "中", "涉及诉讼"),
    ("仲裁", "中", "涉及仲裁"),
    ("冻结", "中", "资产/股份被冻结"),
    ("质押", "中", "股份质押"),
    ("减持", "中", "股东减持"),
    ("解禁", "低", "限售解禁"),
    ("商誉减值", "中", "商誉减值"),
    ("终止", "中", "事项终止"),
    ("亏损", "中", "公告提及亏损"),
    ("风险提示", "中", "风险提示公告"),
    ("停牌", "中", "停牌"),
)

YI = 1e8


def _f(v, digits: int = 2, plus: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}" if plus else f"{v:.{digits}f}"


def _yi(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v / YI:,.{digits}f}"


def _clamp(v: float, lo: float = 0, hi: float = 10) -> float:
    return max(lo, min(hi, v))


# ------------------------------------------------------------------ 附加数据
def _money(code: str) -> dict:
    """近 5 / 10 日主力净流入 + 最近交易日。取不到就返回 {}。"""
    out: dict = {}
    try:
        w5 = moneyflow.window(code, 5) or {}
        out["d5"] = w5.get("total")
        out["up5"] = w5.get("up_days")
        out["down5"] = w5.get("down_days")
        out["bars"] = w5.get("bars") or []
        out["source"] = w5.get("source") or ""
    except Exception:                                # noqa: BLE001
        pass
    try:
        w10 = moneyflow.window(code, 10) or {}
        out["d10"] = w10.get("total")
        out["up10"] = w10.get("up_days")
    except Exception:                                # noqa: BLE001
        pass
    return out


def _news(code: str, limit: int = 40) -> list[dict]:
    try:
        items = em.stock_news(code, limit) or []
    except Exception:                                # noqa: BLE001
        return []
    out = []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({"title": title, "time": (it.get("time") or "")[:16],
                    "score": newsdb.score_text(title)})
    return out


def sentiment_of(news: list[dict], announcements: list[dict], name: str = "") -> dict:
    """舆论汇总:新闻情绪均值、正负面条数、公告情绪。

    标题里没提到公司名的(大盘/别家新闻)按 0.4 权重算 —— 东财个股新闻里常混着
    行业通稿,全按 1 算会把茅台的情绪带成「两融连降」。
    """
    def _w(n: dict) -> float:
        t = n.get("title") or ""
        return 1.0 if (not name or name in t) else 0.4

    scores = [(n.get("score") or 0.0) * _w(n) for n in news]
    pos = sum(1 for s in scores if s > 0.01)
    neg = sum(1 for s in scores if s < -0.01)
    avg = round(sum(scores) / len(scores), 3) if scores else None
    top_bad = sorted([n for n in news if (n.get("score") or 0) < 0],
                     key=lambda n: n["score"])[:3]
    top_good = sorted([n for n in news if (n.get("score") or 0) > 0],
                      key=lambda n: -n["score"])[:3]
    bad_notices = []
    good_notices = []
    for a in announcements or []:
        title = a.get("title") or ""
        sc, _pol = newsdb.score_notice(title)
        if sc < 0:
            bad_notices.append({"title": title, "score": sc})
        elif sc > 0:
            good_notices.append({"title": title, "score": sc})
    return {
        "count": len(news),
        "avg": avg,
        "pos": pos,
        "neg": neg,
        "neg_ratio": round(neg / len(news), 3) if news else None,
        "top_bad": top_bad,
        "top_good": top_good,
        "bad_notices": bad_notices,
        "good_notices": good_notices,
    }


# ------------------------------------------------------------------ 风险清单
def build_risks(base: dict, money: dict, sent: dict) -> list[dict]:
    """从已有数据里能看到的潜在风险。每条带档位与依据,便于单测。"""
    risks: list[dict] = []

    def add(level: str, title: str, detail: str):
        risks.append({"level": level, "title": title, "detail": detail})

    name = str(base.get("name") or "")
    latest = base.get("latest") or {}
    fin = base.get("finance") or []
    val = base.get("valuation") or {}
    holders = base.get("holders") or {}
    industry = ((base.get("profile") or {}).get("industry") or "")
    financial = any(k in industry for k in ("银行", "保险", "证券", "多元金融"))

    if "ST" in name.upper() or "退" in name:
        add("高", "ST / 退市风险", "股票简称带 ST 或退,交易与退市规则都不同")

    profit = latest.get("net_profit")
    rev_yoy = latest.get("revenue_yoy")
    if profit is not None and profit < 0 and rev_yoy is not None and rev_yoy < 0:
        add("高", "营收与利润双降", f"最新报告期净利 {_yi(profit)} 亿,营收同比 {rev_yoy:+.1f}%")
    elif profit is not None and profit < 0:
        add("中", "最近一期亏损", f"净利 {_yi(profit)} 亿,PE 为负,估值指标失效")

    debt = latest.get("debt_ratio")
    if debt is not None and not financial:
        if debt >= 85:
            add("高", "资产负债率过高", f"{debt:.1f}%,偿债压力大")
        elif debt >= 70:
            add("中", "资产负债率偏高", f"{debt:.1f}%")

    cr = latest.get("current_ratio")
    if cr is not None and cr < 1 and not financial:
        add("中", "流动比率低于 1", f"{cr:.2f},短期偿债靠续借或变卖资产")

    if len(fin) >= 2:
        yoy = [r.get("revenue_yoy") for r in fin[:2]]
        if all(y is not None and y < 0 for y in yoy):
            add("中", "营收连续两期下滑", "同比 " + " / ".join(f"{y:+.1f}%" for y in yoy))

    ocf = latest.get("ocf_to_profit")
    if ocf is not None:
        if ocf < 0:
            add("中", "经营现金流为负", f"经营现金流/净利 {ocf:.2f},利润没有现金支撑")
        elif ocf < 0.5:
            add("低", "盈利含金量低", f"经营现金流/净利仅 {ocf:.2f}")

    gmc = latest.get("gross_margin_change")
    if gmc is not None and gmc <= -3:
        add("低", "毛利率下滑", f"同比 {gmc:+.1f} 个百分点")

    price = val.get("price")
    if price is not None and 0 < price < 2:
        add("高" if price < 1.5 else "中", "低价股 / 面值风险",
            f"最新收盘 {price:.2f} 元,"
            + ("已接近 1 元面值退市线" if price < 1.5 else "低价股波动与流动性都差"))

    cap = val.get("total_cap")
    if cap is not None and cap < 50 * YI:
        add("低", "市值偏小", f"总市值 {_yi(cap)} 亿,流动性与波动都要打折")

    # 事件风险:公告标题里能读出来的
    seen = set()
    for a in base.get("announcements") or []:
        title = a.get("title") or ""
        for word, level, why in NOTICE_RISKS:
            if word in title and why not in seen:
                seen.add(why)
                add(level, why, title[:52])
                break

    # 解禁:未来 180 天内、占总股本 ≥5%
    today = dt.date.today()
    for u in holders.get("unlocks") or []:
        try:
            d = dt.date.fromisoformat(u.get("date") or "")
        except ValueError:
            continue
        days = (d - today).days
        ratio = u.get("ratio")
        if 0 <= days <= 180 and ratio is not None and ratio >= 5:
            add("中", "近期大额解禁",
                f"{u['date']} 解禁 {_yi(u.get('num'), 2)} 亿股,占总股本 {ratio:.1f}%")

    # 股东户数连续上升 = 筹码分散
    counts = holders.get("holder_counts") or []
    chs = [c.get("change_pct") for c in counts[:2]]
    if len(chs) == 2 and all(c is not None and c > 0 for c in chs):
        add("低", "股东户数连续上升", "筹码趋于分散")

    ctrl = holders.get("controller_ratio")
    if ctrl is not None and ctrl < 20:
        add("低", "控股股东持股比例低", f"{ctrl:.1f}%,控制力偏弱")

    if money.get("d5") is not None and money["d5"] < 0:
        add("低", "近 5 日主力净流出", f"{_yi(money['d5'])} 亿")
    if money.get("d10") is not None and money["d10"] < -5 * YI:
        add("低", "近 10 日资金持续流出", f"{_yi(money['d10'])} 亿")

    if sent.get("neg_ratio") is not None and sent["neg_ratio"] >= 0.4:
        add("低", "负面新闻占比偏高", f"{sent['neg']}/{sent['count']} 条标题偏空")

    order = {"高": 0, "中": 1, "低": 2}
    risks.sort(key=lambda r: order.get(r["level"], 3))
    return risks


# ------------------------------------------------------------------ 打分
def score_sections(base: dict, money: dict, sent: dict, risks: list[dict]) -> dict:
    """五维规则打分(满分 100)。返回总分、评级、各维度明细。"""
    latest = base.get("latest") or {}
    val = base.get("val_stats") or {}
    industry = ((base.get("profile") or {}).get("industry") or "")
    financial = any(k in industry for k in ("银行", "保险", "证券", "多元金融"))

    items = []

    # ---- 财务 40:盈利 12 + 成长 12 + 质量 8 + 偿债 8
    roe = latest.get("roe")
    if roe is None:
        profit_s = 5
        profit_note = "ROE 缺失,按中性给分"
    elif roe >= 15:
        profit_s, profit_note = 12, f"ROE {roe:.1f}%"
    elif roe >= 10:
        profit_s, profit_note = 9, f"ROE {roe:.1f}%"
    elif roe >= 5:
        profit_s, profit_note = 6, f"ROE {roe:.1f}%"
    elif roe > 0:
        profit_s, profit_note = 3, f"ROE {roe:.1f}%"
    else:
        profit_s, profit_note = 0, f"ROE {roe:.1f}%"

    def _growth(v):
        if v is None:
            return 3
        if v >= 20:
            return 6
        if v >= 5:
            return 4
        if v >= 0:
            return 2
        return 0

    growth_s = _growth(latest.get("revenue_yoy")) + _growth(latest.get("profit_yoy"))
    growth_note = (f"营收 {_f(latest.get('revenue_yoy'), 1, True)}% / "
                   f"净利 {_f(latest.get('profit_yoy'), 1, True)}%")

    ocf = latest.get("ocf_to_profit")
    if ocf is None:
        quality_s, quality_note = 4, "现金流/净利缺失"
    elif ocf >= 1:
        quality_s, quality_note = 8, f"经营现金流/净利 {ocf:.2f}"
    elif ocf >= 0.7:
        quality_s, quality_note = 6, f"{ocf:.2f}"
    elif ocf > 0:
        quality_s, quality_note = 4, f"{ocf:.2f}"
    else:
        quality_s, quality_note = 0, f"{ocf:.2f}"

    debt = latest.get("debt_ratio")
    if financial:
        debt_s, debt_note = 6, "金融业负债率口径不同,给中性"
    elif debt is None:
        debt_s, debt_note = 4, "负债率缺失"
    elif debt <= 40:
        debt_s, debt_note = 8, f"{debt:.1f}%"
    elif debt <= 60:
        debt_s, debt_note = 6, f"{debt:.1f}%"
    elif debt <= 70:
        debt_s, debt_note = 4, f"{debt:.1f}%"
    elif debt <= 85:
        debt_s, debt_note = 2, f"{debt:.1f}%"
    else:
        debt_s, debt_note = 0, f"{debt:.1f}%"
    cr = latest.get("current_ratio")
    if cr is not None and cr < 1 and not financial:
        debt_s = max(0, debt_s - 2)
        debt_note += f";流动比率 {cr:.2f}"

    fin_score = profit_s + growth_s + quality_s + debt_s
    items.append({"name": "财务", "score": fin_score, "max": 40,
                  "note": f"{profit_note};{growth_note};现金质量 {quality_note};负债 {debt_note}"})

    # ---- 资金 20:5 日方向 10 + 流入天数 5 + 最新一日 5
    d5 = money.get("d5")
    if d5 is None:
        money_s, money_note = 10, "资金流数据缺失,给中性"
    else:
        money_s = 10 if d5 > 0 else 2
        money_note = f"近 5 日 {_yi(d5)} 亿"
    up5, down5 = money.get("up5"), money.get("down5")
    if up5 is not None and down5 is not None:
        money_s += 5 if up5 >= 3 else 2
        money_note += f",{up5} 天流入"
    else:
        money_s += 2
    bars = money.get("bars") or []
    if bars:
        lastm = bars[-1].get("main")
        money_s += 5 if (lastm or 0) > 0 else 1
        money_note += f";最近一日 {_yi(lastm)} 亿"
    else:
        money_s += 2
    items.append({"name": "资金", "score": int(_clamp(money_s, 0, 20)), "max": 20,
                  "note": money_note})

    # ---- 舆论 15
    op_s = 12
    op_note = "无新闻样本" if not sent.get("count") else (
        f"新闻 {sent['count']} 条,情绪均值 {_f(sent.get('avg'), 2, True)}")
    avg = sent.get("avg")
    if avg is not None:
        if avg > 0.2:
            op_s += 2
        elif avg < -0.2:
            op_s -= 3
    nr = sent.get("neg_ratio")
    if nr is not None:
        if nr >= 0.5:
            op_s -= 3
        elif nr >= 0.3:
            op_s -= 2
        elif nr >= 0.2:
            op_s -= 1
    op_s -= min(4, 2 * len(sent.get("bad_notices") or []))
    op_s += min(2, len(sent.get("good_notices") or []))
    items.append({"name": "舆论", "score": int(_clamp(op_s, 0, 15)), "max": 15,
                  "note": op_note})

    # ---- 产品 15:毛利率 8 + 集中度 5 + 行业地位 2
    gm = latest.get("gross_margin")
    if gm is None:
        prod_s, prod_note = 8, "毛利率缺失"
    elif gm >= 40:
        prod_s, prod_note = 8, f"毛利率 {gm:.1f}%"
    elif gm >= 20:
        prod_s, prod_note = 6, f"毛利率 {gm:.1f}%"
    elif gm >= 10:
        prod_s, prod_note = 4, f"毛利率 {gm:.1f}%"
    else:
        prod_s, prod_note = 2, f"毛利率 {gm:.1f}%"

    groups = (base.get("business") or {}).get("groups") or []
    top_ratio = None
    for g in groups:
        if g.get("type") == "产品" and g.get("rows"):
            top_ratio = g["rows"][0].get("ratio")
            break
    if top_ratio is None:
        prod_s += 2
        prod_note += ";产品拆分缺失"
    elif top_ratio <= 0.6:
        prod_s += 5
        prod_note += f";最大单品 {top_ratio * 100:.0f}%"
    elif top_ratio <= 0.85:
        prod_s += 3
        prod_note += f";最大单品 {top_ratio * 100:.0f}%"
    else:
        prod_s += 1
        prod_note += f";单品依赖 {top_ratio * 100:.0f}%"

    rank = val.get("cap_rank")
    if rank is not None:
        prod_s += 2 if rank <= 5 else (1 if rank <= 10 else 0)
        prod_note += f";同业市值第 {rank}"
    items.append({"name": "产品", "score": int(_clamp(prod_s, 0, 15)), "max": 15,
                  "note": prod_note})

    # ---- 治理/风险 10:从满分往下扣
    gov_s = 10
    for r in risks:
        if r["level"] == "高":
            gov_s -= 3
        elif r["level"] == "中":
            gov_s -= 1.5
    items.append({"name": "治理风险", "score": int(_clamp(gov_s, 0, 10)), "max": 10,
                  "note": f"高 {sum(1 for r in risks if r['level'] == '高')} · "
                          f"中 {sum(1 for r in risks if r['level'] == '中')} · "
                          f"低 {sum(1 for r in risks if r['level'] == '低')}"})

    total = sum(i["score"] for i in items)
    if total >= 80:
        grade = "稳健"
    elif total >= 65:
        grade = "良好"
    elif total >= 50:
        grade = "中性"
    elif total >= 35:
        grade = "偏弱"
    else:
        grade = "高风险"
    return {"total": total, "grade": grade, "items": items}


# ------------------------------------------------------------------ 报告
def _points(base: dict, money: dict, sent: dict, risks: list[dict],
            scores: dict) -> list[str]:
    """投资要点:只讲数据,不给买卖结论。"""
    items = scores.get("items") or []
    pts: list[str] = []
    vstat = base.get("val_stats") or {}
    val = base.get("valuation") or {}
    latest = base.get("latest") or {}
    if vstat.get("cap_rank"):
        pts.append(f"行业地位:{vstat.get('board') or '同业'}市值第 "
                   f"{vstat['cap_rank']}/{vstat.get('peer_count')}")
    pe = val.get("pe_ttm")
    pct = vstat.get("pe_pct")
    if pct is not None:
        pts.append(f"估值:PE {_f(pe, 1)},低于 {pct * 100:.0f}% 的同业")
    elif pe is not None and pe < 0:
        pts.append("估值:PE 为负,最近 12 个月亏损")
    pts.append(f"成长:营收 {_f(latest.get('revenue_yoy'), 1, True)}% / "
               f"净利 {_f(latest.get('profit_yoy'), 1, True)}%")
    if items:
        best = max(items, key=lambda i: i["score"] / max(1, i["max"]))
        worst = min(items, key=lambda i: i["score"] / max(1, i["max"]))
        pts.append(f"评分强弱:{best['name']} {best['score']}/{best['max']} 最强,"
                   f"{worst['name']} {worst['score']}/{worst['max']} 最弱")
    hi = [r for r in risks if r["level"] == "高"]
    if hi:
        pts.append(f"首要风险:{hi[0]['title']}——{hi[0]['detail']}")
    elif risks:
        pts.append(f"风险:{risks[0]['title']}")
    else:
        pts.append("风险:已检查的项目里没有触发明显信号(不代表没有风险)")
    return pts


def render_report(base: dict, money: dict, sent: dict, risks: list[dict],
                  scores: dict) -> str:
    """Markdown 报告正文。UI 用 QTextBrowser.setMarkdown 渲染,也可直接导出 .md。"""
    name = base.get("name") or base.get("code")
    code = base.get("code")
    latest = base.get("latest") or {}
    val = base.get("valuation") or {}
    vstat = base.get("val_stats") or {}
    holders = base.get("holders") or {}
    biz = base.get("business") or {}
    fin = base.get("finance") or []
    peers = base.get("peers") or []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    head_bits = []
    if vstat.get("board"):
        head_bits.append(vstat["board"])
    if val.get("price") is not None:
        head_bits.append(f"收盘 {_f(val.get('price'), 2)}({_f(val.get('pct'), 2, True)}%)")
    if val.get("total_cap") is not None:
        head_bits.append(f"市值 {_yi(val.get('total_cap'), 0)} 亿")
    if val.get("pe_ttm") is not None:
        head_bits.append(f"PE {_f(val.get('pe_ttm'), 1)}")
    if val.get("pb") is not None:
        head_bits.append(f"PB {_f(val.get('pb'), 2)}")

    lines = [
        f"# {name}({code}) · 公司评估报告",
        "",
        "> " + " · ".join(head_bits) if head_bits else "",
        "",
        f"- 生成时间:{now}",
        f"- 数据截止:财报 {latest.get('period') or '—'};"
        f"估值 {val.get('date') or '—'}({vstat.get('board') or '—'})",
        "",
        "## 一、结论",
        "",
        f"**{scores['grade']} · {scores['total']}/100**",
        "",
    ]
    for pt in _points(base, money, sent, risks, scores):
        lines.append(f"- {pt}")

    lines += [
        "",
        "## 二、评分",
        "",
        "| 维度 | 得分 | 满分 | 依据 |",
        "|---|---:|---:|---|",
    ]
    for it in scores["items"]:
        lines.append(f"| {it['name']} | {it['score']} | {it['max']} | {it['note']} |")

    lines += [
        "",
        "## 三、财务",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 营收 | {_yi(latest.get('revenue'))} 亿 |",
        f"| 营收同比 | {_f(latest.get('revenue_yoy'), 1, True)}% |",
        f"| 归母净利 | {_yi(latest.get('net_profit'))} 亿 |",
        f"| 净利同比 | {_f(latest.get('profit_yoy'), 1, True)}% |",
        f"| ROE | {_f(latest.get('roe'), 1)}% |",
        f"| 毛利率 / 净利率 | {_f(latest.get('gross_margin'), 1)}% / "
        f"{_f(latest.get('net_margin'), 1)}% |",
        f"| 资产负债率 | {_f(latest.get('debt_ratio'), 1)}% |",
        f"| 流动比率 | {_f(latest.get('current_ratio'), 2)} |",
        f"| 经营现金流/净利 | {_f(latest.get('ocf_to_profit'), 2)} |",
    ]
    if latest.get("rd_expense"):
        lines.append(f"| 研发费用 | {_yi(latest.get('rd_expense'))} 亿 |")

    if fin:
        lines += [
            "",
            "最近 4 期趋势:",
            "",
            "| 报告期 | 营收(亿) | 同比% | 净利(亿) | 同比% | ROE% |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in fin[:4]:
            lines.append(
                f"| {r.get('period') or '—'} | {_yi(r.get('revenue'))} | "
                f"{_f(r.get('revenue_yoy'), 1, True)} | {_yi(r.get('net_profit'))} | "
                f"{_f(r.get('profit_yoy'), 1, True)} | {_f(r.get('roe'), 1)} |")

    lines += [
        "",
        "## 四、估值与同业",
        "",
    ]
    if vstat.get("peer_count"):
        bits = [f"{vstat.get('board') or '同业'} {vstat['peer_count']} 只",
                f"PE 中位 {_f(vstat.get('pe_median'), 1)}",
                f"PB 中位 {_f(vstat.get('pb_median'), 2)}"]
        if vstat.get("pe_pct") is not None:
            bits.append(f"本股 PE 分位 {vstat['pe_pct'] * 100:.0f}%")
        if vstat.get("cap_rank"):
            bits.append(f"市值第 {vstat['cap_rank']}/{vstat['peer_count']}")
        lines += [" · ".join(bits), ""]
    if peers:
        show = list(peers[:6])
        own = code
        if not any(p.get("code") == own for p in show):
            me = next((p for p in peers if p.get("code") == own), None)
            if me:
                show.append(me)
        lines += ["| 名称 | PE(TTM) | PB | 市值(亿) |", "|---|---:|---:|---:|"]
        for p in show:
            mark = " **(本股)**" if p.get("code") == own else ""
            lines.append(f"| {p.get('name') or p.get('code')}{mark} | "
                         f"{_f(p.get('pe_ttm'), 1)} | {_f(p.get('pb'), 2)} | "
                         f"{_yi(p.get('total_cap'), 0)} |")
    elif not vstat.get("peer_count"):
        lines.append("- 估值表没有这只票或同业数据,无法对比")

    lines += ["", "## 五、资金流", ""]
    if money.get("d5") is not None:
        lines += [
            f"- 近 5 日主力净流入:**{_yi(money.get('d5'))} 亿**"
            f"({money.get('up5', '—')} 天流入 / {money.get('down5', '—')} 天流出)",
            f"- 近 10 日主力净流入:{_yi(money.get('d10'))} 亿",
        ]
        bars = money.get("bars") or []
        if bars:
            lines.append("- 最近 5 个交易日:"
                         + " · ".join(f"{str(b.get('date'))[5:]}:{_yi(b.get('main'))}"
                                      for b in bars[-5:]))
    else:
        lines.append("- 资金流数据没取到,不做判断")

    lines += ["", "## 六、舆论", ""]
    if sent.get("count"):
        lines += [
            f"- 最近新闻 {sent['count']} 条,情绪均值 {_f(sent.get('avg'), 2, True)}"
            f",正面 {sent['pos']} / 负面 {sent['neg']}",
        ]
        for n in sent.get("top_bad") or []:
            lines.append(f"- 偏空:{n['title'][:56]}({n.get('time')})")
        if not sent.get("top_bad"):
            lines.append("- 没有明显偏空的标题")
        for n in sent.get("top_good") or []:
            lines.append(f"- 偏多:{n['title'][:56]}({n.get('time')})")
    else:
        lines.append("- 没有取到近期新闻,不做判断")
    if sent.get("bad_notices"):
        lines.append("- 公告里的负面信号:" + ";".join(
            f"{b['title'][:36]}" for b in sent["bad_notices"][:4]))

    lines += ["", "## 七、产品与主营", ""]
    if biz.get("groups"):
        lines.append(f"最新报告期 {biz.get('period') or '—'}:")
        lines.append("")
        lines.append("| 分类 | 项目 | 收入(亿) | 占比 | 毛利率 |")
        lines.append("|---|---|---:|---:|---:|")
        for g in biz["groups"]:
            for i, row in enumerate(g.get("rows") or []):
                ratio = row.get("ratio")
                m = row.get("margin")
                cells = [
                    g["type"] if i == 0 else "",
                    str(row.get("name") or ""),
                    _yi(row.get("income")),
                    f"{ratio * 100:.1f}%" if ratio is not None else "—",
                    f"{m * 100:.1f}%" if m is not None else "—",
                ]
                lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("- 主营构成没取到")

    lines += ["", "## 八、股东与事件", ""]
    counts = holders.get("holder_counts") or []
    if counts:
        c0 = counts[0]
        s = f"- 股东户数:{c0.get('holders') or '—'}"
        if c0.get("change_pct") is not None:
            s += f",环比 {c0['change_pct']:+.1f}%"
        if c0.get("focus"):
            s += f",筹码{c0['focus']}"
        lines.append(s)
    if holders.get("controller"):
        s = f"- 控股股东:{holders['controller']}"
        if holders.get("controller_ratio"):
            s += f"({holders['controller_ratio']:.1f}%)"
        lines.append(s)
    inst = holders.get("institutions") or []
    if inst and inst[0].get("org_num"):
        lines.append(f"- 机构:{inst[0]['org_num']:,.0f} 家,合计持股 "
                     f"{_f(inst[0].get('ratio'), 1)}%")
    unlocks = holders.get("unlocks") or []
    if unlocks:
        lines.append("- 解禁:" + ";".join(
            f"{u.get('date')} {_yi(u.get('num'), 2)} 亿股({_f(u.get('ratio'), 1)}%)"
            for u in unlocks[:3]))
    divs = base.get("dividends") or []
    paid = next((x for x in divs if x.get("cash_per10")), None)
    if paid:
        lines.append(f"- 最近分红:10 派 {paid['cash_per10']:.2f} 元"
                     f"({paid.get('progress') or '—'})")
    anns = base.get("announcements") or []
    if anns:
        lines.append("- 最新公告:" + " / ".join(
            (a.get("title") or "")[:30] for a in anns[:2]))

    lines += ["", "## 九、潜在风险", ""]
    if not risks:
        lines.append("- 已检查的项目里没有触发明显风险信号(不代表没有风险)")
    for r in risks:
        lines.append(f"- **[{r['level']}] {r['title']}**:{r['detail']}")

    lines += ["", "## 十、数据与口径", ""]
    err = base.get("errors") or {}
    if err:
        lines.append("- 未取到:" + ";".join(f"{k}({v[:40]})" for k, v in err.items()))
    else:
        lines.append("- 各块数据均取到")
    if not money.get("d5"):
        lines.append("- 资金流缺失,资金维度按中性给分")
    if not (base.get("business") or {}).get("groups"):
        lines.append("- 主营构成缺失,产品维度按中性给分")
    lines += [
        "",
        "> 本报告由规则自动生成,评分不是收益预测,也不构成买卖建议。"
        "数据来自东财 F10 / datacenter / 新浪资金流,口径见 `engine/company_eval.py`。",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 对外
def evaluate(code: str, force: bool = False) -> dict:
    """一家公司的完整评估。基础资料复用 company.analyze 的缓存。"""
    base = company.analyze(code, force=force)
    if not base.get("ok"):
        return base
    c = base["code"]
    hit = _cache.get(c)
    if hit and not force and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]

    money = _money(c)
    news = _news(c)
    sent = sentiment_of(news, base.get("announcements") or [], base.get("name") or "")
    risks = build_risks(base, money, sent)
    scores = score_sections(base, money, sent, risks)
    points = _points(base, money, sent, risks, scores)
    out = {
        "ok": True,
        "code": c,
        "name": base.get("name") or c,
        "base": base,
        "money": money,
        "news": news,
        "sentiment": sent,
        "risks": risks,
        "scores": scores,
        "points": points,
        "text": render_report(base, money, sent, risks, scores),
        "ts": time.strftime("%H:%M:%S"),
    }
    _cache[c] = (time.monotonic(), out)
    return out
