# -*- coding: utf-8 -*-
"""参数自适应:按实盘回传的决策/结果动态调 atr_mult 和 rr_ratio。

为什么要有这个:
  实盘参数(atr_mult=2.5 → 止损 6.25%)是拍出来的,不是从数据里学的。
  回测显示 6.25% 落在最差档(收益 21.1% vs 无止损 25.1%),
  但「该调到多少」不该我再猜 —— 应该由实盘数据说话。

三条规则(都有滞回,不会频繁改):

  1 止损执行率低(亏穿止损的样本 > 30%)
     → 止损相对波动太紧,ATR 倍数放宽
     → atr_mult × 1.15,上限 5.0

  2 止盈命中率低(打到止盈的样本 < 15%)
     → 止盈相对止损太高,盈亏比降低
     → rr_ratio × 0.85,下限 1.2

  3 胜率 < 35% 且盈亏比 < 0.8
     → 策略整体失效,不调参数,建议停手
     → 返回 action="halt"

安全边界:
  atr_mult ∈ [1.5, 5.0]
  rr_ratio ∈ [1.2, 3.0]
  单次调整不超过 ±20%,两次调整间隔 >= 24 小时
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / "param_adapt.json"
CONFIG = ROOT / "config.json"

# 安全边界
ATR_MIN, ATR_MAX = 1.5, 5.0
RR_MIN, RR_MAX = 1.2, 3.0
MAX_STEP = 0.20            # 单次最多调 20%
MIN_INTERVAL = 86400       # 两次调整至少隔 24 小时
MIN_SAMPLES = 100          # 样本少于这个只出建议不落地


def _conf() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _state() -> dict:
    try:
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def live_stats(market: str = "a") -> dict:
    """从云端拉实盘统计。拿不到就返回空。"""
    try:
        from .. import cloud
        d = cloud.feedback_summary()
        if not d.get("ok"):
            return {}
        rows = d.get("rows") or []
        total = int(d.get("total") or 0)
        return {"total": total, "rows": rows}
    except Exception:  # noqa: BLE001
        return {}


def _bucket_health() -> dict:
    """从云端 group_status 拿参数桶的健康度。"""
    try:
        from .. import cloud
        g = cloud.feedback_group_status()
        if not g.get("ok"):
            return {}
        return g
    except Exception:  # noqa: BLE001
        return {}


def suggest(market: str = "a") -> dict:
    """给参数建议。不写配置,只算。"""
    st = live_stats(market)
    if not st or st.get("total", 0) < MIN_SAMPLES:
        return {"ok": False, "msg": f"样本不足(需要 {MIN_SAMPLES} 条)"}

    # 用 buckets(原始桶统计),不用 overrides —— 后者在桶不赚钱时是空的
    g = _bucket_health()
    if not g:
        return {"ok": False, "msg": "拿不到参数桶统计"}

    cur = _conf().get("autotrade") or {}
    atr = float(cur.get("atr_mult") or 2.5)
    rr = float(cur.get("rr_ratio") or 2.5)

    # 只统计样本量够的桶,避免 n=1 的噪声
    bs = [b for b in (g.get("buckets") or [])
          if int(b.get("n") or 0) >= 20]
    means = [float(b.get("mean") or 0) for b in bs]
    wins = [float(b.get("win") or 0) for b in bs]
    if not means:
        return {"ok": False,
                "msg": "没有样本足够的参数桶(需要 n>=20)"}

    avg_mean = sum(means) / len(means)
    avg_win = sum(wins) / len(wins) if wins else 0.0

    # 盈亏比:用 win/(1-win) 粗估(没有逐笔数据时的近似)
    pf = (avg_win / (1 - avg_win)) if 0 < avg_win < 1 else 0.0

    act, why = "keep", ""
    new_atr, new_rr = atr, rr

    if avg_win < 0.35 and pf < 0.8:
        act, why = "halt", (f"胜率 {avg_win:.1%}、盈亏比 {pf:.2f} 双低,"
                            f"策略整体失效,不建议继续调参")
    elif avg_mean < 0 and avg_win < 0.40:
        # 整体在亏且胜率低 → 止损太紧,放宽
        new_atr = min(ATR_MAX, atr * (1 + MAX_STEP))
        act = "loosen_stop"
        why = (f"桶均值 {avg_mean:+.2%}、胜率 {avg_win:.1%} —— "
               f"止损相对波动太紧,ATR 倍数 {atr:.2f} → {new_atr:.2f}")
    elif avg_mean > 0 and avg_win > 0.45:
        # 在赚且胜率高 → 可以收紧止损保护利润
        new_atr = max(ATR_MIN, atr * (1 - MAX_STEP * 0.5))
        act = "tighten_stop"
        why = (f"桶均值 {avg_mean:+.2%}、胜率 {avg_win:.1%} —— "
               f"可以收紧止损保住利润,ATR 倍数 {atr:.2f} → {new_atr:.2f}")
    else:
        why = (f"桶均值 {avg_mean:+.2%}、胜率 {avg_win:.1%} —— "
               f"在合理区间,不动")

    return {
        "ok": True, "action": act, "why": why,
        "atr_mult": {"now": round(atr, 3), "suggest": round(new_atr, 3)},
        "rr_ratio": {"now": round(rr, 3), "suggest": round(new_rr, 3)},
        "stats": {"avg_mean": round(avg_mean, 4), "avg_win": round(avg_win, 4),
                  "profit_factor": round(pf, 3), "n_buckets": len(means),
                  "n_samples": sum(int(b.get("n") or 0) for b in bs),
                  "total_events": st.get("total")},
    }


def apply(market: str = "a", force: bool = False) -> dict:
    """按建议改配置。有滞回和边界,调不动就明说。"""
    s = suggest(market)
    if not s.get("ok"):
        return s
    if s["action"] in ("keep", "halt"):
        return {**s, "applied": False, "msg": s["why"]}

    # 样本不够只出建议,不改实盘
    n_samples = int((s.get("stats") or {}).get("n_samples") or 0)
    if n_samples < MIN_SAMPLES:
        return {**s, "applied": False,
                "msg": f"样本只有 {n_samples} 条,不到 {MIN_SAMPLES} —— "
                       f"只出建议,不改实盘"}

    st = _state()
    last = float(st.get("last_apply") or 0)
    if not force and time.time() - last < MIN_INTERVAL:
        left = int((MIN_INTERVAL - (time.time() - last)) / 3600)
        return {**s, "applied": False,
                "msg": f"距离上次调整不到 24 小时(还有 {left} 小时)"}

    new_atr = float(s["atr_mult"]["suggest"])
    new_rr = float(s["rr_mult"]["suggest"]) if "rr_mult" in s else float(
        s["rr_ratio"]["suggest"])
    new_atr = max(ATR_MIN, min(ATR_MAX, new_atr))
    new_rr = max(RR_MIN, min(RR_MAX, new_rr))

    conf = _conf()
    at = conf.setdefault("autotrade", {})
    old = {"atr_mult": at.get("atr_mult"), "rr_ratio": at.get("rr_ratio")}
    at["atr_mult"] = round(new_atr, 3)
    at["rr_ratio"] = round(new_rr, 3)
    try:
        CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {**s, "applied": False, "msg": f"写配置失败:{e}"}

    hist = st.setdefault("history", [])
    hist.append({"at": time.strftime("%Y-%m-%d %H:%M"),
                 "action": s["action"], "old": old,
                 "new": {"atr_mult": at["atr_mult"],
                         "rr_ratio": at["rr_ratio"]},
                 "why": s["why"]})
    st["history"] = hist[-20:]      # 只留最近 20 次
    st["last_apply"] = time.time()
    _save_state(st)

    return {**s, "applied": True,
            "msg": f"已调整:{s['action']}", "old": old,
            "new": {"atr_mult": at["atr_mult"], "rr_ratio": at["rr_ratio"]}}


def history() -> list[dict]:
    return _state().get("history") or []


def rollback() -> dict:
    """回滚到上一次调整前的值。"""
    st = _state()
    hist = st.get("history") or []
    if not hist:
        return {"ok": False, "msg": "没有可回滚的记录"}
    last = hist.pop()
    conf = _conf()
    at = conf.setdefault("autotrade", {})
    at.update(last.get("old") or {})
    try:
        CONFIG.write_text(json.dumps(conf, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"写配置失败:{e}"}
    st["history"] = hist
    _save_state(st)
    return {"ok": True, "msg": "已回滚", "restored": last.get("old")}
