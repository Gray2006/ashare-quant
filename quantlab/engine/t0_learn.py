# -*- coding: utf-8 -*-
"""做T参数学习:从这只票自己的历史里,学出它适合的触发/回补幅度。

为什么不是手填:1% 的触发对茅台和一只 5 块钱的票完全不是一个概念。
固定参数要么在低波动票上永不触发,要么在高波动票上天天被扫。

学的是三件事:
  1. 可达性 —— 历史上「开盘后能涨到 X%」和「能跌到 -X%」的天数占比
  2. 回补空间 —— 触发之后还有多少回落/反弹空间(take_pct 的上限)
  3. 样本外 —— 前 70% 天选参数,后 30% 天验证。样本外不成立就不该用

数据源优先级:5 分钟线(真实路径) > 日线近似(有路径假设,会偏乐观)。
日线近似只做粗筛,结论前面会标出来。
"""
from __future__ import annotations

import itertools

from .t0 import T0Config, T0Engine, daily_pseudo_bars, min5_days, daily_days, summarize

# 网格:触发幅度 × 回补幅度。粗一点,避免在几百个点上过拟合
MIN_GRID = (0.005, 0.0075, 0.010, 0.0125, 0.015, 0.020)
TAKE_GRID = (0.003, 0.005, 0.008, 0.010)
TRAIN_FRAC = 0.7


def _days_of(code: str, prefer_min5: bool = True) -> tuple[list[dict], str]:
    """取历史,优先 5 分钟线。返回 (days, 数据源标签)。"""
    if prefer_min5:
        try:
            days = min5_days(code)
            if len(days) >= 15:
                return days, "min5"
        except Exception:  # noqa: BLE001
            pass
    try:
        days = daily_days(code, "2023-01-01")
    except Exception:  # noqa: BLE001
        days = []
    return days, "daily"


def _reach_stats(days: list[dict]) -> dict:
    """可达性统计:开盘后能走到多高/多低。用日线 OHLC,不做路径假设。"""
    up, down, amp = [], [], []
    for d in days:
        bars = d.get("bars") or daily_pseudo_bars(d)
        if not bars:
            continue
        o = float(bars[0].get("open") or 0)
        pc = float(d.get("pre_close") or 0) or o
        if o <= 0:
            continue
        hi = max(float(b.get("high") or 0) for b in bars)
        lo = min(float(b.get("low") or 0) for b in bars if float(b.get("low") or 0) > 0)
        if hi > 0:
            up.append(hi / o - 1)
        if lo > 0:
            down.append((o - lo) / o)
        amp.append((hi - lo) / pc if pc > 0 else 0.0)

    def pct(xs, q):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return 0.0
        i = min(len(xs) - 1, max(0, int(len(xs) * q)))
        return xs[i]

    return {
        "n": len(amp),
        "amp_p50": round(pct(amp, 0.5), 4),
        "amp_p80": round(pct(amp, 0.8), 4),
        "up_p50": round(pct(up, 0.5), 4),
        "up_p80": round(pct(up, 0.8), 4),
        "down_p50": round(pct(down, 0.5), 4),
        "down_p80": round(pct(down, 0.8), 4),
    }


def _run(days: list[dict], mode: str, mp: float, tp: float,
         base_shares: int = 1000) -> dict:
    cfg = T0Config(base_shares=base_shares, ratio=0.30, mode=mode,
                   min_pct=mp, take_pct=tp, fill_mode="through",
                   better_fill=True)
    recs = T0Engine("600000", cfg).run(days)
    bv = base_shares * float(days[0].get("close") or 1) if days else 1.0
    s = summarize(recs, bv, trading_days=len(days))
    return {"net": s["net"], "net_pct": s["net_pct"], "traded": s["traded_days"],
            "days": s["days"], "win": s["win_rate"], "recs": recs}


def learn(code: str, mode: str = "reverse", prefer_min5: bool = True,
          base_shares: int = 1000) -> dict:
    """学出这只票适合的做T参数。

    两种数据源,给的东西不一样,不能混:
      · min5  真实路径 → 网格搜索 + 样本外验证,结论可用
      · daily 有路径假设(pseudo bar 保证先高后低)→ **不算收益**,
              只按「可达性」给建议参数。日线算出来的收益是假设的产物,
              之前用日线跑出过 +200%,纯属自欺。
    """
    days, src = _days_of(code, prefer_min5)
    if len(days) < 30 and src == "min5":
        # 5 分钟线还没攒够(现在约 21 天),退回日线做可达性估计
        days, src = _days_of(code, prefer_min5=False)
    if len(days) < 30:
        return {"ok": False, "code": code, "src": src,
                "msg": f"历史只有 {len(days)} 天,不够学参数(至少 30 天)"}

    split = int(len(days) * TRAIN_FRAC)
    train, test = days[:split], days[split:]
    reach = _reach_stats(train)

    # ---- 日线:只给可达性建议,不给收益结论
    if src == "daily":
        up = reach["up_p50"] or 0.01
        down = reach["down_p50"] or 0.01
        mp = round(min(max(0.5 * up, 0.005), 0.03), 4)
        tp = round(min(max(0.5 * down, 0.003), mp * 0.8), 4)
        return {
            "ok": True, "code": code, "src": src, "mode": mode,
            "days": len(days), "train": len(train), "test": len(test),
            "suggest": {"min_pct": mp, "take_pct": tp,
                        "train_net_pct": None, "train_traded": None,
                        "train_win": None},
            "out_sample": None, "baseline": None,
            "reach": reach, "top_grid": [],
            "verdict": (f"按可达性给建议:触发 {mp:.2%}(中位涨幅 {up:.2%} 的一半)、"
                        f"回补 {tp:.2%}。日线有路径假设,**没算收益**,"
                        f"要真结论得有 5 分钟线"),
            "caveat": "日线近似不算收益,只做可达性估计",
        }

    # ---- 5 分钟线:真实路径,可以网格 + 样本外
    if len(test) < 5:
        return {"ok": False, "code": code, "src": src,
                "msg": "样本外天数太少,分不开训练/验证"}

    best = None
    grid = []
    for mp, tp in itertools.product(MIN_GRID, TAKE_GRID):
        if tp >= mp:           # 回补不能大于触发,否则逻辑倒挂
            continue
        r = _run(train, mode, mp, tp, base_shares)
        grid.append({"min_pct": mp, "take_pct": tp,
                     "net_pct": round(r["net_pct"], 4),
                     "traded": r["traded"], "win": r["win"]})
        if best is None or r["net_pct"] > best["net_pct"]:
            best = {"min_pct": mp, "take_pct": tp, "net_pct": r["net_pct"],
                    "traded": r["traded"], "win": r["win"]}

    out = _run(test, mode, best["min_pct"], best["take_pct"], base_shares)
    base = _run(test, mode, 0.010, 0.005, base_shares)   # 默认参数作对照

    ok = out["net_pct"] > 0 and out["traded"] >= 3
    if not ok:
        verdict = (f"样本外不成立:训练段 {best['net_pct']:+.2%} → "
                   f"样本外 {out['net_pct']:+.2%},这只票不适合做T")
    elif out["net_pct"] > base["net_pct"]:
        verdict = (f"学到参数胜过默认:样本外 {out['net_pct']:+.2%} "
                   f"(默认 1%/0.5% 是 {base['net_pct']:+.2%})")
    else:
        verdict = (f"学到参数没跑赢默认:样本外 {out['net_pct']:+.2%} "
                   f"(默认 {base['net_pct']:+.2%}),别用")

    grid.sort(key=lambda g: -g["net_pct"])
    return {
        "ok": True, "code": code, "src": src, "mode": mode,
        "days": len(days), "train": len(train), "test": len(test),
        "suggest": {"min_pct": best["min_pct"], "take_pct": best["take_pct"],
                    "train_net_pct": round(best["net_pct"], 4),
                    "train_traded": best["traded"], "train_win": best["win"]},
        "out_sample": {"net_pct": round(out["net_pct"], 4),
                       "traded": out["traded"], "win": out["win"]},
        "baseline": {"min_pct": 0.010, "take_pct": 0.005,
                     "net_pct": round(base["net_pct"], 4),
                     "traded": base["traded"]},
        "reach": reach, "top_grid": grid[:8], "verdict": verdict,
        "caveat": "5 分钟真实路径,但样本只有约 20 个交易日",
    }


def render_markdown(p: dict) -> str:
    if not p.get("ok"):
        return f"# 做T参数学习 · {p.get('code', '')}\n\n{p.get('msg', '')}\n"
    s, r = p["suggest"], p["reach"]
    o, b = p.get("out_sample"), p.get("baseline")
    lines = [
        f"# 做T参数学习 · {p['code']}", "",
        f"数据:{p['src']} · {p['days']} 天(训练 {p['train']} / 验证 {p['test']}) · "
        f"方向 {'反T' if p['mode'] == 'reverse' else '正T'}", "",
        f"**{p['verdict']}**", "",
        "## 建议参数", "",
        "| 项 | 值 |", "|---|---|",
        f"| 触发 | {s['min_pct']:.2%} |",
        f"| 回补 | {s['take_pct']:.2%} |",
    ]
    if s.get("train_net_pct") is not None:
        lines.append(f"| 训练段净收益 | {s['train_net_pct']:+.2%}"
                     f"(成交 {s['train_traded']} 天) |")
    if o:
        lines.append(f"| 样本外净收益 | {o['net_pct']:+.2%}(成交 {o['traded']} 天) |")
    if b:
        lines.append(f"| 默认参数对照 | {b['net_pct']:+.2%} |")
    lines += [
        "",
        "## 这只票的日内脾气(训练段)", "",
        "| 指标 | 中位 | 80 分位 |", "|---|---|---|",
        f"| 日内振幅 | {r['amp_p50']:.2%} | {r['amp_p80']:.2%} |",
        f"| 开盘后最高涨幅 | {r['up_p50']:.2%} | {r['up_p80']:.2%} |",
        f"| 开盘后最低跌幅 | {r['down_p50']:.2%} | {r['down_p80']:.2%} |",
        "",
        "触发幅度超过「80 分位涨幅」,大部分日子根本到不了;"
        "回补幅度超过「80 分位跌幅」,卖出去就买不回来。",
        "",
        f"注:{p['caveat']}", "",
    ]
    return "\n".join(lines)
