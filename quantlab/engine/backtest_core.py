# -*- coding: utf-8 -*-
"""回测口径的唯一出处。

以前每个回测模块自己算:做T一套成本、因子回测一套、席位画像又一套。
结果就是 2026-11-24 那次踩的坑 —— 席位画像拿「T 日收盘」当买入价,
而龙虎榜是盘后才披露的,T 日收盘根本买不到,凭空多出几个点的虚假收益。

这里把四件事定死,新写回测只许调用,不许自己算:
  1. 买入价口径(buy_price)
  2. 持有收益(holding_return)
  3. 同期基准(market_benchmark)
  4. 判定(tstat / judge)

历史模块暂时不强制迁移,但新代码必须走这里。
"""
from __future__ import annotations

import math

# 双边成本:佣金万 2.5 ×2 + 印花税万 5 + 滑点万 5 ×2 ≈ 0.16%。
# 与 factor_analysis.COST_ROUND_TRIP 同值,改的时候两边一起改。
COST_ROUND_TRIP = 0.0016

# 买入价口径:
#   next_open  —— T+1 开盘价。事件驱动的唯一正确选择(龙虎榜/公告/财报都是盘后才有)
#   close      —— T 日收盘价。只做对照,实盘买不到
#   next_vwap  —— T+1 均价。分钟线不够时用不了,留接口
BUY_MODES = ("next_open", "close", "next_vwap")


def buy_price(bars: list[dict], i: int, mode: str = "next_open") -> float | None:
    """第 i 根之后的买入价。i 是事件发生的那一天。"""
    if i < 0 or not bars:
        return None
    if mode == "close":
        if i >= len(bars):
            return None
        px = float(bars[i].get("close") or 0)
        return px if px > 0 else None
    j = i + 1
    if j >= len(bars):
        return None
    bar = bars[j]
    if mode == "next_vwap":
        amt = float(bar.get("amount") or 0)
        vol = float(bar.get("volume") or 0)
        if amt > 0 and vol > 0:
            return amt / vol
        return None
    px = float(bar.get("open") or 0)
    return px if px > 0 else None


def exit_price(bars: list[dict], i: int, hold: int, mode: str = "next_open") -> float | None:
    """买入后持有 hold 个交易日的卖出价(收盘)。"""
    j = i + (1 if mode != "close" else 0) + hold
    if j >= len(bars):
        return None
    px = float(bars[j].get("close") or 0)
    return px if px > 0 else None


def holding_return(bars: list[dict], i: int, hold: int,
                   mode: str = "next_open") -> float | None:
    """事件日 i 买入、持有 hold 日的收益率(未扣成本)。"""
    entry = buy_price(bars, i, mode)
    exit_px = exit_price(bars, i, hold, mode)
    if not entry or not exit_px:
        return None
    return exit_px / entry - 1


def net_return(ret: float | None, cost: float = COST_ROUND_TRIP) -> float | None:
    return None if ret is None else ret - cost


def tstat(xs: list[float]) -> float:
    """单序列 t 值(对 0 检验)。"""
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    return (m / (sd / math.sqrt(n))) if sd > 0 else 0.0


def mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- 同期基准
_bench_cache: dict[tuple[str, int], float] = {}
_bench_loaded = False


def _bench_file():
    from ..config import cfg
    return cfg().path.parent / "bench_cache.json"


def _load_bench() -> None:
    global _bench_loaded
    if _bench_loaded:
        return
    _bench_loaded = True
    try:
        import json
        p = _bench_file()
        if p.exists():
            for k, v in json.loads(p.read_text(encoding="utf-8")).items():
                day, hold = k.split("|")
                _bench_cache[(day, int(hold))] = float(v)
    except Exception:  # noqa: BLE001
        pass


def _save_bench() -> None:
    try:
        import json
        _bench_file().write_text(
            json.dumps({f"{d}|{h}": v for (d, h), v in _bench_cache.items()},
                       ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def market_benchmark(days: list[str], hold: int = 5, mode: str = "next_open",
                     min_codes: int = 2000) -> dict[str, float | None]:
    """同期全市场同口径平均收益。

    没有这个对照,任何「跟买 -5%」都会把行情下跌的锅扣到策略头上。
    样本不足 min_codes 的日期给 None(数据还没补齐)。
    """
    _load_bench()
    todo = [d for d in dict.fromkeys(days) if (d, hold) not in _bench_cache]
    if todo:
        from ..data.store import store
        conn = store().conn
        last = max(todo)
        added = False
        for day in todo:
            rows = conn.execute(
                "select code, bar_time, open, close from kline_cache "
                "where klt=101 and fqt=1 and bar_time>=? and bar_time<=? "
                "order by code, bar_time", (day, last)).fetchall()
            per: dict[str, list] = {}
            for r in rows:
                per.setdefault(r["code"], []).append(r)
            rets = []
            for _code, g in per.items():
                if len(g) < hold + 2 or str(g[0]["bar_time"])[:10] != day:
                    continue
                entry = float(g[1]["open"] or 0)
                exit_px = float(g[min(hold + 1, len(g) - 1)]["close"] or 0)
                if entry > 0 and exit_px > 0:
                    rets.append(exit_px / entry - 1)
            if len(rets) >= min_codes:
                _bench_cache[(day, hold)] = sum(rets) / len(rets)
                added = True
        if added:
            _save_bench()
    return {d: _bench_cache.get((d, hold)) for d in days}


# ---------------------------------------------------------------- 判定
def judge(series: list[float], bench: float | None = None,
          cost: float = 0.0, min_n: int = 15) -> dict:
    """统一判定:毛 t、扣费净额、超额 t。

    series 必须是**按日聚合**的序列(每天一个数),不是「每股票·交易日」——
    同一天几十只票一起涨跌是相关的,当独立样本来算会把 t 放大 sqrt(只数) 倍。
    """
    xs = [x for x in series if x is not None]
    n = len(xs)
    if n == 0:
        return {"ok": False, "n": 0, "msg": "没有样本"}
    m = mean(xs) or 0.0
    t = tstat(xs)
    excess = (m - bench) if bench is not None else None
    net = m - cost
    if n < min_n:
        verdict = f"样本太少({n} 天 < {min_n}),别下结论"
    elif excess is not None and t >= 2.0:
        verdict = f"显著正:t={t:.2f},日均 {m:+.3%},超额 {excess:+.3%}"
    elif excess is not None and t <= -2.0:
        verdict = f"显著负:t={t:.2f},日均 {m:+.3%},超额 {excess:+.3%}"
    else:
        verdict = f"不显著:t={t:.2f},日均 {m:+.3%}"
    return {"ok": True, "n": n, "mean": round(m, 5), "t": round(t, 2),
            "bench": None if bench is None else round(bench, 5),
            "excess": None if excess is None else round(excess, 5),
            "net": round(net, 5), "verdict": verdict}
