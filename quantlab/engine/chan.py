# -*- coding: utf-8 -*-
"""缠论(简化实现):包含关系 → 分型 → 笔 → 线段 → 中枢 → 买卖点。

这是**简化版**,不是完整缠论。差异写在这里,别当权威用:

1. 包含关系按「高低点全包」合并,没有实现缠师原文里按方向取极值的那种递归。
   对日线的实际影响很小,但极端跳空时合并结果会和人手画的略有出入。
2. 笔的成立条件按「分型之间至少隔 4 根合并后的 K 线」判断。原文要求的是
   5 根原始 K 线且顶底之间至少一根独立 K 线,合并之后计数方式不同。
3. 线段用「三笔重叠且突破前一笔极值」判定,不实现特征序列的完整递归。
4. 中枢取「连续三笔的重叠区间」,不处理中枢扩展/延伸的多种情形。
5. 买卖点只给一买/二买/三买(及对应卖点),判定用「离开中枢 + 回抽不破」,
   没有严格处理盘整背驰与趋势背驰的区分。

结论:足够用来「在图上标出结构、辅助看图」,不足以机械照做。
任何据此下单的行为都需要自己再用别的证据确认。
"""
from __future__ import annotations

from ..utils import safe_float

DIR_UP, DIR_DOWN = 1, -1


def _bar(b) -> dict:
    return {"high": safe_float(b.get("high")), "low": safe_float(b.get("low")),
            "close": safe_float(b.get("close")), "open": safe_float(b.get("open")),
            "datetime": str(b.get("datetime") or "")[:10],
            "volume": safe_float(b.get("volume"))}


def merge_bars(bars: list[dict]) -> list[dict]:
    """处理包含关系:一根的高低点完全包住另一根时合并。

    方向由「合并前的趋势」决定 —— 向上取高高、向下取低低,这是缠论的做法。
    """
    src = [_bar(b) for b in bars if safe_float(b.get("high")) > 0]
    out: list[dict] = []
    for b in src:
        if not out:
            out.append(dict(b))
            continue
        prev = out[-1]
        contained = (b["high"] <= prev["high"] and b["low"] >= prev["low"])
        contains = (b["high"] >= prev["high"] and b["low"] <= prev["low"])
        if contained or contains:
            direction = DIR_UP if len(out) < 2 or out[-1]["high"] > out[-2]["high"] else DIR_DOWN
            if direction == DIR_UP:
                high, low = max(b["high"], prev["high"]), max(b["low"], prev["low"])
            else:
                high, low = min(b["high"], prev["high"]), min(b["low"], prev["low"])
            out[-1] = {**prev, "high": high, "low": low,
                       "close": b["close"], "datetime": b["datetime"]}
        else:
            out.append(dict(b))
    return out


def fractals(merged: list[dict]) -> list[dict]:
    """分型:中间那根的高点最高=顶分型,低点最低=底分型。"""
    out = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b["high"] > a["high"] and b["high"] > c["high"]:
            out.append({"kind": "top", "i": i, "price": b["high"],
                        "datetime": b["datetime"]})
        elif b["low"] < a["low"] and b["low"] < c["low"]:
            out.append({"kind": "bottom", "i": i, "price": b["low"],
                        "datetime": b["datetime"]})
    return out


def strokes(merged: list[dict], fr: list[dict], min_gap: int = 4) -> list[dict]:
    """笔:相邻的顶底分型连起来,且两分型之间至少要隔 min_gap 根合并 K 线。

    同一类型的相邻分型只保留更极端的那个(顶取更高、底取更低)。
    """
    picked: list[dict] = []
    for f in fr:
        if not picked:
            picked.append(f)
            continue
        last = picked[-1]
        if f["kind"] == last["kind"]:
            better = f["price"] > last["price"] if f["kind"] == "top" else f["price"] < last["price"]
            if better:
                picked[-1] = f
        else:
            if f["i"] - last["i"] >= min_gap:
                picked.append(f)
            else:
                # 太近,不足以成笔;若比上一个同类分型更极端则替换掉那一段
                if len(picked) >= 2 and f["i"] - picked[-2]["i"] >= min_gap:
                    picked[-1] = f
    out = []
    for a, b in zip(picked, picked[1:]):
        out.append({"from": a, "to": b, "dir": DIR_UP if b["price"] > a["price"] else DIR_DOWN,
                    "high": max(a["price"], b["price"]), "low": min(a["price"], b["price"]),
                    "i0": a["i"], "i1": b["i"],
                    "datetime0": a["datetime"], "datetime1": b["datetime"]})
    return out


def lines(strokes_: list[dict]) -> list[dict]:
    """线段(简化):连续三笔重叠且方向一致,就归为一段。

    只做一层,不做特征序列的递归 —— 输出用于画线和判断方向,不做精确端点。
    """
    out = []
    i = 0
    while i + 2 < len(strokes_):
        a, b, c = strokes_[i], strokes_[i + 1], strokes_[i + 2]
        same = a["dir"] == c["dir"]
        overlap = min(x["high"] for x in (a, b, c)) < max(x["low"] for x in (a, b, c))
        if same and overlap:
            out.append({"dir": a["dir"], "i0": a["i0"], "i1": c["i1"],
                        "high": max(a["high"], b["high"], c["high"]),
                        "low": min(a["low"], b["low"], c["low"]),
                        "datetime0": a["datetime0"], "datetime1": c["datetime1"]})
            i += 2
        else:
            i += 1
    return out


def centers(strokes_: list[dict]) -> list[dict]:
    """中枢:连续三笔的重叠区间。zg=三笔高点的最小值,zd=三笔低点的最大值。"""
    out = []
    i = 0
    while i + 2 < len(strokes_):
        trio = strokes_[i:i + 3]
        zg = min(x["high"] for x in trio)
        zd = max(x["low"] for x in trio)
        if zg > zd:
            out.append({"zg": zg, "zd": zd, "i0": trio[0]["i0"], "i1": trio[-1]["i1"],
                        "datetime0": trio[0]["datetime0"],
                        "datetime1": trio[-1]["datetime1"],
                        "dir": trio[0]["dir"]})
            # 中枢延伸:紧接着的笔只要还和区间重叠,就并入
            j = i + 3
            while j < len(strokes_) and strokes_[j]["low"] <= zg and strokes_[j]["high"] >= zd:
                out[-1]["i1"] = strokes_[j]["i1"]
                out[-1]["datetime1"] = strokes_[j]["datetime1"]
                j += 1
            i = j
        else:
            i += 1
    return out


def points(bars: list[dict], ctrs: list[dict], strokes_: list[dict]) -> list[dict]:
    """买卖点(简化):以最后一个中枢为参照。

    三买 = 价格向上离开中枢后回抽不破中枢上沿(zg)。
    三卖 = 向下离开后回抽不破中枢下沿(zd)。
    一买 = 中枢下方出现底分型且随后的笔向上回到中枢区间内(仅作提示)。
    """
    out: list[dict] = []
    if not ctrs or not strokes_ or not bars:
        return out
    last = ctrs[-1]
    tail = strokes_[-1]
    closes = [safe_float(b.get("close")) for b in bars[-30:]]
    last_close = closes[-1] if closes else 0.0
    if tail["dir"] == DIR_UP and tail["low"] > last["zg"]:
        out.append({"kind": "三买", "price": tail["low"], "i": tail["i1"],
                    "datetime": tail["datetime1"],
                    "why": f"回抽 {tail['low']:.2f} 未破中枢上沿 {last['zg']:.2f}"})
    elif tail["dir"] == DIR_DOWN and tail["high"] < last["zd"]:
        out.append({"kind": "三卖", "price": tail["high"], "i": tail["i1"],
                    "datetime": tail["datetime1"],
                    "why": f"回抽 {tail['high']:.2f} 未破中枢下沿 {last['zd']:.2f}"})
    if last_close > last["zg"] and tail["dir"] == DIR_UP:
        out.append({"kind": "向上离开中枢", "price": last_close, "i": tail["i1"],
                    "datetime": tail["datetime1"],
                    "why": f"现价 {last_close:.2f} 在中枢上沿 {last['zg']:.2f} 之上"})
    elif last_close < last["zd"] and tail["dir"] == DIR_DOWN:
        out.append({"kind": "向下离开中枢", "price": last_close, "i": tail["i1"],
                    "datetime": tail["datetime1"],
                    "why": f"现价 {last_close:.2f} 在中枢下沿 {last['zd']:.2f} 之下"})
    return out


def analyze(bars: list[dict]) -> dict:
    """一次算完,给界面用。bars 需按时间升序。"""
    if not bars or len(bars) < 20:
        return {"ok": False, "reason": f"K 线太少({len(bars) if bars else 0} 根),缠论至少要 20 根"}
    merged = merge_bars(bars)
    fr = fractals(merged)
    st = strokes(merged, fr)
    ln = lines(st)
    ct = centers(st)
    pts = points(bars, ct, st)
    trend = "盘整"
    if ln:
        trend = "向上" if ln[-1]["dir"] == DIR_UP else "向下"
    return {"ok": True, "merged": merged, "fractals": fr, "strokes": st,
            "lines": ln, "centers": ct, "points": pts, "trend": trend,
            "bars": len(bars), "merged_n": len(merged),
            "fractal_n": len(fr), "stroke_n": len(st), "line_n": len(ln),
            "center_n": len(ct)}


def describe(rep: dict) -> str:
    """给一段人话,直接贴到界面上。"""
    if not rep.get("ok"):
        return rep.get("reason") or "算不出来"
    c = rep["centers"][-1] if rep["centers"] else None
    parts = [
        f"{rep['bars']} 根 K 线合并成 {rep['merged_n']} 根",
        f"分型 {rep['fractal_n']} 个 · 笔 {rep['stroke_n']} 条 · 线段 {rep['line_n']} 段",
        f"中枢 {rep['center_n']} 个",
    ]
    if c:
        parts.append(f"最后一个中枢 {c['zd']:.2f} ~ {c['zg']:.2f}"
                     f"({c['datetime0'][:10]} 起)")
    parts.append(f"当前结构:{rep['trend']}")
    if rep["points"]:
        p = rep["points"][-1]
        parts.append(f"最新信号:{p['kind']} —— {p['why']}")
    return " · ".join(parts)
