# -*- coding: utf-8 -*-
"""综合评判:下单前过一遍市场/资金/走势/公司,不只看策略信号。

裁判层的判断不是替代策略,是在策略选出的票上再过一道:
  市场差 → 整体降分
  资金流出 → 该票降分
  走势破位 → 该票降分
  估值离谱 → 该票降分

只做评分和过滤,不下单。数据取不到时给中性分,不拦。
"""
from __future__ import annotations

from ..data import moneyflow
from ..data.kline import get_kline


def market_state(days: int = 60) -> dict:
    """大盘状态:上证 20 日均线方向 + 近 5 日涨跌。"""
    try:
        bars, _ = get_kline("000001", 101, count=days, is_index=True)
    except Exception:  # noqa: BLE001
        return {"ok": False, "score": 0.0, "reason": "取不到大盘"}
    if not bars or len(bars) < 25:
        return {"ok": False, "score": 0.0, "reason": "大盘数据不足"}
    closes = [float(b.get("close") or 0) for b in bars]
    ma20 = sum(closes[-20:]) / 20
    last = closes[-1]
    chg5 = last / closes[-6] - 1 if len(closes) >= 6 else 0.0
    score = 0.0
    if last > ma20:
        score += 0.5
    else:
        score -= 0.5
    if chg5 > 0.02:
        score += 0.5
    elif chg5 < -0.02:
        score -= 0.5
    return {"ok": True, "score": score, "ma20": ma20, "last": last,
            "chg5": chg5,
            "reason": f"上证 {last:.0f} vs MA20 {ma20:.0f},5日 {chg5:+.1%}"}


def evaluate(code: str, quote: dict | None = None,
             market: dict | None = None) -> dict:
    """单票综合评分。返回 {score, pass, reasons}。

    大盘分**不计入个股 score**,单独放在 mscore 返回。
    原因:大盘弱是全场共性,把它加进每只票,大盘一跌所有票同分往下压,
    filter_picks 的 -0.5 阈值会被一口气击穿 —— 实测上证跌破 MA20 且
    5 日 -3% 时,5 只票被拦 4 只,报告里写的是「这只票差」,
    实际是「大盘差、该整体降仓」。两件事必须分开说,否则调参永远调不对。
    """
    reasons: list[str] = []
    score = 0.0
    m = market or market_state()
    mscore = 0.0
    if m.get("ok"):
        mscore = float(m.get("score") or 0)
        # 大盘只作为「为什么少买」的解释,不参与个股淘汰
        reasons.append(m["reason"])

    # 资金:主力净流入
    try:
        mf = moneyflow.now(code)
        main = float(mf.get("main") or 0)
        if main > 0:
            score += 0.5
            reasons.append(f"主力净流入 {main / 1e8:.2f}亿")
        elif main < 0:
            score -= 0.5
            reasons.append(f"主力净流出 {main / 1e8:.2f}亿")
    except Exception:  # noqa: BLE001
        pass

    # 走势:20 日动量 + 位置
    try:
        bars, _ = get_kline(code, 101, count=60)
        if bars and len(bars) >= 21:
            closes = [float(b.get("close") or 0) for b in bars]
            mom20 = closes[-1] / closes[-21] - 1
            hi = max(closes)
            pos = closes[-1] / hi if hi > 0 else 1.0
            if mom20 > 0.05:
                score += 0.5
                reasons.append(f"20日动量 {mom20:+.1%}")
            elif mom20 < -0.05:
                score -= 0.5
                reasons.append(f"20日动量 {mom20:+.1%}")
            if pos < 0.85:
                score += 0.3
                reasons.append(f"距60日高点 {pos:.0%}")
            elif pos > 0.98:
                score -= 0.3
                reasons.append("接近60日高点")
    except Exception:  # noqa: BLE001
        pass

    # 公司:估值(PE)
    q = quote or {}
    pe = q.get("pe")
    if pe is not None:
        try:
            pe = float(pe)
            if 0 < pe < 30:
                score += 0.2
            elif pe > 100 or pe < 0:
                score -= 0.2
                reasons.append(f"PE {pe:.0f} 偏高/为负")
        except Exception:  # noqa: BLE001
            pass

    return {"code": code, "score": round(score, 2), "mscore": round(mscore, 2),
            "pass": score >= 0.0, "reasons": [r for r in reasons if r]}


def filter_picks(picks: list[tuple[str, float]], quotes: dict,
                 min_score: float = -0.5) -> tuple[list, list]:
    """对策略选出的票过一遍综合评判。返回 (放行, 拦下)。

    阈值 -0.5:只拦明显差的票。大盘分单独算(见 evaluate),
    不参与这个阈值 —— 大盘弱时该「整体少买」,不是「逐只淘汰」。

    并发拉数据:每只票要资金流 + K 线两次网络请求,串行 10 只要十几秒。
    """
    from concurrent.futures import ThreadPoolExecutor
    m = market_state()

    def one(item):
        code, strength = item
        return code, strength, evaluate(code, quotes.get(code), m)

    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(one, picks))
    except Exception:  # noqa: BLE001
        results = [one(item) for item in picks]

    keep, drop = [], []
    for code, strength, r in results:
        if r["score"] >= min_score:
            keep.append((code, strength))
        else:
            drop.append({"code": code, "score": r["score"],
                         "reasons": r["reasons"]})
    return keep, drop
