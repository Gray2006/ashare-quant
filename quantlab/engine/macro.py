# -*- coding: utf-8 -*-
"""大盘研判:趋势 / 广度 / 资金 / 外围 / 消息政策 五个维度打分。

输出是一个「站位」而不是预测:每个维度给 0~100 分和一句人话依据,
再加若干要点与风险。分数只用来判断当下该进攻还是防守,不预测点位。

数据缺一块就少一维(权重按剩余维度归一),不让接口失败把整份报告搞崩。
"""
from __future__ import annotations

import datetime as dt


from ..data import kline as kl
from ..data import world
from . import indicators as ind

# 维度权重(总和 100)
WEIGHTS = {"趋势": 32, "广度": 18, "资金": 20, "外围": 18, "消息政策": 12}

INDEXES = [("000001", "上证指数"), ("000300", "沪深300"),
           ("399006", "创业板指"), ("399001", "深证成指")]

# 消息面关键词:利空 / 利多(命中即计分,纯规则,可读可改)
BULL_WORDS = ("降准", "降息", "宽松", "刺激", "回暖", "超预期", "回购", "增持",
              "利好", "反弹", "放量上涨", "净流入", "提振", "支持", "扩内需")
BEAR_WORDS = ("加征", "关税", "制裁", "限制", "收紧", "暴跌", "跳水", "违约",
              "下滑", "不及预期", "减持", "净流出", "缩量", "风险", "调查", "处罚")
POLICY_WORDS = ("国常会", "政治局", "证监会", "央行", "财政部", "发改委", "十五五",
                "国务院", "政策", "会议", "部署", "印发", "试点")


def _pct(a: float, b: float) -> float | None:
    if not a or not b:
        return None
    return (a / b - 1) * 100


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return float(max(lo, min(hi, x)))


def _index_trend() -> dict | None:
    """用两三个主要指数算趋势分。"""
    picked = []
    for code, name in INDEXES[:3]:
        try:
            bars, _ = kl.get_kline(code, 101, count=160, is_index=True)
        except Exception:
            bars = []
        if not bars or len(bars) < 70:
            continue
        df = ind.bars_to_df(bars)
        c = df["close"]
        last = float(c.iloc[-1])
        ma20 = float(c.rolling(20, min_periods=1).mean().iloc[-1])
        ma60 = float(c.rolling(60, min_periods=1).mean().iloc[-1])
        r5 = _pct(last, float(c.iloc[-6])) if len(c) > 6 else None
        r20 = _pct(last, float(c.iloc[-21])) if len(c) > 21 else None
        picked.append({"code": code, "name": name, "price": last,
                       "ma20": ma20, "ma60": ma60, "r5": r5, "r20": r20,
                       "above20": last > ma20, "above60": last > ma60,
                       "date": str(df["datetime"].iloc[-1])[:10]})
    if not picked:
        return None

    score = 50.0
    for p in picked:
        score += 8 if p["above20"] else -8
        score += 7 if p["above60"] else -7
        if p["r5"] is not None:
            score += _clamp(p["r5"] * 1.6, -8, 8)
        if p["r20"] is not None:
            score += _clamp(p["r20"] * 0.7, -8, 8)
    score = _clamp(score / len(picked) * 1.0 + 50 * (1 - 1 / len(picked)) * 0)

    lines = []
    p0 = picked[0]
    lines.append(f"{p0['name']} {p0['price']:.2f},"
                 f"{'站上' if p0['above20'] else '跌破'} 20 日线,"
                 f"{'站上' if p0['above60'] else '跌破'} 60 日线")
    weak = [p["name"] for p in picked if not p["above20"]]
    if weak:
        lines.append("弱势:" + "、".join(weak))
    return {"score": _clamp(score), "detail": ";".join(lines), "items": picked}


def _breadth() -> dict | None:
    """涨跌家数 + 涨停跌停(全市场快照,有缓存)。"""
    try:
        from . import screener
        df = screener.snapshot()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    up = int((df["pct_chg"] > 0).sum())
    down = int((df["pct_chg"] < 0).sum())
    total = max(1, up + down)
    ratio = up / total
    # 涨跌停要按板块限制判:创业板/科创板 20%、北交所 30%,不能一律按 9.7% 算
    from .exchange import limit_pct as _lp
    limits = df["code"].map(lambda c: _lp(str(c)) * 100)
    limit_up = int((df["pct_chg"] >= limits - 0.3).sum())
    limit_down = int((df["pct_chg"] <= -limits + 0.3).sum())
    amt = float(df["amount"].fillna(0).sum())

    score = 50.0
    score += (ratio - 0.5) * 110
    score += _clamp((limit_up - limit_down) * 1.2, -12, 12)
    detail = (f"上涨 {up} 家 / 下跌 {down} 家(多空比 {ratio*100:.0f}%),"
              f"涨停 {limit_up} 跌停 {limit_down},两市成交 {amt/1e8:.0f} 亿")
    return {"score": _clamp(score), "detail": detail,
            "items": {"up": up, "down": down, "limit_up": limit_up,
                      "limit_down": limit_down, "amount": amt}}


def _money() -> dict | None:
    """资金面:主力净流入合计。取不到就返回 None 让权重归一,不编中性分。"""
    out: dict = {}
    try:
        from . import screener
        df = screener.snapshot()
        if df is not None and not df.empty and "main_inflow" in df.columns:
            vals = df["main_inflow"].dropna()
            if len(vals) > 100:                     # 覆盖太少说明字段没回来
                out["inflow"] = float(vals.sum())
                out["coverage"] = len(vals)
    except Exception:
        pass
    if "inflow" not in out:
        try:
            from ..data import em
            rows = em.moneyflow_rank(1, asc=False) or []
            if rows and rows[0].get("main_inflow") is not None:
                out["top_inflow"] = float(rows[0]["main_inflow"])
                out["top_name"] = rows[0].get("name")
        except Exception:
            pass
    # 第三条路:screener 也没数据(东财全挂)→ 抽样大票,走资金流层批量口。
    # 批量口内部并行 + 带缓存,10 只从 7 秒压到 1 秒出头;口径仍是抽样,不是全市场合计。
    if "inflow" not in out:
        try:
            from ..data import moneyflow as _mf
            from . import screener as _sc
            snap = _sc.snapshot()
            codes: list[str] = []
            if snap is not None and not snap.empty and "amount" in snap.columns:
                codes = [str(c) for c in
                         snap.sort_values("amount", ascending=False)["code"].head(10)]
            if not codes:
                codes = [r.get("code") for r in (_mf.rank(20) or []) if r.get("code")][:10]
            got_rows = _mf.batch(codes[:10], deadline=3.0)
            vals = [float(r["main"]) for r in got_rows.values()
                    if r.get("main") is not None]
            if len(vals) >= 5:
                out["sample_inflow"] = sum(vals)
                out["sample_pos"] = sum(1 for v in vals if v > 0)
                out["sample_got"] = len(vals)
        except Exception:
            pass

    if not out:
        return None

    score = 50.0
    inflow = out.get("inflow")
    if inflow is not None:
        # 主力净流入相对两市规模的粗略刻度:±50 亿约对应 ±12 分
        score += _clamp(inflow / 5e9 * 12, -18, 18)
    elif out.get("sample_got"):
        # 抽样口径:20 只成交额最大的票里,净流入为正的占比偏离 50% 多少
        pos = out.get("sample_pos") or 0
        got = out.get("sample_got") or 0
        ratio = pos / got if got else 0.5
        score += _clamp((ratio - 0.5) * 80, -20, 20)
        if out.get("sample_inflow"):
            score += _clamp(out["sample_inflow"] / 3e9 * 6, -8, 8)
    detail = ""
    if out.get("sample_got") and "inflow" not in out:
        detail = (f"抽样 {out['sample_got']} 只大票,净流入为正 "
                  f"{out.get('sample_pos', 0)} 只")
    if inflow is not None:
        detail = (f"主力净{'流入' if inflow >= 0 else '流出'} {abs(inflow)/1e8:.1f} 亿"
                  f"({out.get('coverage', 0)} 只覆盖)")
    if out.get("top_name"):
        detail += f";资金最集中:{out['top_name']}"
    return {"score": _clamp(score), "detail": detail, "items": out}


def _overseas() -> dict | None:
    """外围:美股/港股/商品/汇率的最近涨跌。"""
    try:
        rows = world.overseas_cached()
    except Exception:
        return None
    by = {r["code"]: r for r in rows}
    if not by:
        return None
    score = 50.0
    parts = []

    def take(code: str, label: str, w: float, invert: bool = False) -> None:
        nonlocal score
        r = by.get(code)
        if not r or r.get("change_pct") is None:
            return
        pct = float(r["change_pct"])
        score += _clamp(pct * w, -8, 8) * (-1 if invert else 1)
        parts.append(f"{label} {pct:+.2f}%")

    take("ixic", "纳指", 0.9)
    take("spx", "标普", 0.7)
    take("hsi", "恒指", 0.8)
    take("vix", "VIX", 0.6, invert=True)
    take("usdcny", "人民币", 0.5)
    take("oil", "原油", 0.3)
    take("gold", "黄金", 0.3)
    if not parts:
        return None
    return {"score": _clamp(score), "detail": "外围:" + " · ".join(parts),
            "items": {k: by.get(k, {}).get("change_pct") for k in
                      ("ixic", "spx", "hsi", "vix", "usdcny", "oil", "gold")}}


def _news_sentiment() -> dict | None:
    """消息政策:新闻里利多/利空词与政策词的出现次数。"""
    try:
        rows = world.news_cached(40)
    except Exception:
        return None
    if not rows:
        return None
    bull = bear = policy = 0
    hot: list[str] = []
    for r in rows:
        text = (r.get("title") or "") + " " + (r.get("summary") or "")
        b = sum(1 for w in BULL_WORDS if w in text)
        s = sum(1 for w in BEAR_WORDS if w in text)
        p = sum(1 for w in POLICY_WORDS if w in text)
        bull += b
        bear += s
        policy += p
        if (b or s) and len(hot) < 5:
            hot.append(f"[{r.get('tag')}] {r.get('title', '')[:44]}")
    score = 50.0 + _clamp((bull - bear) * 3.5, -20, 20) + _clamp(policy * 1.5, 0, 8)
    detail = (f"消息 {len(rows)} 条:利多词 {bull} / 利空词 {bear} / 政策词 {policy}")
    return {"score": _clamp(score), "detail": detail,
            "items": {"bull": bull, "bear": bear, "policy": policy, "hot": hot}}


def _stance(score: float) -> tuple[str, str]:
    if score >= 72:
        return "进攻", "多维共振,可正常持仓、按策略加仓"
    if score >= 58:
        return "偏多", "结构性机会,控制单票仓位"
    if score >= 42:
        return "中性", "降低换手,等信号确认"
    if score >= 28:
        return "偏空", "减仓、控制回撤,少接飞刀"
    return "防守", "以现金为主,等趋势修复"


def analyze(progress=None) -> dict:
    """跑五个维度,返回完整研判。任一段失败不影响其它段。"""
    dims: list[dict] = []
    steps = [("趋势", _index_trend), ("广度", _breadth), ("资金", _money),
             ("外围", _overseas), ("消息政策", _news_sentiment)]
    for i, (name, fn) in enumerate(steps):
        if progress:
            progress(i, len(steps))
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            res = None
            err = str(e)[:60]
        else:
            err = ""
        if res is None:
            dims.append({"name": name, "score": None, "detail": f"取数失败{(':' + err) if err else ''}",
                         "weight": WEIGHTS[name], "items": {}})
            continue
        dims.append({"name": name, "score": float(res["score"]),
                     "detail": res.get("detail", ""), "weight": WEIGHTS[name],
                     "items": res.get("items", {})})

    live = [d for d in dims if d["score"] is not None]
    if not live:
        raise RuntimeError("五个维度全部取数失败,检查网络后重试")
    wsum = sum(d["weight"] for d in live)
    total = sum(d["score"] * d["weight"] for d in live) / wsum if wsum else 50.0
    stance, advice = _stance(total)

    highlights: list[str] = []
    risks: list[str] = []
    for d in live:
        if d["score"] >= 65:
            highlights.append(f"{d['name']}:{d['detail']}")
        elif d["score"] <= 35:
            risks.append(f"{d['name']}:{d['detail']}")

    news = next((d for d in dims if d["name"] == "消息政策" and d["items"]), None)
    if news:
        highlights += ["消息 · " + h for h in (news["items"].get("hot") or [])[:3]]

    return {
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": round(total, 1),
        "stance": stance,
        "advice": advice,
        "dims": dims,
        "highlights": highlights[:6],
        "risks": risks[:6],
        "missing": [d["name"] for d in dims if d["score"] is None],
    }


def to_text(rep: dict) -> str:
    """给界面/日志用的一段话。"""
    lines = [f"大盘研判 {rep['asof']} · 总分 {rep['score']} · {rep['stance']}",
             rep["advice"]]
    for d in rep["dims"]:
        s = "—" if d["score"] is None else f"{d['score']:.0f}"
        lines.append(f"  {d['name']:4s} {s:>3s}  {d['detail']}")
    if rep["risks"]:
        lines.append("风险:" + ";".join(rep["risks"][:3]))
    return "\n".join(lines)
