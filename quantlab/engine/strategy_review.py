# -*- coding: utf-8 -*-
"""策略归因 —— 回答「哪条策略真在赚钱、哪条在赔、该减多少」。

为什么要有这一层:selflearn 原来只调**参数**(仓位/止损/止盈),不判断**策略本身**
还有没有效。参数调得再细,策略已经失效也白搭。这里把决策记忆按维度切开:

  * 按策略 —— 跨策略比,决定资源往哪条线倾斜
  * 按信号强度 —— 强信号是不是真的更值得下重手
  * 按持有天数 —— 拿几天最划算(短了被手续费吃,长了回吐)
  * 按批次 / 方向 / 市场状态

结论口径和其它地方一致:**扣双边成本后的均值**和 **t 值**,样本不足不下结论。

输出是**建议**,不是命令:降权(乘数)可以自动执行,暂停只写建议等人工确认
(selflearn.auto_suspend 打开才真停)。理由写清楚,能追溯。
"""
from __future__ import annotations

import time

from ..data.store import store

COST = 0.0015          # 兜底值。真实成本用 cost_rate() 从配置算
MIN_N = 12             # 少于这么多笔不下结论
MIN_DAYS = 3            # 至少几个交易日有回填才下结论
ACT_N = 30             # 要真降权/建议暂停,至少这么多笔
GATE_MIN_T = 2.0       # 升权要过的回测门槛:原始与中性 t 都要 ≥2
GATE_TTL = 1800.0      # 回测门槛结果缓存半小时(跑一次要几秒,不能每轮都跑)


def cost_rate() -> float:
    """双边总成本(比例),从 broker.sim 配置算。

    2026-11-27 发现 COST 硬编码 0.15%,而实际配置是:
      佣金 0.0085% + 过户费 0.001% + 滑点 0.1%  (买入)
      再加印花税 0.05%                          (卖出)
    双边合计约 0.269%,硬编码低估了 0.12 个百分点。

    低估成本会让归因偏乐观 —— 负的策略看着没那么负,
    正的策略看着更赚。这里改成从配置算,和回测/模拟盘同口径。
    """
    try:
        from ..config import cfg
        c = cfg()
        comm = float(c.get("broker.sim.commission_rate", 0.000085) or 0)
        transfer = float(c.get("broker.sim.transfer_fee_rate", 0.00001) or 0)
        stamp = float(c.get("broker.sim.stamp_tax_rate", 0.0005) or 0)
        slip = float(c.get("broker.sim.slippage_rate", 0.001) or 0)
        # 买入:佣金+过户费+滑点;卖出:再加印花税
        buy = comm + transfer + slip
        sell = comm + transfer + stamp + slip
        total = buy + sell
        return total if total > 0 else COST
    except Exception:  # noqa: BLE001
        return COST

# key = f"{strategy}:{days}" → (取的时间, 门槛结果)
_GATE_CACHE: dict[str, tuple[float, dict]] = {}


def _db():
    return store()


def backtest_gate(strategy: str, days: int = 250, horizon: int = 5,
                  max_stocks: int = 300, panel: dict | None = None) -> dict:
    """升权前的回测门槛:拿策略自己的因子定义,在无偏池上跑一遍双口径扣费回测。

    为什么必须这一层:实盘样本再好,也可能只是一段行情撞上了。升权意味着
    加钱,加钱前至少要证明「这套东西在历史上扣完费、剔掉 beta 之后还赚」。
    口径和因子审计完全一致(audit_scores:原始 + beta 中性化两份 t 都 ≥2)。
    """
    key = f"{strategy}:{days}"
    now = time.time()
    if panel is None:
        hit = _GATE_CACHE.get(key)
        if hit and now - hit[0] < GATE_TTL:
            return dict(hit[1])
    out: dict
    try:
        from . import factor_analysis as fa
        from . import factor_mine as fm
        from . import strategy as st_mod

        cls = st_mod.REGISTRY.get(strategy)
        if cls is None:
            out = {"ok": False, "why": f"{strategy} 不在 REGISTRY,没有可回测的策略定义"}
        else:
            data = fm._frames(panel if panel is not None
                              else fm._panel_unbiased(days, max_stocks))
            if not data:
                out = {"ok": False, "why": "没有行情面板"}
            else:
                strat = cls()
                scores = strat.factor_scores(data)
                if scores is None or scores.empty:
                    out = {"ok": False, "why": "策略算不出分数"}
                else:
                    n_long = int((strat.params or {}).get("n_long") or 10)
                    k = max(1, len(scores.columns))
                    top_pct = max(0.005, min(0.5, float(n_long) / k))
                    au = fa.audit_scores(data, scores, horizon=int(horizon),
                                         top_pct=top_pct)
                    raw = au.get("raw") or {}
                    neu = au.get("neutral") or {}
                    rt, nt = raw.get("excess_t"), neu.get("excess_t")
                    passed = (rt is not None and nt is not None
                              and float(rt) >= GATE_MIN_T
                              and float(nt) >= GATE_MIN_T)
                    out = {
                        "ok": bool(passed),
                        "raw_t": rt, "neutral_t": nt,
                        "excess": raw.get("excess_mean"),
                        "n": raw.get("n"), "horizon": int(horizon),
                        "verdict": au.get("verdict"),
                        "why": (f"回测双 t 都 ≥{GATE_MIN_T}" if passed else
                                f"回测未过门槛(原始 t={rt},中性 t={nt})"),
                    }
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "why": f"回测失败:{str(e)[:120]}"}
    if panel is None:
        _GATE_CACHE[key] = (now, dict(out))
    return out


def _ret(row: dict) -> float | None:
    """一条决策的结果:优先收盘口径,没有就用 20 小时口径。"""
    for key in ("close_outcome", "outcome"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _hold_days(row: dict) -> float | None:
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    end = row.get("close_at") or row.get("outcome_at")
    try:
        end = float(end or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or end <= ts:
        return None
    return (end - ts) / 86400.0


def summarize(items: list[dict]) -> dict:
    """一组的样本统计。返回的 net_mean 已经扣过成本。

    **口径警告**:`_ret()` 优先取 close_outcome(真实平仓),没有才用
    outcome(20 小时快照)。两种口径混算会让结论失真 ——
    2026-11-27 实测 rule:均线多头 43 条里只有 7 条是真实平仓,
    那 7 条均值 -0.47%/笔,而 36 条快照是 +0.13%/笔,
    混算出来 +0.07% 看着「唯一为正」,其实真实平仓是亏的。

    所以这里额外返回 n_close / n_snapshot,调用方要能看出口径构成。
    """
    rets = [r for r in (_ret(x) for x in items) if r is not None]
    n = len(rets)
    if not n:
        return {"n": 0, "win": None, "mean": None, "net_mean": None,
                "t": None, "total": None, "avg_hold": None, "best": None,
                "worst": None, "n_close": 0, "n_snapshot": 0}
    n_close = sum(1 for x in items if x.get("close_outcome") is not None)
    n_snapshot = n - n_close
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / max(1, n - 1)
    std = var ** 0.5
    t = mean / std * (n ** 0.5) if std > 1e-12 and n > 1 else None
    holds = [h for h in (_hold_days(x) for x in items) if h is not None]
    # 样本独立性:同一天几十笔是一起涨跌的,不能当几十个独立样本
    days_set = set()
    for x in items:
        ts = x.get("outcome_at") or x.get("ts")
        if ts:
            try:
                days_set.add(time.strftime("%Y-%m-%d", time.localtime(float(ts))))
            except (TypeError, ValueError, OSError):
                pass
    return {
        "n": n,
        "days": len(days_set),
        "win": round(sum(1 for x in rets if x > 0) / n, 4),
        "mean": round(mean, 6),
        "net_mean": round(mean - cost_rate(), 6),
        "t": round(t, 2) if t is not None else None,
        "total": round(sum(rets), 4),
        "avg_hold": round(sum(holds) / len(holds), 2) if holds else None,
        "best": round(max(rets), 4),
        "worst": round(min(rets), 4),
        "n_close": n_close,
        "n_snapshot": n_snapshot,
    }


def verdict(s: dict) -> dict:
    """由样本给出「维持 / 降权 / 暂停建议 / 加强」以及仓位乘数。"""
    n = int(s.get("n") or 0)
    if n < MIN_N:
        return {"state": "insufficient", "mult": 1.0,
                "why": f"样本 {n} 条,不足 {MIN_N},不下结论"}
    days = int(s.get("days") or 0)
    if days and days < MIN_DAYS:
        return {"state": "insufficient", "mult": 1.0,
                "why": (f"只有 {days} 个交易日有回填(要 ≥{MIN_DAYS}),"
                        f"同一天的几十笔不是独立样本,先不下结论")}
    net = s.get("net_mean")
    t = s.get("t")
    if net is None:
        return {"state": "insufficient", "mult": 1.0, "why": "没有回填结果"}
    if n >= ACT_N and t is not None and t <= -2.0:
        return {"state": "suspend", "mult": 0.5,
                "why": f"扣费均值 {net * 100:+.2f}%/笔,t={t},显著为负 —— 建议暂停"}
    if n >= ACT_N and net <= -0.01:
        return {"state": "suspend", "mult": 0.5,
                "why": f"扣费均值 {net * 100:+.2f}%/笔,亏损已超过 1%,建议暂停"}
    if n >= ACT_N and net < 0:
        return {"state": "downweight", "mult": 0.7,
                "why": f"扣费均值 {net * 100:+.2f}%/笔为负,降到 70% 仓位观察"}
    if t is not None and t >= 2.0 and net > 0:
        return {"state": "up", "mult": 1.1,
                "why": f"扣费均值 {net * 100:+.2f}%/笔,t={t},可加 10% 仓位"}
    return {"state": "neutral", "mult": 1.0,
            "why": f"扣费均值 {net * 100:+.2f}%/笔,没有足够证据改变"}


def _bucket_strength(v: float) -> str:
    if v >= 0.7:
        return "强(≥0.7)"
    if v >= 0.4:
        return "中(0.4~0.7)"
    return "弱(<0.4)"


def _bucket_hold(d: float | None) -> str:
    if d is None:
        return "未知"
    if d <= 1:
        return "≤1天"
    if d <= 3:
        return "1~3天"
    if d <= 7:
        return "3~7天"
    return ">7天"


def _group(items: list[dict], keyfn) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for it in items:
        try:
            k = keyfn(it)
        except Exception:  # noqa: BLE001
            k = "未知"
        groups.setdefault(str(k), []).append(it)
    out = []
    for k, rows in groups.items():
        s = summarize(rows)
        if not s["n"]:
            continue
        out.append({"key": k, **s})
    out.sort(key=lambda r: -(r.get("n") or 0))
    return out


def _fetch(account: str | None, days: int) -> list[dict]:
    since = time.time() - max(1, int(days)) * 86400
    sql = ("SELECT account,strategy,code,action,qty,price,strength,market,"
           "tranche,tranches,outcome,outcome_at,close_outcome,close_at,ts,note "
           "FROM sl_decisions WHERE ts>=? AND dropped=0 "
           "AND (outcome IS NOT NULL OR close_outcome IS NOT NULL)")
    args: list = [since]
    if account:
        sql += " AND account=?"
        args.append(account)
    sql += " ORDER BY ts"
    try:
        return [dict(r) for r in _db().conn.execute(sql, tuple(args)).fetchall()]
    except Exception:  # noqa: BLE001
        return []


def build(account: str | None = None, days: int = 90,
          strategy: str | None = None, use_gate: bool = True) -> dict:
    """归因主体。account=None 表示跨账户全看。

    升权(乘数 >1)必须再过一个回测门槛:实盘样本好也可能只是行情撞上了。
    门槛不过就退回「维持」,并写清为什么 —— 加钱的动作不能只凭几十笔成交。
    """
    rows = _fetch(account, days)
    if strategy:
        rows = [r for r in rows if str(r.get("strategy") or "") == strategy]

    by_strategy = []
    for g in _group(rows, lambda r: r.get("strategy") or "未命名"):
        v = verdict(g)
        item = {**g, "state": v["state"], "mult": v["mult"], "why": v["why"]}
        if use_gate and v["state"] == "up":
            gate = backtest_gate(str(g["key"]))
            item["gate"] = gate
            if not gate.get("ok"):
                item["state"] = "neutral"
                item["mult"] = 1.0
                item["why"] = (f"{v['why']},但升权要过回测门槛;"
                               f"{gate.get('why') or '回测未过'} —— 维持不加仓")
            else:
                item["why"] = f"{v['why']};{gate.get('why') or '回测过门槛'}"
        by_strategy.append(item)
    by_strategy.sort(key=lambda r: -((r.get("t") or -99)))

    overall = summarize(rows)
    dims = {
        "strength": _group(rows, lambda r: _bucket_strength(float(r.get("strength") or 0))),
        "action": _group(rows, lambda r: r.get("action") or "?"),
        "tranche": _group(rows, lambda r: "首笔" if int(r.get("tranche") or 1) <= 1
                          else "后续"),
        "market": _group(rows, lambda r: r.get("market") or "未知"),
        "hold": _group(rows, lambda r: _bucket_hold(_hold_days(r))),
    }
    # 按因子切分:决策表不记「哪几个因子触发的」,预测层记了(sl_predictions.factors),
    # 就拿预测层的逐因子命中率当因子归因 —— 同一条链上的另一段数据。
    try:
        from ..config import cfg as _cfg
        from . import predict_learn as pl
        acc_key = account or (_cfg().get("autotrade") or {}).get("account") or "默认账户"
        dims["factor"] = pl.PredictLog(str(acc_key)).factor_report()
    except Exception:  # noqa: BLE001
        dims["factor"] = []

    advice: list[str] = []
    for s in by_strategy:
        if s["state"] in ("suspend", "downweight", "up"):
            advice.append(f"策略 {s['key']}:{s['why']}(n={s['n']})")
    # 强度分层里,强信号反而更差 → 说明强度模型在骗人
    st = {d["key"]: d for d in dims["strength"]}
    weak = st.get("弱(<0.4)") or {}
    strong = st.get("强(≥0.7)") or {}
    if (weak.get("n") or 0) >= 8 and (strong.get("n") or 0) >= 8:
        if (strong.get("net_mean") or 0) < (weak.get("net_mean") or 0):
            advice.append(
                f"信号强度失效:强信号扣费均值 {strong['net_mean'] * 100:+.2f}%/笔,"
                f"弱信号 {weak['net_mean'] * 100:+.2f}%/笔 —— 强度不该再决定首笔大小")
    # 持有天数维度不可用于「该持有多久」的结论:
    # close_outcome / outcome 都是**快照口径**(当天收盘 / 20 小时报价),
    # 不是真实平仓时点。以前据此建议「延长最短持有」是误导,已改成数据提示。
    hd = {d["key"]: d for d in dims["hold"]}
    short = hd.get("≤1天") or {}
    if (short.get("n") or 0) >= 8:
        advice.append(
            f"持有天数维度是快照口径(close_outcome=当日收盘 / outcome=20 小时报价),"
            f"不是真实平仓时点 —— {short['n']} 笔落在「≤1天」是回填机制造成的,"
            "别据此调最短持有;要这个结论得先记录真实卖出时间")

    return {
        "account": account or "全部",
        "days": days,
        "n": int(overall["n"]),
        "overall": overall,
        "by_strategy": by_strategy,
        "dims": dims,
        "advice": advice,
        "ts": round(time.time(), 1),
    }


def _build_with_fallback(account: str, strategy: str, days: int) -> dict:
    """先按账户查,查不到就回退全账户。

    为什么:实盘账户名是「默认账户」(中文),但 ParamTuner 传的是 'default',
    精确匹配返回 0 条 → 归因系统完全没工作(2026-11-26 发现)。
    回退时在 why 里写明,别让人以为这是精确结论。
    """
    rep = build(account=account, days=days, strategy=strategy)
    if rep.get("by_strategy"):
        return rep
    rep2 = build(account=None, days=days, strategy=strategy)
    if rep2.get("by_strategy"):
        for s in rep2["by_strategy"]:
            s["why"] = (f"[账户名 {account!r} 无记录,按全账户算] "
                        + str(s.get("why") or ""))
        return rep2
    return rep


def tuner_mult(account: str, strategy: str, days: int = 90) -> tuple[float, str]:
    """给 ParamTuner 用:这条策略该乘多少仓位。拿不到数据就是 1.0。"""
    try:
        rep = _build_with_fallback(account, strategy, days)
    except Exception:  # noqa: BLE001
        return 1.0, ""
    for s in rep.get("by_strategy") or []:
        if s["key"] == (strategy or "未命名"):
            return float(s.get("mult") or 1.0), str(s.get("why") or "")
    return 1.0, ""


def state_of(account: str, strategy: str, days: int = 90) -> str:
    """这条策略当前归因结论(suspend/downweight/up/neutral/insufficient)。"""
    try:
        rep = _build_with_fallback(account, strategy, days)
    except Exception:  # noqa: BLE001
        return ""
    for s in rep.get("by_strategy") or []:
        if s["key"] == (strategy or "未命名"):
            return str(s.get("state") or "")
    return ""


def resolve_key(account: str, strategy: str, days: int = 7) -> dict:
    """配置里的 (account, strategy) 在库里没有记录时,指出实际写库的那个。

    根因:AutoTrader 实例用**启动时**的 strategy_name 写决策,
    learn_scheduler 用**当前** config 出报告。配置改过之后两边就对不上了,
    日报于是一片空白。这里不改交易逻辑,只让报告自证「我找的是谁、实际是谁」。
    """
    st = _db()
    since = time.time() - max(1, int(days)) * 86400
    try:
        hit = st.conn.execute(
            "SELECT COUNT(*) FROM sl_decisions WHERE account=? AND strategy=? AND ts>=?",
            (account, strategy, since)).fetchone()
        if int((hit or [0])[0] or 0) > 0:
            return {"account": account, "strategy": strategy, "source": "config",
                    "note": ""}
        row = st.conn.execute(
            "SELECT strategy, COUNT(*) n FROM sl_decisions WHERE account=? AND ts>=? "
            "AND strategy<>? GROUP BY strategy ORDER BY MAX(ts) DESC LIMIT 1",
            (account, since, strategy)).fetchone()
    except Exception:  # noqa: BLE001
        return {"account": account, "strategy": strategy, "source": "config",
                "note": ""}
    if row and str(row[0]):
        return {"account": account, "strategy": str(row[0]), "source": "db",
                "note": (f"配置的策略 {strategy or '(空)'} 近 {days} 天没有决策记录,"
                         f"库里最近写的是 {row[0]}({int(row[1])} 条),报告改用后者")}
    return {"account": account, "strategy": strategy, "source": "config",
            "note": ""}


def activity(account: str, day: str) -> dict:
    """某天库里到底写了什么 —— 日报显示 0 条时用来解释为什么。"""
    import datetime as dt
    d0 = dt.datetime.strptime(day, "%Y-%m-%d")
    start, end = d0.timestamp(), (d0 + dt.timedelta(days=1)).timestamp()
    st = _db()
    out = {"by_strategy": [], "by_account": [], "total": 0}
    try:
        rows = st.conn.execute(
            "SELECT strategy, COUNT(*) n FROM sl_decisions WHERE account=? "
            "AND ts>=? AND ts<? GROUP BY strategy", (account, start, end)).fetchall()
        out["by_strategy"] = [{"strategy": str(r[0] or ""), "n": int(r[1])}
                              for r in rows]
        rows = st.conn.execute(
            "SELECT account, COUNT(*) n FROM sl_decisions WHERE ts>=? AND ts<? "
            "GROUP BY account", (start, end)).fetchall()
        out["by_account"] = [{"account": str(r[0] or ""), "n": int(r[1])}
                             for r in rows]
        out["total"] = sum(x["n"] for x in out["by_account"])
    except Exception:  # noqa: BLE001
        pass
    return out
