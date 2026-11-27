# -*- coding: utf-8 -*-
"""模拟盘与实盘互相反哺。

问题:现在归因把所有账户混在一起算(account=None),模拟盘的试错样本
和实盘的真实样本权重一样。但两者性质完全不同:

  模拟盘:样本多、可以大量试错、成交假设理想化
  实盘:  样本少、真金白银、有滑点和排队

混在一起的后果:
  - 模拟盘的大量样本会淹没实盘的少量信号
  - 实盘表现和模拟盘背离时看不出来(被平均掉了)

反哺规则:
  1 模拟盘负责「发现」—— 哪个策略/参数在模拟里表现好,列为候选
  2 实盘负责「确认」—— 实盘样本够了才升级结论
  3 背离报警 —— 同一策略模拟赚实盘亏,说明模拟假设失效

账户类型用 config 的 broker.account_kinds 标记:
  {"默认账户": "sim", "量化": "live"}
没标的默认当 sim(安全:不会因为没标就当成实盘证据)。
"""
from __future__ import annotations

import time
from collections import defaultdict

# 实盘样本少于这个数,不给「确认」结论
LIVE_MIN_N = 20
# 模拟盘样本少于这个数,不给「候选」结论
SIM_MIN_N = 30
# 背离阈值:模拟净均值和实盘净均值差这么多就报警
DIVERGE_GAP = 0.02
# 实盘账户少于这么多条记录,当成联调残留过滤掉
NOISE_MIN_N = 3


def _cfg():
    from ..config import cfg
    return cfg()


def account_kinds() -> dict[str, str]:
    """账户 → sim/live。

    判定依据(从严):
      1 config 的 broker.account_kinds(人工指定,最可信)
      2 sim_accounts 表里有 → sim(本地模拟账户)
      3 orders 表有成交记录、且不在模拟账户表里 → live(外部账户)
      4 其余(只有 decisions 没有成交) → sim

    第 3 条才是真实盘证据 —— 只有 sl_decisions 没有 orders 的,
    是测试批次或回填数据,不能当实盘样本。
    「量化」账户就是这种情况:11-13 一天 134 条 decision,orders 0 条。
    """
    explicit: dict[str, str] = {}
    try:
        raw = _cfg().get("broker.account_kinds", {}) or {}
        explicit = {str(k): str(v).lower() for k, v in raw.items()
                    if str(v).lower() in ("sim", "live")}
    except Exception:  # noqa: BLE001
        pass

    sim_names: set[str] = set()
    order_names: set[str] = set()
    try:
        from ..data.store import store
        conn = store().conn
        for r in conn.execute("select name from sim_accounts").fetchall():
            sim_names.add(str(r["name"]))
        for r in conn.execute(
                "select distinct account from orders").fetchall():
            order_names.add(str(r["account"]))
    except Exception:  # noqa: BLE001
        pass

    out = dict(explicit)
    for name in sim_names:
        out.setdefault(name, "sim")
    for name in order_names:
        if name in sim_names:
            continue
        out.setdefault(name, "live")
    return out


def kind_of(account: str, kinds: dict[str, str] | None = None) -> str:
    k = (kinds if kinds is not None else account_kinds()).get(str(account))
    if k in ("sim", "live"):
        return k
    # 没配、不在模拟账户表、也没有成交记录 —— 按模拟盘处理。
    # 只有 sl_decisions 没有 orders 的是测试批次或回填数据,
    # 不能因为「没标」就当成实盘证据。
    return "sim"


def _fetch(days: int) -> list[dict]:
    from . import strategy_review as sr
    return sr._fetch(None, days)


def build(days: int = 90) -> dict:
    """按 (账户类型, 策略) 分组统计,给出反哺建议。"""
    from . import strategy_review as sr

    rows = _fetch(days)
    kinds = account_kinds()

    # 噪音账户过滤:实盘账户里只有 1~2 条记录的,是联调残留(okx-demo / default),
    # 混进统计会把「实盘样本 0」变成「实盘样本 2」,结论整个错位。
    acct_count: dict[str, int] = defaultdict(int)
    for r in rows:
        acct_count[str(r.get("account") or "")] += 1
    noisy = {a for a, n in acct_count.items()
             if kind_of(a, kinds) == "live" and n < NOISE_MIN_N}

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        acct = str(r.get("account") or "")
        if acct in noisy:
            continue
        strat = str(r.get("strategy") or "未命名")
        groups[(kind_of(acct, kinds), strat)].append(r)

    by_kind: dict[str, dict[str, dict]] = {"sim": {}, "live": {}}
    for (kind, strat), items in groups.items():
        by_kind[kind][strat] = sr.summarize(items)

    advice: list[dict] = []
    strategies = set(by_kind["sim"]) | set(by_kind["live"])
    for strat in sorted(strategies):
        sim = by_kind["sim"].get(strat) or {}
        live = by_kind["live"].get(strat) or {}
        sn = int(sim.get("n") or 0)
        ln = int(live.get("n") or 0)
        sm = sim.get("net_mean")
        lm = live.get("net_mean")

        if sn >= SIM_MIN_N and ln < LIVE_MIN_N:
            if (sm or 0) > 0:
                advice.append({
                    "level": "candidate", "strategy": strat,
                    "why": (f"模拟盘 {sn} 笔净 {sm:+.2%}/笔,实盘样本只有 "
                            f"{ln} 笔 —— 可列候选,等实盘小仓位验证"),
                })
            else:
                advice.append({
                    "level": "sim_negative", "strategy": strat,
                    "why": f"模拟盘 {sn} 笔净 {sm:+.2%}/笔,模拟里就不行,别上实盘",
                })
            continue

        if ln >= LIVE_MIN_N:
            if sm is not None and lm is not None:
                gap = float(lm) - float(sm)
                if abs(gap) >= DIVERGE_GAP:
                    advice.append({
                        "level": "diverge", "strategy": strat,
                        "why": (f"模拟 {sm:+.2%} vs 实盘 {lm:+.2%},"
                                f"差 {gap:+.2%} —— 模拟假设可能失效"),
                    })
                    continue
            if (lm or 0) > 0:
                advice.append({
                    "level": "confirmed", "strategy": strat,
                    "why": f"实盘 {ln} 笔净 {lm:+.2%}/笔,模拟实盘都为正",
                })
            else:
                advice.append({
                    "level": "live_negative", "strategy": strat,
                    "why": f"实盘 {ln} 笔净 {lm:+.2%}/笔,实盘为负",
                })
            continue

        advice.append({
            "level": "insufficient", "strategy": strat,
            "why": f"模拟 {sn} 笔 / 实盘 {ln} 笔,都不够下结论",
        })

    return {
        "ts": time.time(),
        "days": days,
        "kinds": account_kinds(),
        "by_kind": by_kind,
        "advice": advice,
    }


def render(rep: dict) -> str:
    """给人看的一段。"""
    lines = [f"模拟/实盘反哺 · 最近 {rep.get('days')} 天"]
    kinds = rep.get("kinds") or {}
    if kinds:
        lines.append("账户类型:" + "、".join(
            f"{k}={v}" for k, v in kinds.items()))
    else:
        lines.append("账户类型:未配置(broker.account_kinds),"
                     "全部按模拟盘处理")

    lines.append("")
    for kind, label in (("live", "实盘"), ("sim", "模拟盘")):
        d = (rep.get("by_kind") or {}).get(kind) or {}
        if not d:
            continue
        lines.append(f"{label}:")
        for strat, s in sorted(d.items()):
            lines.append(
                f"  {strat}: n={s.get('n')} 胜率 {(s.get('win') or 0):.1%} "
                f"净 {(s.get('net_mean') or 0):+.2%}/笔")
        lines.append("")

    adv = rep.get("advice") or []
    if not adv:
        lines.append("建议:样本不足,暂无")
        return "\n".join(lines)

    tag = {"candidate": "候选", "confirmed": "确认",
           "diverge": "背离", "live_negative": "实盘为负",
           "sim_negative": "模拟为负", "insufficient": "样本不足"}
    lines.append("建议:")
    for a in adv:
        lines.append(f"  [{tag.get(a['level'], a['level'])}] "
                     f"{a['strategy']}: {a['why']}")
    return "\n".join(lines)
