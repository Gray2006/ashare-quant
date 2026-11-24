# -*- coding: utf-8 -*-
"""把公司评估结果渲染成一份「能看」的 HTML 报告(QTextBrowser 直接 setHtml)。

参考同花顺 F10 的卡片式排版和 OpenBB 的研究页:顶部结论卡、KPI 网格、
分节标题、浅色表格线、风险用颜色分级。HTML 里带锚点,页面左侧的导航按钮
用 `scrollToAnchor` 跳转,不用一路往下滚。

纯函数、不碰网络,方便单测;导出的 Markdown 仍由 engine/company_eval.py 生成。
"""
from __future__ import annotations

import html

from .theme import ACCENT, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

YI = 1e8


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _f(v, digits: int = 2, plus: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}" if plus else f"{v:.{digits}f}"


def _yi(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v / YI:,.{digits}f}"


def _pnl(v) -> str:
    if v is None:
        return TEXT
    return UP if v > 0 else DOWN if v < 0 else TEXT_DIM


def _h2(text: str, anchor: str) -> str:
    return (f'<p style="margin:22px 0 6px 0"><a name="{anchor}"></a>'
            f'<table cellspacing="0" cellpadding="0" border="0"><tr>'
            f'<td width="3" bgcolor="{ACCENT}"></td>'
            f'<td style="padding-left:8px;font-size:15px;font-weight:700;'
            f'color:{TEXT}">{_e(text)}</td></tr></table></p>')


def _p(text: str, color: str = TEXT, size: int = 13, top: int = 4) -> str:
    return (f'<p style="margin:{top}px 0;font-size:{size}px;color:{color}">'
            f'{text}</p>')


def _kpi_grid(cells: list[tuple[str, str, str]]) -> str:
    """三列 KPI 卡:每格 (标签, 数值, 颜色)。"""
    out = ['<table width="100%" cellspacing="6" cellpadding="0" border="0"><tr>']
    for i, (name, value, color) in enumerate(cells):
        if i and i % 3 == 0:
            out.append("</tr><tr>")
        out.append(
            f'<td width="33%" bgcolor="#151e28" style="padding:8px 10px;">'
            f'<span style="font-size:11px;color:{TEXT_MUT}">{_e(name)}</span><br>'
            f'<span style="font-size:17px;font-weight:700;color:{color}">{_e(value)}</span>'
            f'</td>')
    out.append("</tr></table>")
    return "".join(out)


def _table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    aligns = aligns or ["left"] + ["right"] * (len(headers) - 1)
    out = ['<table width="100%" cellspacing="0" cellpadding="5" border="0" '
           'style="border-collapse:collapse;font-size:12px;">']
    out.append("<tr bgcolor=\"#18232f\">")
    for i, h in enumerate(headers):
        out.append(f'<td align="{aligns[i]}" style="color:{TEXT_MUT};'
                   f'border-bottom:1px solid #2a3542;">{_e(h)}</td>')
    out.append("</tr>")
    for row in rows:
        out.append("<tr>")
        for i, c in enumerate(row):
            out.append(f'<td align="{aligns[i]}" style="color:{TEXT};'
                       f'border-bottom:1px solid #212a35;">{c}</td>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _badge(text: str, color: str) -> str:
    return (f'<span style="color:{color};font-weight:600">'
            f'{_e(text)}</span>')


def build_html(d: dict) -> str:
    """评估结果 → 带锚点的 HTML。d 是 company_eval.evaluate() 的返回。"""
    base = d.get("base") or {}
    sc = d.get("scores") or {}
    latest = base.get("latest") or {}
    val = base.get("valuation") or {}
    vstat = base.get("val_stats") or {}
    holders = base.get("holders") or {}
    biz = base.get("business") or {}
    money = d.get("money") or {}
    sent = d.get("sentiment") or {}
    risks = d.get("risks") or []
    items = sc.get("items") or []
    name = base.get("name") or d.get("name") or ""
    code = base.get("code") or d.get("code") or ""

    grade = sc.get("grade") or "—"
    grade_color = {"稳健": UP, "良好": ACCENT, "中性": TEXT,
                   "偏弱": WARN, "高风险": DOWN}.get(grade, TEXT)

    parts: list[str] = ['<div style="font-family:\'Microsoft YaHei\',sans-serif;">']

    # ---- 顶部结论卡
    facts = []
    if vstat.get("board"):
        facts.append(_e(vstat["board"]))
    if val.get("price") is not None:
        facts.append(f"收盘 {_f(val.get('price'))} "
                     f"({_f(val.get('pct'), 2, True)}%)")
    if val.get("total_cap") is not None:
        facts.append(f"市值 {_yi(val.get('total_cap'), 0)} 亿")
    if val.get("pe_ttm") is not None:
        facts.append(f"PE {_f(val.get('pe_ttm'), 1)}")
    parts.append(
        f'<table width="100%" cellspacing="0" cellpadding="12" border="0">'
        f'<tr><td bgcolor="#131d28" style="border-left:4px solid {grade_color};">'
        f'<span style="font-size:19px;font-weight:700;color:{TEXT}">{_e(name)}'
        f' <span style="font-size:12px;color:{TEXT_MUT}">({_e(code)})</span></span>'
        f'<br><span style="font-size:12px;color:{TEXT_DIM}">{" · ".join(facts)}</span>'
        f'</td>'
        f'<td bgcolor="#131d28" align="right" width="150">'
        f'<span style="font-size:26px;font-weight:700;color:{grade_color}">{sc.get("total", "—")}</span>'
        f'<span style="font-size:12px;color:{TEXT_MUT}">/100</span><br>'
        f'<span style="font-size:13px;color:{grade_color}">{_e(grade)}</span>'
        f'</td></tr></table>')

    # ---- 一、结论
    parts.append(_h2("一、结论与投资要点", "sec-1"))
    for pt in d.get("points") or []:
        parts.append(f'<p style="margin:3px 0;font-size:13px;color:{TEXT}">'
                     f'<span style="color:{ACCENT}">·</span> {_e(pt)}</p>')
    hi = [r for r in risks if r.get("level") == "高"]
    if hi:
        parts.append(_p(f'<span style="color:{DOWN};font-weight:600">先看高风险</span>'
                        f':{_e(";".join(r["title"] for r in hi[:3]))}', TEXT, 13, 8))

    # ---- 二、评分
    parts.append(_h2("二、五维评分", "sec-2"))
    rows = []
    for it in items:
        ratio = it["score"] / max(1, it["max"])
        color = UP if ratio >= 0.75 else ACCENT if ratio >= 0.5 else \
            WARN if ratio >= 0.3 else DOWN
        rows.append([f'<b>{_e(it["name"])}</b>',
                     f'<span style="color:{color};font-weight:700">{it["score"]}'
                     f'</span><span style="color:{TEXT_MUT}">/{it["max"]}</span>',
                     f'<span style="color:{TEXT_DIM}">{_e(it["note"])}</span>'])
    parts.append(_table(["维度", "得分", "依据"], rows, ["left", "left", "left"]))

    # ---- 三、财务
    parts.append(_h2("三、财务", "sec-3"))
    parts.append(_kpi_grid([
        ("营收(亿)", _yi(latest.get("revenue")), TEXT),
        ("营收同比", f'{_f(latest.get("revenue_yoy"), 1, True)}%',
         _pnl(latest.get("revenue_yoy"))),
        ("归母净利(亿)", _yi(latest.get("net_profit")), TEXT),
        ("净利同比", f'{_f(latest.get("profit_yoy"), 1, True)}%',
         _pnl(latest.get("profit_yoy"))),
        ("ROE", f'{_f(latest.get("roe"), 1)}%', TEXT),
        ("毛利率", f'{_f(latest.get("gross_margin"), 1)}%', TEXT),
        ("资产负债率", f'{_f(latest.get("debt_ratio"), 1)}%',
         DOWN if (latest.get("debt_ratio") or 0) >= 70 else TEXT),
        ("流动比率", _f(latest.get("current_ratio"), 2), TEXT),
        ("经营现金流/净利", _f(latest.get("ocf_to_profit"), 2),
         DOWN if (latest.get("ocf_to_profit") or 1) < 0 else TEXT),
    ]))
    fin = base.get("finance") or []
    if fin:
        rows = [[_e(r.get("period") or "—"), _yi(r.get("revenue")),
                 f'{_f(r.get("revenue_yoy"), 1, True)}%',
                 _yi(r.get("net_profit")),
                 f'{_f(r.get("profit_yoy"), 1, True)}%',
                 f'{_f(r.get("roe"), 1)}%'] for r in fin[:4]]
        parts.append(_p("最近 4 期", TEXT_MUT, 12, 10))
        parts.append(_table(["报告期", "营收(亿)", "同比", "净利(亿)", "同比", "ROE"],
                            rows))

    # ---- 四、估值与同业
    parts.append(_h2("四、估值与同业", "sec-4"))
    if vstat.get("peer_count"):
        parts.append(_kpi_grid([
            ("PE(TTM)", _f(val.get("pe_ttm"), 1), TEXT),
            ("行业 PE 中位", _f(vstat.get("pe_median"), 1), TEXT_DIM),
            ("PE 分位", f'{vstat["pe_pct"] * 100:.0f}%' if vstat.get("pe_pct") is not None else "—",
             ACCENT),
            ("PB", _f(val.get("pb"), 2), TEXT),
            ("行业 PB 中位", _f(vstat.get("pb_median"), 2), TEXT_DIM),
            ("市值排名", f'{vstat.get("cap_rank")}/{vstat.get("peer_count")}', TEXT),
        ]))
    peers = base.get("peers") or []
    if peers:
        show = list(peers[:6])
        own = code
        if not any(p.get("code") == own for p in show):
            me = next((p for p in peers if p.get("code") == own), None)
            if me:
                show.append(me)
        rows = []
        for p in show:
            me = p.get("code") == own
            nm = _e(p.get("name") or p.get("code"))
            if me:
                nm = f'<b style="color:{ACCENT}">{nm}</b>'
            rows.append([nm, _f(p.get("pe_ttm"), 1), _f(p.get("pb"), 2),
                         _yi(p.get("total_cap"), 0)])
        parts.append(_p("同业对比", TEXT_MUT, 12, 10))
        parts.append(_table(["名称", "PE", "PB", "市值(亿)"], rows))

    # ---- 五、资金流
    parts.append(_h2("五、资金流", "sec-5"))
    if money.get("d5") is not None:
        parts.append(_kpi_grid([
            ("近 5 日主力", f'{_yi(money.get("d5"))} 亿',
             _pnl(money.get("d5"))),
            ("近 10 日主力", f'{_yi(money.get("d10"))} 亿',
             _pnl(money.get("d10"))),
            ("5 日流入天数", f'{money.get("up5", "—")} / {money.get("down5", "—")}', TEXT),
        ]))
        bars = money.get("bars") or []
        if bars:
            parts.append(_p("· ".join(
                f'{str(b.get("date"))[5:]}:{_yi(b.get("main"))}'
                for b in bars[-5:]), TEXT_DIM, 12, 6))
    else:
        parts.append(_p("资金流数据没取到,不做判断", TEXT_DIM))

    # ---- 六、舆论
    parts.append(_h2("六、舆论", "sec-6"))
    if sent.get("count"):
        parts.append(_kpi_grid([
            ("新闻条数", str(sent["count"]), TEXT),
            ("情绪均值", _f(sent.get("avg"), 2, True), _pnl(sent.get("avg"))),
            ("正 / 负", f'{sent["pos"]} / {sent["neg"]}', TEXT),
        ]))
        for n in sent.get("top_bad") or []:
            parts.append(_p(f'<span style="color:{DOWN}">偏空</span> '
                            f'{_e(n["title"][:56])} <span style="color:{TEXT_MUT}">'
                            f'{_e(n.get("time"))}</span>', TEXT, 12, 2))
        for n in sent.get("top_good") or []:
            parts.append(_p(f'<span style="color:{UP}">偏多</span> '
                            f'{_e(n["title"][:56])} <span style="color:{TEXT_MUT}">'
                            f'{_e(n.get("time"))}</span>', TEXT, 12, 2))
    else:
        parts.append(_p("没有取到近期新闻,不做判断", TEXT_DIM))

    # ---- 七、产品与主营
    parts.append(_h2("七、产品与主营", "sec-7"))
    if biz.get("groups"):
        rows = []
        for g in biz["groups"]:
            for i, row in enumerate(g.get("rows") or []):
                ratio = row.get("ratio")
                m = row.get("margin")
                rows.append([
                    _e(g["type"] if i == 0 else ""),
                    _e(row.get("name") or ""),
                    _yi(row.get("income")),
                    f"{ratio * 100:.1f}%" if ratio is not None else "—",
                    f"{m * 100:.1f}%" if m is not None else "—",
                ])
        parts.append(_p(f'报告期 {_e(biz.get("period") or "—")}', TEXT_MUT, 12))
        parts.append(_table(["分类", "项目", "收入(亿)", "占比", "毛利率"], rows,
                            ["left", "left", "right", "right", "right"]))
    else:
        parts.append(_p("主营构成没取到", TEXT_DIM))

    # ---- 八、股东与事件
    parts.append(_h2("八、股东与事件", "sec-8"))
    counts = holders.get("holder_counts") or []
    if counts:
        c0 = counts[0]
        s = f'股东户数 {c0.get("holders") or "—"}'
        if c0.get("change_pct") is not None:
            s += f',环比 {c0["change_pct"]:+.1f}%'
        parts.append(_p("· " + _e(s)))
    if holders.get("controller"):
        parts.append(_p(f'· 控股股东:{_e(holders["controller"])}'
                        + (f'({holders["controller_ratio"]:.1f}%)'
                           if holders.get("controller_ratio") else "")))
    inst = holders.get("institutions") or []
    if inst and inst[0].get("org_num"):
        parts.append(_p(f'· 机构:{int(inst[0]["org_num"]):,} 家,合计持股 '
                        f'{_f(inst[0].get("ratio"), 1)}%'))
    for u in (holders.get("unlocks") or [])[:2]:
        parts.append(_p(f'· 解禁:{_e(u.get("date"))} {_yi(u.get("num"), 2)} 亿股'
                        f'({_f(u.get("ratio"), 1)}%)'))
    paid = next((x for x in (base.get("dividends") or []) if x.get("cash_per10")), None)
    if paid:
        parts.append(_p(f'· 最近分红:10 派 {paid["cash_per10"]:.2f} 元'
                        f'({_e(paid.get("progress") or "—")})'))

    # ---- 九、风险
    parts.append(_h2("九、潜在风险", "sec-9"))
    if not risks:
        parts.append(_p("已检查的项目里没有触发明显风险信号(不代表没有风险)", TEXT_DIM))
    for r in risks:
        color = DOWN if r["level"] == "高" else WARN if r["level"] == "中" else TEXT_MUT
        parts.append(_p(f'<span style="color:{color};font-weight:600">'
                        f'[{_e(r["level"])}] {_e(r["title"])}</span> '
                        f'<span style="color:{TEXT_DIM}">{_e(r["detail"])}</span>',
                        TEXT, 12, 3))

    # ---- 十、数据与口径
    parts.append(_h2("十、数据与口径", "sec-10"))
    err = base.get("errors") or {}
    if err:
        parts.append(_p("未取到:" + ";".join(f"{_e(k)}({_e(v[:40])})"
                                            for k, v in err.items()), TEXT_DIM, 12))
    else:
        parts.append(_p("各块数据均取到", TEXT_DIM, 12))
    parts.append(_p("规则自动生成,评分不是收益预测,也不构成买卖建议。"
                    "数据来自东财 F10 / datacenter / 新浪资金流。", TEXT_MUT, 11, 10))
    parts.append("</div>")
    return "".join(parts)
