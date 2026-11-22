# -*- coding: utf-8 -*-
"""背离:价格创新高/新低,指标没跟上。

做法:找价格的显著高(低)点,取该点的 MACD 柱/RSI 值;相邻两个同类极值比较 ——
价格更高而指标更低 = 顶背离(看跌);价格更低而指标更高 = 底背离(看涨)。

注意:
- 只比较**相邻**两个同类极值,不去找更早的极值配对,所以会漏掉跨越多波的背离。
- 极值用「左右各 n 根都不超过」判定,窗口外的极值不参与。
- 背离只说明「上涨/下跌的力度在衰减」,**不等于**马上反转。
"""
from __future__ import annotations

from . import indicators as ind
from ..utils import safe_float

SPAN = 5


def _series(bars: list[dict]) -> dict:
    import pandas as pd

    close = [safe_float(b.get("close")) for b in bars]
    high = [safe_float(b.get("high")) or c for b, c in zip(bars, close)]
    low = [safe_float(b.get("low")) or c for b, c in zip(bars, close)]
    cs = pd.Series(close, dtype=float)
    _dif, _dea, hist = ind.macd(cs)
    rsi = ind.rsi(cs)
    return {"close": close, "high": high, "low": low,
            "macd": _dif.tolist(), "hist": hist.tolist(), "rsi": rsi.tolist()}


def _peaks(values: list[float], span: int = SPAN) -> list[int]:
    out = []
    for i in range(span, len(values) - span):
        v = values[i]
        if v is None:
            continue
        left = [x for x in values[i - span:i] if x is not None]
        right = [x for x in values[i + 1:i + span + 1] if x is not None]
        if left and right and v >= max(left) and v >= max(right):
            out.append(i)
    return out


def _troughs(values: list[float], span: int = SPAN) -> list[int]:
    out = []
    for i in range(span, len(values) - span):
        v = values[i]
        if v is None:
            continue
        left = [x for x in values[i - span:i] if x is not None]
        right = [x for x in values[i + 1:i + span + 1] if x is not None]
        if left and right and v <= min(left) and v <= min(right):
            out.append(i)
    return out


def _val(series: list, i: int):
    if i < 0 or i >= len(series):
        return None
    return series[i]


def detect(bars: list[dict]) -> list[dict]:
    if len(bars) < 40:
        return []
    s = _series(bars)
    out = []
    # 顶背离:价格高点抬高,指标高点降低
    hi_idx = _peaks(s["high"])
    for a, b in zip(hi_idx, hi_idx[1:]):
        pa, pb = s["high"][a], s["high"][b]
        for name, ser in (("MACD 柱", s["hist"]), ("RSI", s["rsi"])):
            va, vb = _val(ser, a), _val(ser, b)
            if va is None or vb is None:
                continue
            if pb > pa and vb < va:
                out.append({"kind": "顶背离", "indicator": name,
                            "i": b, "datetime": str(bars[b].get("datetime") or "")[:10],
                            "price_now": pb, "price_prev": pa,
                            "ind_now": vb, "ind_prev": va,
                            "detail": f"价格 {pa:.2f}→{pb:.2f} 抬高,{name} "
                                      f"{va:.3f}→{vb:.3f} 走低"})
                break
    # 底背离:价格低点降低,指标低点抬高
    lo_idx = _troughs(s["low"])
    for a, b in zip(lo_idx, lo_idx[1:]):
        pa, pb = s["low"][a], s["low"][b]
        for name, ser in (("MACD 柱", s["hist"]), ("RSI", s["rsi"])):
            va, vb = _val(ser, a), _val(ser, b)
            if va is None or vb is None:
                continue
            if pb < pa and vb > va:
                out.append({"kind": "底背离", "indicator": name,
                            "i": b, "datetime": str(bars[b].get("datetime") or "")[:10],
                            "price_now": pb, "price_prev": pa,
                            "ind_now": vb, "ind_prev": va,
                            "detail": f"价格 {pa:.2f}→{pb:.2f} 走低,{name} "
                                      f"{va:.3f}→{vb:.3f} 抬高"})
                break
    return out


def analyze(bars: list[dict]) -> dict:
    if not bars or len(bars) < 40:
        return {"ok": False, "reason": f"K 线太少({len(bars) if bars else 0} 根),至少 40 根"}
    items = detect(bars)
    last = items[-1] if items else None
    recent = [x for x in items if x["i"] >= len(bars) - 10]
    return {"ok": True, "items": items, "last": last, "recent": recent,
            "top_n": sum(1 for x in items if x["kind"] == "顶背离"),
            "bottom_n": sum(1 for x in items if x["kind"] == "底背离")}


def describe(rep: dict) -> str:
    if not rep.get("ok"):
        return rep.get("reason") or "算不出来"
    if not rep["items"]:
        return "最近 40 根以上 K 线里没找到背离"
    parts = [f"共 {len(rep['items'])} 处(顶 {rep['top_n']} / 底 {rep['bottom_n']})"]
    if rep["recent"]:
        r = rep["recent"][-1]
        parts.append(f"最近 10 根内有{ r['kind']}({r['indicator']}):{r['detail']}")
    elif rep["last"]:
        parts.append(f"最近一处是 {rep['last']['datetime']} 的{rep['last']['kind']}")
    return " · ".join(parts)
