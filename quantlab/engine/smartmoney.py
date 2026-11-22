# -*- coding: utf-8 -*-
"""智能资金(订单块 / 流动性 / 失衡缺口)。

概念来源是 ICT/SMC 那套,但这里只做「能在 OHLCV 上算出来」的部分,
**不做任何机构持仓推断** —— 数据里没有订单流,只有价与量,这点必须说清楚:

- 订单块(order block):一根有力度的脱离蜡烛之前、方向相反的那根蜡烛的高低价区间。
  看涨订单块 = 大阳线前最后一根阴线的区间;看跌订单块 = 大阴线前最后一根阳线的区间。
  「未回补」指价格之后没有回到该区间。
- 流动性(liquidity):多个几乎等高/等低的极值 —— 止损单通常堆在这些位置,
  被扫掉(价格刺穿后快速收回)就是一次流动性猎取。
- 失衡缺口(FVG):三根 K 线里第一根的高点低于第三根的低点(向上缺口),
  中间留下的真空区,常见于急涨急跌,后续容易回补。
"""
from __future__ import annotations

from ..utils import safe_float

EQ_TOL = 0.003       # 等高等低的容差 0.3%
DISPLACE = 1.6       # 脱离蜡烛:实体或振幅达到近期均值的 1.6 倍
SWING = 4


def _ohlc(bars: list[dict]) -> list[dict]:
    out = []
    for b in bars:
        o, h, l, c = (safe_float(b.get("open")), safe_float(b.get("high")),
                      safe_float(b.get("low")), safe_float(b.get("close")))
        if h <= 0 or l <= 0:
            continue
        out.append({"o": o, "h": h, "l": l, "c": c,
                    "v": safe_float(b.get("volume")),
                    "dt": str(b.get("datetime") or "")[:10]})
    return out


def swings(rows: list[dict], span: int = SWING) -> tuple[list[dict], list[dict]]:
    highs, lows = [], []
    for i in range(span, len(rows) - span):
        h, l = rows[i]["h"], rows[i]["l"]
        if h >= max(r["h"] for r in rows[i - span:i]) and \
           h >= max(r["h"] for r in rows[i + 1:i + span + 1]):
            highs.append({"i": i, "price": h, "dt": rows[i]["dt"]})
        if l <= min(r["l"] for r in rows[i - span:i]) and \
           l <= min(r["l"] for r in rows[i + 1:i + span + 1]):
            lows.append({"i": i, "price": l, "dt": rows[i]["dt"]})
    return highs, lows


def structure(highs: list[dict], lows: list[dict]) -> str:
    if len(highs) < 2 or len(lows) < 2:
        return "不明"
    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    if hh and hl:
        return "HH/HL · 上升结构"
    if not hh and not hl:
        return "LH/LL · 下降结构"
    return "结构交叉 · 方向待定"


def order_blocks(rows: list[dict], look: int = 120) -> list[dict]:
    """找订单块:有力度的脱离蜡烛 + 它前面那根反向蜡烛。"""
    if len(rows) < 20:
        return []
    seg = rows[-look:] if len(rows) > look else rows[:]
    base = len(rows) - len(seg)
    bodies = [abs(r["c"] - r["o"]) for r in seg]
    avg_body = sum(bodies) / max(1, len(bodies))
    out = []
    for i in range(1, len(seg) - 1):
        r = seg[i]
        body = abs(r["c"] - r["o"])
        if body < avg_body * DISPLACE or avg_body <= 0:
            continue
        prev = seg[i - 1]
        bull = r["c"] > r["o"]
        # 大阳线 → 找前面的阴线;大阴线 → 找前面的阳线
        if bull and prev["c"] < prev["o"]:
            zone_hi, zone_lo, kind = prev["h"], prev["l"], "看涨订单块"
        elif (not bull) and prev["c"] > prev["o"]:
            zone_hi, zone_lo, kind = prev["h"], prev["l"], "看跌订单块"
        else:
            continue
        # 之后有没有回到这个区间(回补)
        touched = any(x["l"] <= zone_hi and x["h"] >= zone_lo for x in seg[i + 1:])
        out.append({"kind": kind, "hi": zone_hi, "lo": zone_lo,
                    "i": base + i - 1, "dt": prev["dt"], "filled": touched,
                    "strength": body / avg_body})
    return out[-8:]


def liquidity(highs: list[dict], lows: list[dict], rows: list[dict]) -> list[dict]:
    """等高等低:距离在容差内的极值聚成一组。"""
    out = []

    def group(items, side):
        used = set()
        for i in range(len(items)):
            if i in used:
                continue
            cluster = [items[i]]
            for j in range(i + 1, len(items)):
                if j in used:
                    continue
                a, b = items[i]["price"], items[j]["price"]
                if a > 0 and abs(b / a - 1) <= EQ_TOL:
                    cluster.append(items[j])
                    used.add(j)
            if len(cluster) >= 2:
                price = sum(x["price"] for x in cluster) / len(cluster)
                out.append({"side": side, "price": price, "n": len(cluster),
                            "dt": cluster[-1]["dt"],
                            "detail": f"{side} {price:.2f} 附近有 {len(cluster)} 次触及"})

    group(highs, "等高点")
    group(lows, "等低点")
    # 最近一根有没有扫掉这些位置
    if rows:
        last = rows[-1]
        for g in out:
            if g["side"] == "等高点" and last["h"] > g["price"] and last["c"] < g["price"]:
                g["swept"] = "价格刺穿后收回,疑似流动性猎取"
            elif g["side"] == "等低点" and last["l"] < g["price"] and last["c"] > g["price"]:
                g["swept"] = "价格刺穿后收回,疑似流动性猎取"
    return out[-6:]


def imbalances(rows: list[dict], look: int = 60) -> list[dict]:
    """FVG:三根 K 线的真空区。"""
    out = []
    seg = rows[-look:]
    for i in range(2, len(seg)):
        a, c = seg[i - 2], seg[i]
        if a["h"] < c["l"]:
            gap_lo, gap_hi = a["h"], c["l"]
            filled = any(x["l"] <= gap_lo for x in seg[i + 1:])
            out.append({"dir": "向上", "lo": gap_lo, "hi": gap_hi, "dt": a["dt"],
                        "filled": filled, "size_pct": (gap_hi / gap_lo - 1) * 100 if gap_lo else 0})
        elif a["l"] > c["h"]:
            gap_lo, gap_hi = c["h"], a["l"]
            filled = any(x["h"] >= gap_hi for x in seg[i + 1:])
            out.append({"dir": "向下", "lo": gap_lo, "hi": gap_hi, "dt": a["dt"],
                        "filled": filled, "size_pct": (gap_hi / gap_lo - 1) * 100 if gap_lo else 0})
    return out[-6:]


def analyze(bars: list[dict]) -> dict:
    rows = _ohlc(bars)
    if len(rows) < 20:
        return {"ok": False, "reason": f"K 线太少({len(rows)} 根),至少 20 根"}
    highs, lows = swings(rows)
    obs = order_blocks(rows)
    liq = liquidity(highs, lows, rows)
    fvg = imbalances(rows)
    open_obs = [o for o in obs if not o["filled"]]
    return {"ok": True, "structure": structure(highs, lows), "highs": highs,
            "lows": lows, "order_blocks": obs, "open_blocks": open_obs,
            "liquidity": liq, "imbalances": fvg, "bars": len(rows),
            "ob_n": len(obs), "fvg_n": len(fvg)}


def describe(rep: dict) -> str:
    if not rep.get("ok"):
        return rep.get("reason") or "算不出来"
    parts = [f"结构 {rep['structure']}"]
    if rep["open_blocks"]:
        b = rep["open_blocks"][-1]
        parts.append(f"最近未回补的{b['kind']} {b['lo']:.2f}~{b['hi']:.2f}"
                     f"({b['dt']} 形成,力度 {b['strength']:.1f}×)")
    else:
        parts.append("订单块都已回补")
    if rep["liquidity"]:
        g = rep["liquidity"][-1]
        parts.append(g.get("swept") or g["detail"])
    unfilled = [f for f in rep["imbalances"] if not f["filled"]]
    if unfilled:
        f = unfilled[-1]
        parts.append(f"{f['dir']}缺口 {f['lo']:.2f}~{f['hi']:.2f} 未回补")
    return " · ".join(parts)
