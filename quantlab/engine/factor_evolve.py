# -*- coding: utf-8 -*-
"""因子进化闭环:审 → 定级 → 淘汰 → 重审。

为什么需要它:现在的因子挖掘每天都在挖,挖出来的东西要么入库要么丢弃,
但**没有记录「为什么被丢弃」**。结果就是下次再挖到同一个垃圾,
还要重新算一遍、人工再看一遍。

这里给因子库加三样东西:
  · audit    最近一次双口径审计结果(原始 t / 中性 t / 判定)
  · status   candidate(候选) / promoted(进观察池) / rejected(淘汰) / decayed(衰减)
  · history  审计历史,重审时追加,能看出是变好还是变差

判定规则(和 qagh 一致,不另立标准):
  · 原始 t ≥2 且 中性 t ≥2   → promoted
  · 原始 t ≥2 但 中性 t <2   → rejected(beta 伪装)
  · 两次连续不达标            → rejected
  · 曾 promoted 现在不达标    → decayed
"""
from __future__ import annotations

import time

from . import factor_analysis as fa
from . import factor_mine as fm

MIN_T = 2.0


def _data_day() -> str:
    """日线数据的最新日期 —— 审计结果的「数据指纹」。"""
    try:
        from ..data.store import store
        from ..data.kline import complete_day
        day = complete_day()
        if not day:
            return ""
        # 带上当日的覆盖数:盘中只有十几只落库,补全后五千多只,
        # 指纹跟着变才会重审 —— 否则会拿不完整的数据审一整天。
        n = store().conn.execute(
            "select count(*) from kline_cache where klt=101 and fqt=1 "
            "and bar_time=?", (day,)).fetchone()[0]
        return f"{day}@{n}"
    except Exception:  # noqa: BLE001
        return ""


def _verdict_of(raw_t: float | None, neu_t: float | None) -> tuple[str, str]:
    """(状态, 人话原因)"""
    r = -9.0 if raw_t is None else float(raw_t)
    n = -9.0 if neu_t is None else float(neu_t)
    if r >= MIN_T and n >= MIN_T:
        return "promoted", f"两份 t 都 ≥{MIN_T}"
    if r >= MIN_T and n < MIN_T:
        return "rejected", f"原始 t={r:.2f} 但中性化只有 {n:.2f},是 beta 伪装"
    if r < 0 and n < 0:
        return "rejected", f"两份都为负(原始 {r:.2f} / 中性 {n:.2f})"
    return "rejected", f"原始 t={r:.2f} 未达 {MIN_T}"


def audit_and_record(expr: str, label: str = "", panel: dict | None = None,
                     horizon: int = 5, save: bool = True,
                     force: bool = False) -> dict:
    """审一个因子并把结果写回库。返回审计结果。

    force=False 时,如果库里这条已经用**同一份数据**审过,直接返回上次结果 ——
    日线一天只更新一次,同一天重复审同一个表达式不可能变。
    """
    expr = (expr or "").strip()
    if not expr:
        return {"ok": False, "msg": "空表达式"}
    if not force:
        lib = fm.load_library()
        hit = next((x for x in lib if x.get("expr") == expr), None)
        if hit:
            a = hit.get("audit") or {}
            if a.get("data_day") and a.get("data_day") == _data_day():
                return {"ok": True, "skipped": True, "expr": expr,
                        "label": label or hit.get("label") or expr,
                        "raw_t": a.get("raw_t"), "neu_t": a.get("neu_t"),
                        "raw_excess": a.get("raw_excess"),
                        "neu_excess": a.get("neu_excess"),
                        "ic": a.get("ic"), "horizon": a.get("horizon"),
                        "status": hit.get("status") or a.get("status"),
                        "why": a.get("why"), "at": a.get("at"),
                        "msg": "数据没变,复用上次审计"}
    if panel is None:
        panel = fm._panel_unbiased(days=250, max_stocks=300, market="a")

    frames = fm._frames(panel)
    try:
        fac = fm._factor_panel(panel, expr)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"求值失败:{str(e)[:80]}"}
    if fac is None or fac.empty:
        return {"ok": False, "msg": "因子面板为空"}

    scores = fac.rank(axis=1, pct=True)
    rep = fa.audit_scores(frames, scores, horizon=horizon)
    raw = rep.get("raw") or {}
    neu = rep.get("neutral") or {}
    status, why = _verdict_of(raw.get("excess_t"), neu.get("excess_t"))
    out = {
        "ok": True, "expr": expr, "label": label or expr,
        "raw_t": raw.get("excess_t"), "neu_t": neu.get("excess_t"),
        "raw_excess": raw.get("excess_mean"),
        "neu_excess": neu.get("excess_mean"),
        "ic": rep.get("rank_ic"), "horizon": horizon,
        "status": status, "why": why, "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    if save:
        _write_back(out)
    return out


def _write_back(out: dict) -> None:
    """把审计结果写进因子库;没有这条就新建。"""
    lib = fm.load_library()
    hit = next((x for x in lib if x.get("expr") == out["expr"]), None)
    entry = {
        "data_day": _data_day(),
        "raw_t": out["raw_t"], "neu_t": out["neu_t"],
        "raw_excess": out["raw_excess"], "neu_excess": out["neu_excess"],
        "ic": out["ic"], "horizon": out["horizon"],
        "status": out["status"], "why": out["why"], "at": out["at"],
    }
    if hit is None:
        lib.append({
            "expr": out["expr"], "label": out["label"],
            "stats": {}, "created": out["at"],
            "audit": entry, "status": out["status"],
            "history": [entry],
        })
    else:
        prev = hit.get("status")
        # 曾经 promoted、这次不达标 → decayed
        if prev == "promoted" and out["status"] != "promoted":
            entry["status"] = "decayed"
            hit["status"] = "decayed"
        else:
            hit["status"] = out["status"]
        hit["audit"] = entry
        hist = hit.setdefault("history", [])
        hist.append(entry)
        hit["history"] = hist[-20:]          # 只留最近 20 次,别把文件撑爆
    try:
        fm.LIBRARY.parent.mkdir(parents=True, exist_ok=True)
        import json
        fm.LIBRARY.write_text(json.dumps(lib, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def evolve(panel: dict | None = None, horizon: int = 5,
           limit: int = 30, force: bool = False) -> dict:
    """重审库里所有因子,按结果升降级。返回汇总。"""
    lib = fm.load_library()
    if not lib:
        return {"ok": True, "n": 0, "msg": "因子库是空的,先挖一批"}
    if panel is None:
        panel = fm._panel(days=250, max_stocks=300, market="a")
    todo = lib[:limit]
    rows = []
    for i, item in enumerate(todo, 1):
        expr = item.get("expr") or ""
        if not expr:
            continue
        r = audit_and_record(expr, item.get("label") or "", panel=panel,
                             horizon=horizon, save=True, force=force)
        if r.get("ok"):
            rows.append(r)
        if i % 5 == 0:
            print(f"  重审 {i}/{len(todo)}", flush=True)
    from collections import Counter
    cnt = Counter(r["status"] for r in rows)
    return {"ok": True, "n": len(rows), "counts": dict(cnt),
            "promoted": [r for r in rows if r["status"] == "promoted"],
            "decayed": [r for r in rows if r["status"] == "decayed"],
            "rows": rows}


def leaderboard(top: int = 20) -> list[dict]:
    """按审计结果排的因子榜:先看两份 t 的较小值,再看 IC。"""
    lib = fm.load_library()
    def key(x):
        a = x.get("audit") or {}
        ts = [v for v in (a.get("raw_t"), a.get("neu_t")) if v is not None]
        return -(min(ts) if ts else -9)
    rows = sorted(lib, key=key)
    return [{"label": x.get("label"), "expr": x.get("expr"),
             "status": x.get("status"),
             "raw_t": (x.get("audit") or {}).get("raw_t"),
             "neu_t": (x.get("audit") or {}).get("neu_t"),
             "why": (x.get("audit") or {}).get("why")}
            for x in rows[:top]]
