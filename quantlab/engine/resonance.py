# -*- coding: utf-8 -*-
"""共振面板:多个独立指标各自投票,看方向是否一致。

每条投票给 +1(看多)/ 0(中性)/ -1(看空),再汇总:
- 总分 >= 4 且看多占多数 → 多头共振
- 总分 <= -4 且看空占多数 → 空头共振
- 其余 → 分歧

**这不是预测模型**,只是一张把常见指标摆在一起看的表:
一致性高只说明「这些指标在同一个方向上」,不说明接下来一定往那边走 ——
所有指标都从价格派生,同涨同跌本来就正常。它的用处是快速排除「说法互相矛盾」的标的。
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def _last(series) -> float | None:
    try:
        v = float(series.iloc[-1])
    except Exception:  # noqa: BLE001
        return None
    return None if pd.isna(v) else v


def votes(bars: list[dict]) -> list[dict]:
    if not bars or len(bars) < 30:
        return []
    df = ind.bars_to_df(bars)
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    close = _last(c) or 0.0
    out: list[dict] = []

    def add(name, vote, why):
        out.append({"name": name, "vote": vote, "why": why})

    # 1. 均线排列
    ma5, ma10, ma20, ma60 = (_last(ind.ma(c, n)) for n in (5, 10, 20, 60))
    if None not in (ma5, ma10, ma20, ma60):
        if ma5 > ma10 > ma20 > ma60:
            add("均线排列", 1, "MA5>MA10>MA20>MA60,多头排列")
        elif ma5 < ma10 < ma20 < ma60:
            add("均线排列", -1, "MA5<MA10<MA20<MA60,空头排列")
        else:
            add("均线排列", 0, "均线交织,无明确排列")

    # 2. 价格与 MA20
    ma20v = _last(ind.ma(c, 20))
    if ma20v:
        gap = (close / ma20v - 1) * 100
        add("价格位置", 1 if gap > 0 else -1 if gap < 0 else 0,
            f"现价 {close:.2f} 在 MA20 {ma20v:.2f} 的{'上' if gap > 0 else '下'}方 {abs(gap):.1f}%")

    # 3. MACD
    dif, dea, hist = ind.macd(c)
    d, e, hh = _last(dif), _last(dea), _last(hist)
    if None not in (d, e, hh):
        if d > e and hh > 0:
            add("MACD", 1, f"DIF {d:.3f} 在 DEA {e:.3f} 上方,红柱")
        elif d < e and hh < 0:
            add("MACD", -1, f"DIF {d:.3f} 在 DEA {e:.3f} 下方,绿柱")
        else:
            add("MACD", 0, "DIF 与 DEA 粘合")

    # 4. KDJ
    k, dd, j = ind.kdj(h, l, c)
    kv, dv = _last(k), _last(dd)
    if None not in (kv, dv):
        if kv > dv and kv < 80:
            add("KDJ", 1, f"K {kv:.1f} 上穿 D {dv:.1f},未超买")
        elif kv < dv and kv > 20:
            add("KDJ", -1, f"K {kv:.1f} 下穿 D {dv:.1f},未超卖")
        else:
            add("KDJ", 0, f"K {kv:.1f} / D {dv:.1f},处于极值区")

    # 5. RSI
    rv = _last(ind.rsi(c, 14))
    if rv is not None:
        if rv > 70:
            add("RSI", -1, f"RSI {rv:.1f} 超买,追高风险大")
        elif rv < 30:
            add("RSI", 1, f"RSI {rv:.1f} 超卖,存在反弹条件")
        else:
            add("RSI", 1 if rv > 50 else -1, f"RSI {rv:.1f} 在{'强' if rv > 50 else '弱'}势区")

    # 6. 布林带位置
    up, mid, low = ind.boll(c)
    uv, lv = _last(up), _last(low)
    if None not in (uv, lv) and uv > lv:
        pos = (close - lv) / (uv - lv)
        if pos > 0.85:
            add("布林带", -1, f"贴近上轨({pos:.0%}),短线过热")
        elif pos < 0.15:
            add("布林带", 1, f"贴近下轨({pos:.0%}),短线超跌")
        else:
            add("布林带", 0, f"位于带内 {pos:.0%} 处")

    # 7. 量能
    v5, v20 = _last(ind.ma(v, 5)), _last(ind.ma(v, 20))
    if v5 and v20:
        ratio = v5 / v20
        chg = (close / float(c.iloc[-6]) - 1) * 100 if len(c) > 6 else 0.0
        if ratio > 1.2:
            add("量能", 1 if chg > 0 else -1,
                f"5 日均量是 20 日的 {ratio:.2f} 倍,放量{'上涨' if chg > 0 else '下跌'}")
        elif ratio < 0.8:
            add("量能", 0, f"缩量({ratio:.2f} 倍),方向待确认")
        else:
            add("量能", 0, f"量能平稳({ratio:.2f} 倍)")

    # 8. ATR 波动位置
    atr_v = _last(ind.atr(h, l, c, 14))
    if atr_v and close:
        add("波动率", 0, f"ATR14 {atr_v:.2f},约占股价 {atr_v / close:.2%}")
    return out


def analyze(bars: list[dict]) -> dict:
    vs = votes(bars)
    if not vs:
        return {"ok": False, "reason": "K 线太少,至少 30 根"}
    bull = sum(1 for x in vs if x["vote"] > 0)
    bear = sum(1 for x in vs if x["vote"] < 0)
    flat = len(vs) - bull - bear
    score = bull - bear
    if score >= 3 and bull > bear:
        verdict, level = "多头共振", "bull"
    elif score <= -3 and bear > bull:
        verdict, level = "空头共振", "bear"
    elif abs(score) <= 1:
        verdict, level = "多空分歧", "flat"
    else:
        verdict, level = "略微偏" + ("多" if score > 0 else "空"), "lean"
    return {"ok": True, "votes": vs, "bull": bull, "bear": bear, "flat": flat,
            "score": score, "verdict": verdict, "level": level,
            "total": len(vs)}


def describe(rep: dict) -> str:
    if not rep.get("ok"):
        return rep.get("reason") or "算不出来"
    return (f"{rep['verdict']}(看多 {rep['bull']} · 看空 {rep['bear']} · "
            f"中性 {rep['flat']},净分 {rep['score']:+d})")
