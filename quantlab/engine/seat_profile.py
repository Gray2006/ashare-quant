# -*- coding: utf-8 -*-
"""席位画像:把龙虎榜记录蒸馏成「这个人怎么炒」。

不回答「跟不跟」,回答「他在买什么、为什么买、拿多久、什么脾气」。
输入一个营业部名,输出结构化画像 + 可直接读的报告。

数据来源:
  · hotmoney 库的 lhb_seats(上榜日、股票、买卖净额)
  · kline_cache 的日线(算位置/涨停/连板/放量/振幅 —— 不依赖「上榜原因」文本,
    因为文本是交易所口径,行为才说明问题)
  · 可选联网增强(em.quotes 拿市值/行业,较慢,默认关)

口径修正:买入基准用 **T+1 开盘价**,不用 T 日收盘 —— 龙虎榜是盘后才披露,
T 日收盘根本买不到。用收盘价算收益会把「次日高开」这段白送掉。
"""
from __future__ import annotations

import datetime as dt
import math

from ..data.kline import get_kline

LIMIT = 0.095          # 涨幅 ≥9.5% 当涨停(含 20cm 板会单列)
STYLE_RULES = (
    ("打板型", "涨停日买入占比 ≥ 50%"),
    ("接力型", "连板股买入占比 ≥ 35%"),
    ("追涨型", "当日涨 5% 以上买入占比 ≥ 60%"),
    ("低吸型", "当日下跌时买入占比 ≥ 30%"),
    ("趋势型", "上榜日位置多在 60 日区间上半段"),
)


# ---------------------------------------------------------------- 取数
_bar_cache: dict[str, list[dict]] = {}
_bench_cache: dict[str, float] = {}
_bench_loaded = False


def _bench_file():
    from ..config import cfg
    return cfg().path.parent / "seat_bench.json"


def _load_bench() -> None:
    global _bench_loaded
    if _bench_loaded:
        return
    _bench_loaded = True
    try:
        import json
        p = _bench_file()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            _bench_cache.update({k: float(v) for k, v in data.items()
                                 if v is not None})
    except Exception:  # noqa: BLE001
        pass


def _save_bench() -> None:
    try:
        import json
        _bench_file().write_text(
            json.dumps(_bench_cache, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def market_bench(days: list[str], min_codes: int = 2000) -> dict[str, float]:
    """同期全市场基准(委托给 backtest_core,口径只在那一处定义)。

    没有这个对照,「跟买 -5%」会把行情下跌的锅扣到席位头上。
    样本不足 min_codes 的日期直接给 None(数据还没补齐)。
    """
    from . import backtest_core as bc
    return bc.market_benchmark(days, hold=5, mode="next_open", min_codes=min_codes)


def daily(code: str) -> list[dict]:
    """日线(前复权),带进程内缓存 —— 画像一次会碰几十只票。"""
    if code in _bar_cache:
        return _bar_cache[code]
    try:
        bars, _src = get_kline(code, 101, count=400)
        bars = bars or []
    except Exception:  # noqa: BLE001
        bars = []
    _bar_cache[code] = bars
    return bars


def _dates(bars: list[dict]) -> list[str]:
    return [str(b.get("datetime") or "")[:10] for b in bars]


def _idx(bars: list[dict], day: str) -> int | None:
    ds = _dates(bars)
    try:
        return ds.index(day)
    except ValueError:
        return None


def _pct(bars: list[dict], i: int) -> float | None:
    if i <= 0 or i >= len(bars):
        return None
    prev = float(bars[i - 1].get("close") or 0)
    cur = float(bars[i].get("close") or 0)
    return (cur / prev - 1) if prev > 0 else None


def _range_pos(bars: list[dict], i: int, window: int = 60) -> float | None:
    """当日收盘在近 window 日 [最低, 最高] 里的位置,0=最低 1=最高。"""
    if i < 0 or i >= len(bars):
        return None
    seg = bars[max(0, i - window + 1):i + 1]
    if len(seg) < 20:
        return None
    lo = min(float(b.get("low") or 0) for b in seg)
    hi = max(float(b.get("high") or 0) for b in seg)
    c = float(bars[i].get("close") or 0)
    return (c - lo) / (hi - lo) if hi > lo else None


def _limit_streak(bars: list[dict], i: int) -> int:
    """截至当日的连续涨停天数(含当日)。"""
    n = 0
    j = i
    while j >= 0:
        p = _pct(bars, j)
        if p is not None and p >= LIMIT:
            n += 1
            j -= 1
        else:
            break
    return n


def _vol_ratio(bars: list[dict], i: int, window: int = 20) -> float | None:
    if i < 5:
        return None
    seg = bars[max(0, i - window):i]
    vols = [float(b.get("volume") or 0) for b in seg if float(b.get("volume") or 0) > 0]
    if not vols:
        return None
    base = sum(vols) / len(vols)
    cur = float(bars[i].get("volume") or 0)
    return (cur / base) if base > 0 else None


def _amp(bars: list[dict], i: int) -> float | None:
    if i < 0 or i >= len(bars):
        return None
    prev = float(bars[i - 1].get("close") or 0) if i > 0 else 0
    hi = float(bars[i].get("high") or 0)
    lo = float(bars[i].get("low") or 0)
    if prev <= 0 or hi <= lo:
        return None
    return (hi - lo) / prev


def _entry_exit(bars: list[dict], i: int, hold: int) -> tuple[float | None, float | None, float | None]:
    """T+1 开盘买入、持有 hold 个交易日(按收盘)卖出的收益。"""
    if i + 1 >= len(bars):
        return None, None, None
    entry = float(bars[i + 1].get("open") or 0)
    j = i + 1 + hold
    if entry <= 0 or j >= len(bars):
        return None, entry, None
    exit_px = float(bars[j].get("close") or 0)
    return (exit_px / entry - 1 if exit_px > 0 else None), entry, exit_px


def _median(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _share(flags: list[bool]) -> float:
    return sum(1 for f in flags if f) / len(flags) if flags else 0.0


def _tstat(xs: list[float]) -> float:
    """单序列 t 值(委托 backtest_core)。"""
    from . import backtest_core as bc
    return bc.tstat(xs)


def _industry_of(code: str) -> str:
    """个股所属行业(一级板块)。取不到就算了,画像不能因为联网失败整个挂掉。"""
    try:
        from ..data import sector
        rows = sector.of_stock(code) or []
        for r in rows:
            if int(r.get("level") or 0) == 2 and r.get("name"):
                return str(r["name"])
        return str(rows[0]["name"]) if rows else ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 画像
def _pattern_of(x: dict) -> str:
    """单次出手归类成「打法」。这是画像里唯一能让人看懂他在干什么的东西。

    判据只用当日可观测的特征:涨没涨、几连板、在什么位置、放没放量。
    顺序有讲究 —— 连板 > 首板 > 半路 > 大跌低吸 > 高位/低位,先命中先算。
    """
    lim = int(x.get("limit") or 0)
    pct = x.get("pct")
    pos = x.get("pos")
    vol = x.get("vol") or 1.0
    if lim >= 3:
        return "高位接力(3板+)"
    if lim == 2:
        return "二板接力"
    if lim == 1:
        return "首板打板"
    if pct is not None and pct >= 0.05:
        return "半路追涨"
    if pct is not None and pct <= -0.05:
        return "大跌/跌停低吸"
    if pct is not None and pct < 0:
        return "回调低吸"
    if pos is not None and pos >= 0.85:
        return "高位横盘接货"
    if pos is not None and pos <= 0.30:
        return "低位潜伏"
    if vol >= 2.0:
        return "放量突破"
    return "普通参与"


def _pattern_summary(recs: list[dict]) -> list[dict]:
    """把打法按出现次数排好,并给每类挑一个最近的例子。"""
    buckets: dict[str, list[dict]] = {}
    for x in recs:
        buckets.setdefault(_pattern_of(x), []).append(x)
    out = []
    for name, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda r: r["day"], reverse=True)
        sample = items[0]
        rets = [r["ret5"] for r in items if r["ret5"] is not None]
        out.append({
            "name": name,
            "n": len(items),
            "share": round(len(items) / len(recs), 3) if recs else 0.0,
            "avg5": round(sum(rets) / len(rets), 4) if rets else None,
            "win5": round(sum(1 for r in rets if r > 0) / len(rets), 3) if rets else None,
            "sample": {
                "day": sample["day"], "code": sample["code"],
                "name": sample.get("name", ""), "pct": sample.get("pct"),
                "limit": sample.get("limit") or 0, "pos": sample.get("pos"),
                "vol": sample.get("vol"), "net": sample.get("net"),
                "ret5": sample.get("ret5"),
            },
        })
    return out


def profile(seat: str, days: int = 60, side: str = "buy",
            enrich: bool = True) -> dict:
    """给一个席位做画像。本地数据为主,enrich=True 时补行业分布(联网,慢几秒)。

    side="buy" 只看净买入的记录 —— 选股逻辑要从「他往里砸钱」的动作里读,
    净卖出的记录是他出货,混进来会把特征搅浑(卖出记录单独统计在 outflow)。
    """
    from . import hotmoney as hm

    c = hm._db()
    try:
        rows = c.execute(
            "SELECT day, code, name, buy, sell, net, close FROM lhb_seats "
            "WHERE seat=? AND day >= date('now', ?) ORDER BY day",
            (seat, f"-{int(days)} day")).fetchall()
    finally:
        c.close()
    rows = [dict(r) for r in rows]
    if not rows:
        return {"seat": seat, "n": 0, "ok": False,
                "msg": "这个席位在最近 %d 天没有记录" % days}

    buys = [r for r in rows if float(r["net"] or 0) > 0]
    sells = [r for r in rows if float(r["net"] or 0) <= 0]
    use = buys if (side == "buy" and buys) else rows

    # ---- 逐条算特征
    recs = []
    for r in use:
        bars = daily(r["code"])
        i = _idx(bars, r["day"])
        if i is None:
            continue
        p = _pct(bars, i)
        ret1, entry, _ = _entry_exit(bars, i, 1)
        ret5, _, _ = _entry_exit(bars, i, 5)
        ret10, _, _ = _entry_exit(bars, i, 10)
        recs.append({
            "day": r["day"], "code": r["code"], "name": r["name"],
            "buy": float(r["buy"] or 0), "sell": float(r["sell"] or 0),
            "net": float(r["net"] or 0), "close": float(r["close"] or 0),
            "pct": p, "pos": _range_pos(bars, i),
            "limit": (_limit_streak(bars, i) if p is not None and p >= LIMIT else 0),
            "vol": _vol_ratio(bars, i), "amp": _amp(bars, i),
            "ret1": ret1, "ret5": ret5, "ret10": ret10,
        })
    if not recs:
        return {"seat": seat, "n": len(rows), "ok": False,
                "msg": "有记录但都取不到 K 线,无法算行为特征"}

    net_sum = sum(x["net"] for x in recs)
    buy_sum = sum(x["buy"] for x in recs if x["buy"] > 0)
    sell_sum = sum(x["sell"] for x in recs if x["sell"] > 0)

    # ---- 手法:把每次出手归类,次数最多的就是主打法
    patterns = _pattern_summary(recs)
    main_pattern = patterns[0]["name"] if patterns else ""

    # ---- 风格判定
    limit_share = _share([x["limit"] >= 1 for x in recs])
    streak_share = _share([x["limit"] >= 2 for x in recs])
    up5_share = _share([x["pct"] is not None and x["pct"] >= 0.05 for x in recs])
    down_share = _share([x["pct"] is not None and x["pct"] < 0 for x in recs])
    pos_hi = _share([x["pos"] is not None and x["pos"] >= 0.7 for x in recs])
    pos_lo = _share([x["pos"] is not None and x["pos"] <= 0.35 for x in recs])
    tags = []
    if limit_share >= 0.5:
        tags.append("打板型")
    if streak_share >= 0.35:
        tags.append("接力型")
    # 低吸看「跌的时候敢买」,不看区间位置 —— 上升趋势里的回踩也叫低吸
    if down_share >= 0.4 and limit_share < 0.25:
        tags.append("低吸型(趋势回踩)")
    if up5_share >= 0.6:
        tags.append("追涨型")
    if pos_hi >= 0.7 and down_share < 0.4:
        tags.append("高位博弈型")
    if not tags:
        tags.append("混合型")

    # ---- 协同:同一天同一只票还出现过哪些席位
    c = hm._db()
    try:
        pairs: dict[str, int] = {}
        for x in recs:
            for o in c.execute(
                    "SELECT seat FROM lhb_seats WHERE day=? AND code=? AND seat<>?",
                    (x["day"], x["code"], seat)):
                pairs[o["seat"]] = pairs.get(o["seat"], 0) + 1
    finally:
        c.close()
    partners = sorted(pairs.items(), key=lambda kv: -kv[1])[:6]

    ret1 = _mean([x["ret1"] for x in recs]) or 0.0
    ret5 = _mean([x["ret5"] for x in recs]) or 0.0
    ret10 = _mean([x["ret10"] for x in recs]) or 0.0
    win5 = _share([(x["ret5"] or 0) > 0 for x in recs if x["ret5"] is not None])

    # ---- 同期基准:扣掉行情涨跌,才看得出席位本身值不值
    bench = market_bench(sorted({x["day"] for x in recs}))
    bvals = [bench[x["day"]] for x in recs
             if x["ret5"] is not None and bench.get(x["day"]) is not None]
    bench5 = _mean(bvals)
    excess5 = (ret5 - bench5) if bench5 is not None else None

    # ---- 反向(规避)价值:按日聚合超额再检验,避免同日多股把 t 放大
    per_day: dict[str, list[float]] = {}
    for x in recs:
        b = bench.get(x["day"])
        if x["ret5"] is not None and b is not None:
            per_day.setdefault(x["day"], []).append(x["ret5"] - b)
    day_series = [sum(v) / len(v) for _, v in sorted(per_day.items())]
    excess_t = _tstat(day_series)

    # ---- 行业分布 + 股价档位(公司特征里能本地/轻量拿到的部分)
    industries: dict[str, int] = {}
    tiers = {"<10元": 0, "10-30元": 0, "30-100元": 0, ">100元": 0}
    seen_codes: list[str] = []
    for x in recs[-40:]:
        px = float(x.get("close") or 0)
        if px > 0:
            if px < 10:
                tiers["<10元"] += 1
            elif px < 30:
                tiers["10-30元"] += 1
            elif px < 100:
                tiers["30-100元"] += 1
            else:
                tiers[">100元"] += 1
        if enrich and x["code"] not in seen_codes:
            seen_codes.append(x["code"])
    if enrich:
        for code in seen_codes[:30]:
            name = _industry_of(code)
            if name:
                industries[name] = industries.get(name, 0) + 1

    # ---- 跟买价值:他赚钱不代表跟他买赚钱
    reverse_t = -excess_t
    if len(recs) < 15:
        verdict = "样本太少(%d 笔),别下结论" % len(recs)
    elif excess5 is not None and excess5 > 0.01 and win5 >= 0.52:
        verdict = ("有正超额:T+1 开盘接,5 日 %+.2f%%(同期全市场 %+.2f%%),"
                   "超额 %+.2f%%、胜率 %.0f%%"
                   % (ret5 * 100, bench5 * 100, excess5 * 100, win5 * 100))
    elif reverse_t >= 2.0:
        verdict = ("可靠的反向指标:超额 %+.2f%%/日盘、t=%.2f(按日聚合),"
                   "胜率 %.0f%% —— 跟买是抬轿,但可以用来**排除**他买的票"
                   % ((excess5 or 0) * 100, reverse_t, win5 * 100))
    elif excess5 is not None and excess5 < -0.01 and win5 <= 0.45:
        verdict = ("跟买亏钱:T+1 开盘接,5 日 %+.2f%%(同期全市场 %+.2f%%),"
                   "超额 %+.2f%%、胜率 %.0f%% —— 扣掉行情也是负的;"
                   "但按日聚合 t=%.2f,还没到能当反向指标的程度"
                   % (ret5 * 100, bench5 * 100, excess5 * 100, win5 * 100,
                      reverse_t))
    else:
        verdict = ("没有稳定的跟买价值:5 日 %+.2f%%、胜率 %.0f%%,超额 %s、t=%.2f —— "
                   "方向不稳,拿它当择时信号会亏在手续费和滑点上"
                   % (ret5 * 100, win5 * 100,
                      "-" if excess5 is None else "%+.2f%%" % (excess5 * 100),
                      reverse_t))

    # ---- 情绪维度:连板高度分布 + 同日同板块共振
    streak_dist: dict[str, int] = {"首板": 0, "2板": 0, "3板+": 0, "未涨停": 0}
    for x in recs:
        n_lim = int(x["limit"] or 0)
        if n_lim <= 0:
            streak_dist["未涨停"] += 1
        elif n_lim == 1:
            streak_dist["首板"] += 1
        elif n_lim == 2:
            streak_dist["2板"] += 1
        else:
            streak_dist["3板+"] += 1

    # 同一天、同一行业里还有几只票上榜 —— 板块共振强度(样本限最近 25 条,控联网)
    co_heat: list[int] = []
    if industries:
        by_day: dict[str, list[str]] = {}
        for x in recs:
            by_day.setdefault(x["day"], []).append(x["code"])
        ind_of = {x["code"]: _industry_of(x["code"]) for x in recs[-25:]}
        for x in recs[-25:]:
            ind = ind_of.get(x["code"])
            if not ind:
                continue
            same = sum(1 for c in by_day.get(x["day"], [])
                       if c != x["code"] and ind_of.get(c) == ind)
            co_heat.append(same)

    return {
        "seat": seat, "ok": True, "days": days, "side": side,
        "n": len(recs), "codes": len({x["code"] for x in recs}),
        "buys": len(buys), "sells": len(sells),
        "outflow": round(sum(float(r["net"] or 0) for r in sells), 0),
        "net_sum": round(net_sum, 0), "buy_sum": round(buy_sum, 0),
        "sell_sum": round(sell_sum, 0),
        "avg_net": round(net_sum / len(recs), 0),
        "buy_sell_ratio": round(buy_sum / sell_sum, 2) if sell_sum > 0 else None,
        "tags": tags, "verdict": verdict,
        "patterns": patterns, "main_pattern": main_pattern,
        "limit_share": round(limit_share, 3),
        "streak_share": round(streak_share, 3),
        "up5_share": round(up5_share, 3),
        "down_share": round(down_share, 3),
        "pos_median": round(_median([x["pos"] for x in recs]) or 0, 3),
        "vol_median": round(_median([x["vol"] for x in recs]) or 0, 2),
        "amp_median": round(_median([x["amp"] for x in recs]) or 0, 4),
        "ret1": round(ret1, 4), "ret5": round(ret5, 4), "ret10": round(ret10, 4),
        "win5": round(win5, 3),
        "bench5": None if bench5 is None else round(bench5, 4),
        "excess5": None if excess5 is None else round(excess5, 4),
        "excess_t": round(excess_t, 2),
        "reverse_t": round(reverse_t, 2),
        "n_days": len(day_series),
        "industries": sorted(industries.items(), key=lambda kv: -kv[1])[:8],
        "tiers": tiers,
        "streak_dist": streak_dist,
        "co_heat": (_median(co_heat) or 0) if co_heat else None,
        "partners": [{"seat": s, "n": n} for s, n in partners],
        "samples": recs[-12:],
    }


# ---------------------------------------------------------------- 报告
def render_markdown(p: dict) -> str:
    if not p.get("ok"):
        return f"# 席位画像\n\n{p.get('seat', '')}\n\n{p.get('msg', '没有数据')}\n"

    def pct(x: float | None) -> str:
        return "-" if x is None else f"{x:+.2%}"

    lines = [
        f"# 席位画像 · {p['seat']}", "",
        f"近 {p['days']} 天:{p['n']} 次净买入上榜、{p['codes']} 只票 · "
        f"净买入 {p['net_sum'] / 1e8:+.2f} 亿 · 单次均值 {p['avg_net'] / 1e4:+.0f} 万",
        f"(另有 {p['sells']} 次净卖出,合计 {p['outflow'] / 1e8:+.2f} 亿)",
        "",
        f"**风格:{' / '.join(p['tags'])}**", "",
        f"**结论:{p['verdict']}**", "",
    ]

    # 手法:比他买在什么位置更直观 —— 每次出手归成哪一类、各占多少、效果如何
    pats = p.get("patterns") or []
    if pats:
        lines += ["## 它的手法", "",
                  f"主打法:**{pats[0]['name']}**({pats[0]['share']:.0%} 的出手)", "",
                  "| 打法 | 次数 | 占比 | 5日均收益 | 胜率 | 最近一次 |",
                  "|---|---|---|---|---|---|"]
        for it in pats[:5]:
            sm = it["sample"]
            lines.append(
                f"| {it['name']} | {it['n']} | {it['share']:.0%} | "
                f"{'-' if it['avg5'] is None else '%+.2f%%' % (it['avg5'] * 100)} | "
                f"{'-' if it['win5'] is None else '%.0f%%' % (it['win5'] * 100)} | "
                f"{sm['day']} {sm['code']} {sm.get('name', '')[:6]} |")
        lines.append("")

    lines += [
        "## 买入时点长什么样", "",
        "| 特征 | 数值 | 读法 |", "|---|---|---|",
        f"| 涨停日买入占比 | {p['limit_share']:.0%} | 越高越是打板选手 |",
        f"| 连板股买入占比 | {p['streak_share']:.0%} | 高 = 做接力,低 = 只做首板 |",
        f"| 当日涨 5% 以上买入 | {p['up5_share']:.0%} | 追涨强度 |",
        f"| 当日下跌时买入 | {p['down_share']:.0%} | 高 = 低吸/接飞刀 |",
        f"| 60 日区间位置(中位) | {p['pos_median']:.0%} | 70%+ 说明只买高位 |",
        f"| 放量倍数(中位) | {p['vol_median']:.1f}x | 相对前 20 日均量 |",
        f"| 当日振幅(中位) | {p['amp_median']:.1%} | 波动偏好 |",
        "",
        "## 买完之后", "",
        "买入基准是 **T+1 开盘价**(龙虎榜盘后才披露,T 日收盘买不到)。", "",
        "| 持有 | 平均收益 |", "|---|---|",
        f"| 1 日 | {pct(p['ret1'])} |",
        f"| 5 日 | {pct(p['ret5'])} |",
        f"| 10 日 | {pct(p['ret10'])} |",
        "",
        f"5 日胜率 {p['win5']:.0%} · 买/卖额比 "
        f"{p['buy_sell_ratio'] if p['buy_sell_ratio'] is not None else '-'}",
        "",
        "同期全市场同样「T+1 开盘买、持有 5 日」的基准 "
        f"{'-' if p['bench5'] is None else '%+.2f%%' % (p['bench5'] * 100)},"
        f"**超额 {'-' if p['excess5'] is None else '%+.2f%%' % (p['excess5'] * 100)}**;"
        f"按日聚合 t={p['excess_t']:+.2f}({p['n_days']} 个交易日),"
        f"反向 t={p['reverse_t']:+.2f}。",
        "",
    ]

    if p.get("industries") or p.get("tiers"):
        lines += ["## 买的是什么样的公司", ""]
        if p["industries"]:
            lines.append("行业分布(抽样):" + "、".join(
                f"{k} {v}" for k, v in p["industries"]))
            lines.append("")
        t = p.get("tiers") or {}
        if any(t.values()):
            lines.append("股价档位:" + "、".join(
                f"{k} {v}" for k, v in t.items() if v))
            lines.append("")

    sd = p.get("streak_dist") or {}
    if any(sd.values()):
        lines += ["## 情绪特征", ""]
        tot = sum(sd.values()) or 1
        lines.append("板数分布:" + "、".join(
            f"{k} {v}({v / tot:.0%})" for k, v in sd.items() if v))
        if p.get("co_heat") is not None:
            lines.append("")
            lines.append(f"同日同行业上榜只数(中位):{p['co_heat']:.0f} 只 —— "
                         + ("板块共振明显,不是单打独斗" if p["co_heat"] >= 2
                            else "基本是孤立个股,没有板块合力"))
        lines.append("")
    if p["partners"]:
        lines += ["## 常跟谁一起出现", "",
                  "| 席位 | 同现次数 |", "|---|---|"]
        lines += [f"| {x['seat'][:34]} | {x['n']} |" for x in p["partners"]]
        lines.append("")

    lines += ["## 最近几次出手", "",
              "| 日期 | 代码 | 名称 | 当日涨幅 | 板数 | 区间位置 | 净买入(万) | 5日(T+1开盘) |",
              "|---|---|---|---|---|---|---|---|"]

    def f(x, spec, d="-"):
        return d if x is None else format(x, spec)

    for x in p["samples"]:
        # 代码做成锚点:弹窗里点一下就能跳到行情页(statusbar 之外的唯一交互入口)
        lines.append(
            f"| {x['day']} | [{x['code']}](code:{x['code']}) | {x['name']} | "
            f"{f(x['pct'], '+.2%')} | "
            f"{x['limit'] or '-'} | {f(x['pos'], '.0%')} | {x['net'] / 1e4:+.0f} | "
            f"{f(x['ret5'], '+.2%')} |")
    lines.append("")
    return "\n".join(lines)


def save_report(seat: str, outdir=None, stem: str | None = None) -> tuple[str, str]:
    """算画像并存 md/json,返回路径。"""
    import json
    from pathlib import Path

    p = profile(seat)
    d = Path(outdir) if outdir else Path("reports")
    d.mkdir(parents=True, exist_ok=True)
    stem = stem or f"seat-{dt.date.today().isoformat()}"
    md = d / f"{stem}.md"
    js = d / f"{stem}.json"
    md.write_text(render_markdown(p), encoding="utf-8")
    js.write_text(json.dumps(p, ensure_ascii=False, indent=2, default=str),
                  encoding="utf-8")
    return str(md), str(js)
