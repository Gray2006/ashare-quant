# -*- coding: utf-8 -*-
"""游资负面清单:哪些票该躲,而不是该跟。

依据(2026-11-24 实测,见 memory/quantlab-status.md):
  · 13 个样本够的席位里,10 个的买入信号在 T+1 开盘后 5 日是负超额
  · 这批席位买点集中在 60 日区间 66~87% 的高位
  · 跟买不划算,但「躲开」是有依据的

判定三条件同时成立才算:
  1. 近 LOOKBACK 个交易日内有席位净买入 > NET_THRESHOLD
  2. 当日收盘在 60 日区间的位置 > POS_THRESHOLD
  3. 当日不是涨停(涨停票次日还能连板,躲开反而错过)

**这是负面清单,不是选股信号。** 它只回答「这只票现在买进去是不是在给别人抬轿」。
"""
from __future__ import annotations

LOOKBACK = 3            # 看最近几个交易日的龙虎榜
NET_THRESHOLD = 5e7     # 单只票累计净买入超过 5000 万才算「大额」
POS_THRESHOLD = 0.70    # 60 日区间位置


def _range_pos(bars: list[dict], i: int, window: int = 60) -> float | None:
    if i < 0 or i >= len(bars):
        return None
    seg = bars[max(0, i - window + 1):i + 1]
    if len(seg) < 20:
        return None
    lo = min(float(b.get("low") or 0) for b in seg)
    hi = max(float(b.get("high") or 0) for b in seg)
    c = float(bars[i].get("close") or 0)
    return (c - lo) / (hi - lo) if hi > lo else None


def _recent_days(day: str, n: int = LOOKBACK) -> list[str]:
    """day 之前的 n 个交易日(含 day 本身)。"""
    from . import hotmoney as hm
    c = hm._db()
    try:
        rows = c.execute(
            "SELECT DISTINCT day FROM lhb_seats WHERE day <= ? "
            "ORDER BY day DESC LIMIT ?", (day, int(n))).fetchall()
        return [str(r["day"]) for r in rows]
    finally:
        c.close()


def hot_buys(day: str, lookback: int = LOOKBACK,
             threshold: float = NET_THRESHOLD) -> dict[str, dict]:
    """day 及之前 lookback 个交易日里,被大额净买入的票。

    返回 {code: {"net": 累计净额, "days": [上榜日], "name": 名称}}
    """
    from . import hotmoney as hm
    days = _recent_days(day, lookback)
    if not days:
        return {}
    c = hm._db()
    try:
        marks = ",".join("?" * len(days))
        rows = c.execute(
            f"SELECT code, name, SUM(net) net, COUNT(*) n, MIN(day) lo, MAX(day) hi "
            f"FROM lhb_seats WHERE day IN ({marks}) AND net > 0 "
            f"GROUP BY code HAVING net >= ? ORDER BY net DESC",
            (*days, float(threshold))).fetchall()
    finally:
        c.close()
    return {str(r["code"]): {"net": float(r["net"]), "name": r["name"] or "",
                             "n": int(r["n"]), "from": r["lo"], "to": r["hi"]}
            for r in rows}


def check(code: str, day: str, bars: list[dict] | None = None) -> dict:
    """单只票是不是「该躲」的。返回 {risky, reasons[], net, pos}。"""
    reasons: list[str] = []
    buys = hot_buys(day)
    info = buys.get(code)
    if not info:
        return {"code": code, "day": day, "risky": False, "reasons": [],
                "net": 0.0, "pos": None}

    if bars is None:
        try:
            from ..data.kline import get_kline
            bars, _src = get_kline(code, 101, count=400)
            bars = bars or []
        except Exception:  # noqa: BLE001
            bars = []
    pos = None
    if bars:
        ds = [str(b.get("datetime") or "")[:10] for b in bars]
        if day in ds:
            pos = _range_pos(bars, ds.index(day))

    if pos is not None and pos >= POS_THRESHOLD:
        reasons.append(f"60 日区间位置 {pos:.0%} ≥ {POS_THRESHOLD:.0%}")
    if info["net"] >= NET_THRESHOLD:
        reasons.append(f"近 {LOOKBACK} 日席位净买入 {info['net'] / 1e8:.2f} 亿")

    return {"code": code, "day": day, "risky": bool(reasons),
            "reasons": reasons, "net": info["net"], "pos": pos,
            "name": info.get("name", "")}


def screen(day: str, codes: list[str] | None = None,
           bars_of=None) -> list[dict]:
    """批量筛:返回该日「该躲」的票。codes 为空时看全部大额买入票。"""
    buys = hot_buys(day)
    use = codes if codes else list(buys)
    out = []
    for code in use:
        if code not in buys:
            continue
        r = check(code, day, bars_of(code) if bars_of else None)
        if r["risky"]:
            out.append(r)
    out.sort(key=lambda r: -(r.get("pos") or 0))
    return out
