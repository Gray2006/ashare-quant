# -*- coding: utf-8 -*-
"""道氏理论:趋势线、高低点结构、2B 假突破。

定义:(都按收盘/最高价算,不用未来数据)
- 结构:连续抬高的高点与低点 = 上升结构(HH+HL);连续降低 = 下降结构。
- 趋势线:用最近的两个显著低点连成上升支撑线(或两个高点连成下降压力线),
  判断「当前价在这条线的哪一侧、离多远」,不预测会不会破。
- 2B 假突破:价格创出前高之后又收回前高之下(且幅度超过阈值),视为多头陷阱;
  反过来创出新低后收回新低之上 = 空头陷阱。这是维克托的 2B 法则。
"""
from __future__ import annotations

from ..utils import safe_float

SWING = 5          # 显著高低点的左右确认窗口
FAKE_PCT = 0.005   # 假突破至少要收回 0.5%


def _swings(bars: list[dict], span: int = SWING) -> tuple[list[dict], list[dict]]:
    """找局部显著高点/低点:左右各 span 根都不超过它。"""
    highs, lows = [], []
    n = len(bars)
    for i in range(span, n - span):
        h = safe_float(bars[i].get("high"))
        l = safe_float(bars[i].get("low"))
        if h <= 0:
            continue
        left_h = [safe_float(bars[j].get("high")) for j in range(i - span, i)]
        right_h = [safe_float(bars[j].get("high")) for j in range(i + 1, i + span + 1)]
        left_l = [safe_float(bars[j].get("low")) for j in range(i - span, i)]
        right_l = [safe_float(bars[j].get("low")) for j in range(i + 1, i + span + 1)]
        if h >= max(left_h + right_h):
            highs.append({"i": i, "price": h,
                          "datetime": str(bars[i].get("datetime") or "")[:10]})
        if l > 0 and l <= min(left_l + right_l):
            lows.append({"i": i, "price": l,
                         "datetime": str(bars[i].get("datetime") or "")[:10]})
    return highs, lows


def structure(highs: list[dict], lows: list[dict]) -> dict:
    """由最近若干高低点判断结构。"""
    if len(highs) < 2 or len(lows) < 2:
        return {"kind": "不明", "detail": "显著高低点不足两个,结构判断不了"}
    h2, h1 = highs[-2], highs[-1]
    l2, l1 = lows[-2], lows[-1]
    higher_high, higher_low = h1["price"] > h2["price"], l1["price"] > l2["price"]
    lower_high, lower_low = h1["price"] < h2["price"], l1["price"] < l2["price"]
    if higher_high and higher_low:
        return {"kind": "上升结构", "detail": f"高点 {h2['price']:.2f}→{h1['price']:.2f},"
                                             f"低点 {l2['price']:.2f}→{l1['price']:.2f} 同步抬高"}
    if lower_high and lower_low:
        return {"kind": "下降结构", "detail": f"高点 {h2['price']:.2f}→{h1['price']:.2f},"
                                             f"低点 {l2['price']:.2f}→{l1['price']:.2f} 同步降低"}
    if higher_high and lower_low:
        return {"kind": "扩散", "detail": "高低点同时外扩,波动放大、方向不明"}
    return {"kind": "收敛", "detail": "高低点同时内收,震荡收窄后通常有方向选择"}


def trendlines(highs: list[dict], lows: list[dict]) -> list[dict]:
    """用最近两个低点/高点连线,给出斜率与当前价的位置。"""
    out = []
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if b["i"] != a["i"]:
            slope = (b["price"] - a["price"]) / (b["i"] - a["i"])
            out.append({"kind": "上升支撑线", "a": a, "b": b, "slope": slope})
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if b["i"] != a["i"]:
            slope = (b["price"] - a["price"]) / (b["i"] - a["i"])
            out.append({"kind": "下降压力线", "a": a, "b": b, "slope": slope})
    return out


def line_pos(line: dict, bars: list[dict]) -> dict:
    """当前价相对趋势线的位置(按线的延长值算)。"""
    n = len(bars)
    last_i = n - 1
    value = line["b"]["price"] + line["slope"] * (last_i - line["b"]["i"])
    close = safe_float(bars[-1].get("close"))
    if value <= 0 or close <= 0:
        return {"value": value, "close": close, "gap_pct": 0.0, "side": "不明"}
    gap = (close / value - 1) * 100
    side = "上方" if gap > 0 else "下方"
    return {"value": value, "close": close, "gap_pct": gap, "side": side}


def fake_breakouts(bars: list[dict], highs: list[dict], lows: list[dict]) -> list[dict]:
    """2B 假突破:破前高后收回、破前低后收回。"""
    out = []
    if len(bars) < 3:
        return out
    close = safe_float(bars[-1].get("close"))
    high = safe_float(bars[-1].get("high"))
    low = safe_float(bars[-1].get("low"))
    if highs:
        prev = highs[-1]["price"]
        if high > prev and close < prev * (1 - FAKE_PCT):
            out.append({"kind": "2B 顶(假突破)", "level": prev,
                        "detail": f"盘中冲到 {high:.2f} 越过前高 {prev:.2f},"
                                  f"收在 {close:.2f},没收住就是多头陷阱"})
    if lows:
        prev = lows[-1]["price"]
        if low < prev and close > prev * (1 + FAKE_PCT):
            out.append({"kind": "2B 底(假跌破)", "level": prev,
                        "detail": f"盘中砸到 {low:.2f} 破前低 {prev:.2f},"
                                  f"收在 {close:.2f},没收住就是空头陷阱"})
    return out


def analyze(bars: list[dict]) -> dict:
    if not bars or len(bars) < 20:
        return {"ok": False, "reason": f"K 线太少({len(bars) if bars else 0} 根)"}
    highs, lows = _swings(bars)
    st = structure(highs, lows)
    lines_ = trendlines(highs, lows)
    pos = [{"line": ln, **line_pos(ln, bars)} for ln in lines_]
    fake = fake_breakouts(bars, highs, lows)
    return {"ok": True, "highs": highs, "lows": lows, "structure": st,
            "trendlines": lines_, "positions": pos, "fakes": fake,
            "bars": len(bars)}


def describe(rep: dict) -> str:
    if not rep.get("ok"):
        return rep.get("reason") or "算不出来"
    parts = [f"结构:{rep['structure']['kind']}"]
    for p in rep["positions"]:
        parts.append(f"{p['line']['kind']} 延长值 {p['value']:.2f},"
                     f"现价在其{p['side']} {abs(p['gap_pct']):.1f}%")
    for f in rep["fakes"]:
        parts.append(f"{f['kind']}:{f['detail']}")
    return " · ".join(parts)
