# -*- coding: utf-8 -*-
"""涨停复盘:全市场涨停池、连板梯队、炸板/跌停与涨停分析。

数据来自东财 push2ex 的四个池(见 `data.em.zt_pool`)。这里只做三件事:
  1. 找到「哪一天有数据」:接口要显式日期,周末/节假日返回空,得自己回退;
  2. 把涨停池按连板数排成梯队,并和前一交易日的池子对比算晋级率;
  3. 给出可解释的统计(行业分布、封单、炸板率、一字/回封),落一份本地快照。

几个口径必须写清楚,别在界面上糊过去:
  · 连板数(lbc)只有涨停池有;炸板池/跌停池的 boards 一律 None,不拿 1 冒充首板。
  · 晋级率 = 昨日涨停股今日继续涨停 / 昨日涨停股。按连板层级拆时,
    「k 进 k+1」的基数是昨日 k 板,分子是今日 k+1 板且同一只票。
  · 炸板率 = 炸板数 / (涨停数 + 炸板数)。炸板池是「触板后没封住」,
    不含全天没摸过板的票,所以这个比例只描述摸板票的封板成败。
  · 一字板用「首封 ≤ 09:26 且全天未炸板」近似。集合竞价成交、9:25 就封住的
    就是一字;数据里没有开盘价,不再往下推。
  · 盘中拉到的池子是实时值,收盘前会变;页面必须标注「盘中」。
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from pathlib import Path

from ..config import DATA_DIR
from ..data import em

CACHE_TTL = 120.0          # 同一交易日的池子缓存 2 分钟(盘中会变,不能更长)
MAX_BACK_DAYS = 8          # 找最近有数据的交易日,最多回退 8 个工作日

_cache: dict[str, dict] = {}
_cache_ts: dict[str, float] = {}


# ------------------------------------------------------------------ 路径 / 日期
def _dir() -> Path:
    p = Path(DATA_DIR) / "limitup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(day: str) -> Path:
    return _dir() / f"{day}.json"


def recent_days(n: int = 12, end: dt.date | None = None) -> list[str]:
    """最近 n 个工作日(不含周末,不排节假日;节假日由接口空数据自然淘汰)。"""
    d = end or dt.date.today()
    out: list[str] = []
    guard = 0
    while len(out) < n and guard < n * 3 + 10:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
        guard += 1
    return out


def _is_today(day: str) -> bool:
    return day == dt.date.today().isoformat()


def _intraday(day: str) -> bool:
    if not _is_today(day):
        return False
    now = dt.datetime.now()
    return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 35)


# ------------------------------------------------------------------ 取数
def _fetch_pools(day: str, force: bool = False) -> dict:
    """一天的三只池子(zt / zb / dt)。按天缓存,盘中 2 分钟。"""
    import time
    key = f"pools:{day}"
    if not force:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - _cache_ts.get(key, 0) < CACHE_TTL:
            return hit
    pools = {}
    for kind in ("zt", "zb", "dt"):
        pools[kind] = em.zt_pool(day.replace("-", ""), kind)["rows"]
    _cache[key] = pools
    _cache_ts[key] = time.monotonic()
    return pools


def _load_saved(day: str) -> dict | None:
    p = _path(day)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(day: str, payload: dict) -> None:
    """原子落盘:先写 .tmp 再 replace,别让半截 JSON 留在正式路径上。"""
    p = _path(day)
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _find_day(start: dt.date | None = None, force: bool = False,
              max_back: int = MAX_BACK_DAYS) -> str | None:
    """从 start(含)往前找第一个涨停池非空的工作日。

    start 为空时从今天起找;但 09:15 前当天还没开盘,接口给的会是上一交易日的池子,
    所以直接从昨天起找,避免把昨天的涨停当今天的。
    """
    d = start or dt.date.today()
    if start is None and d == dt.date.today() and dt.datetime.now().time() < dt.time(9, 15):
        d -= dt.timedelta(days=1)
    tried = 0
    while tried < max_back:
        if d.weekday() < 5:
            day = d.isoformat()
            saved = None if force else _load_saved(day)
            if saved is not None:
                rows = ((saved.get("pools") or {}).get("zt")) or []
                if rows:
                    return day
            else:
                try:
                    rows = em.zt_pool(day.replace("-", ""), "zt")["rows"]
                except Exception:
                    rows = []
                if rows:
                    return day
            tried += 1
        d -= dt.timedelta(days=1)
    return None


def _prev_day(day: str, force: bool = False) -> str | None:
    """day 之前最近一个有涨停数据的交易日。"""
    d = dt.date.fromisoformat(day) - dt.timedelta(days=1)
    return _find_day(d, force=force, max_back=MAX_BACK_DAYS)


# ------------------------------------------------------------------ 统计
def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _boards(r: dict) -> int:
    b = r.get("boards")
    return int(b) if b else 1


def _sectors(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        name = (r.get("sector") or "未分类").strip() or "未分类"
        it = agg.setdefault(name, {"name": name, "count": 0, "boards_max": 0,
                                   "fund": 0.0, "codes": []})
        it["count"] += 1
        it["boards_max"] = max(it["boards_max"], _boards(r))
        it["fund"] += float(r.get("fund") or 0)
        it["codes"].append(r.get("code"))
    out = list(agg.values())
    out.sort(key=lambda x: (x["count"], x["boards_max"], x["fund"]), reverse=True)
    return out


def _promotion(today: list[dict], prev: list[dict]) -> dict:
    """连板晋级率。只认「昨日 k 板 → 今日 k+1 板」的同一只票。"""
    tmap = {r["code"]: _boards(r) for r in today if r.get("code")}
    pmap = {r["code"]: _boards(r) for r in prev if r.get("code")}
    if not pmap:
        return {"prev_total": 0, "promoted": 0, "rate": None, "by_level": []}
    by_level = []
    for k in sorted(set(pmap.values())):
        base = [c for c, b in pmap.items() if b == k]
        promoted = [c for c in base if tmap.get(c) == k + 1]
        by_level.append({"from": k, "base": len(base), "promoted": len(promoted),
                         "rate": _rate(len(promoted), len(base))})
    promoted_all = sum(1 for c, b in tmap.items() if b >= 2 and c in pmap)
    return {"prev_total": len(pmap), "promoted": promoted_all,
            "rate": _rate(promoted_all, len(pmap)), "by_level": by_level}


def _history(days: int = 8) -> list[dict]:
    """本地快照里的近 N 日概览(没有的日子跳过),按日期升序。"""
    out = []
    for p in sorted(_dir().glob("????-??-??.json"))[-days:]:
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s = saved.get("summary") or {}
        if s:
            out.append(s)
    return out


def analyze(day: str, rows: list[dict], broken: list[dict], limit_down: list[dict],
            prev: list[dict]) -> dict:
    """把三只池子算成一组可展示的统计与结论。纯函数,便于单测。"""
    zt_n, zb_n, dt_n = len(rows), len(broken), len(limit_down)
    zb_rate = _rate(zb_n, zt_n + zb_n)
    ladder_counts = Counter(_boards(r) for r in rows)
    max_board = max(ladder_counts) if ladder_counts else 0
    multi = sum(n for b, n in ladder_counts.items() if b >= 2)
    first = ladder_counts.get(1, 0)

    ladder = []
    for b in sorted(ladder_counts, reverse=True):
        items = [r for r in rows if _boards(r) == b]
        items.sort(key=lambda r: (-(float(r.get("fund") or 0)),
                                  r.get("first_time") or "99:99:99"))
        ladder.append({"boards": b, "count": len(items), "rows": items})

    one_word = [r for r in rows if r.get("break_count") == 0
                and (r.get("first_time") or "99") <= "09:26:00"]
    early = [r for r in rows if (r.get("first_time") or "99") <= "10:00:00"]
    late = [r for r in rows if (r.get("last_time") or "") >= "14:30:00"]
    reseal = [r for r in rows if int(r.get("break_count") or 0) > 0]
    fund_total = sum(float(r.get("fund") or 0) for r in rows)
    fund_top = sorted(rows, key=lambda r: -(float(r.get("fund") or 0)))[:5]
    for r in fund_top:
        cap = float(r.get("float_cap") or 0)
        r["fund_ratio"] = round(float(r.get("fund") or 0) / cap * 100, 2) if cap else None

    sectors = _sectors(rows)
    promo = _promotion(rows, prev)

    # 情绪标签:只描述数字,不做买卖建议
    if zt_n == 0:
        mood = "无数据"
    elif dt_n > zt_n * 1.5:
        mood = "退潮"
    elif zt_n >= 60 and (zb_rate or 0) <= 0.20:
        mood = "活跃"
    elif zt_n >= 30:
        mood = "一般"
    else:
        mood = "偏冷"

    notes = []
    if zb_rate is not None and zb_rate >= 0.30:
        notes.append(f"炸板率 {zb_rate*100:.0f}%,封板成功率偏低,追高容易吃面")
    if dt_n > zt_n:
        notes.append("跌停多于涨停,亏钱效应占上风")
    if max_board >= 5:
        notes.append(f"高度已到 {max_board} 板,空间打开;断板日波动也会更大")
    if multi == 0 and zt_n > 0:
        notes.append("没有连板,全是首板,接力意愿弱")
    if promo["rate"] is not None and promo["rate"] < 0.25 and promo["prev_total"] >= 5:
        notes.append(f"晋级率仅 {promo['rate']*100:.0f}%,昨日涨停股接力差")
    if sectors and sectors[0]["count"] >= 5:
        notes.append(f"主线集中在「{sectors[0]['name']}」({sectors[0]['count']} 只)")

    lines = []
    if zt_n == 0:
        lines.append("该交易日涨停池为空:不是非交易日,就是数据还没更新。")
    else:
        lines.append(f"【情绪】涨停 {zt_n} · 跌停 {dt_n} · 炸板 {zb_n}"
                     f"(炸板率 {(zb_rate or 0)*100:.0f}%)→ {mood}")
        lv = " / ".join(f"{'首板' if b == 1 else f'{b}板'} {n}" for b, n in
                        sorted(ladder_counts.items(), reverse=True))
        lines.append(f"【高度】最高 {max_board} 板;连板 {multi} 只,首板 {first} 只"
                     + (f";梯队 {lv}" if lv else ""))
        if promo["prev_total"]:
            parts = [f"{x['from']}进{x['from']+1} {x['promoted']}/{x['base']}"
                     for x in promo["by_level"] if x["base"] >= 2]
            lines.append(f"【晋级】昨日涨停 {promo['prev_total']} 只,今日继续涨停 "
                         f"{promo['promoted']} 只({(promo['rate'] or 0)*100:.0f}%)"
                         + ("; " + " · ".join(parts) if parts else ""))
        else:
            lines.append("【晋级】拿不到上一交易日的涨停池,本次不计算晋级率")
        if sectors:
            top = " · ".join(f"{s['name']} {s['count']}只(最高{s['boards_max']}板)"
                             for s in sectors[:4])
            lines.append(f"【主线】{top}")
        lines.append(f"【封单】合计 {fund_total/1e8:.2f} 亿"
                     + (f";最强 {fund_top[0]['name']} "
                        f"{(float(fund_top[0].get('fund') or 0))/1e8:.2f} 亿"
                        if fund_top else ""))
        lines.append(f"【特征】一字/竞价板 {len(one_word)} 只 · 10 点前封板 {len(early)} 只"
                     f" · 尾盘回封 {len(late)} 只 · 炸板后回封 {len(reseal)} 只")
        hist = _history(6)
        past = [h for h in hist if h.get("day") != day and h.get("zt")]
        if len(past) >= 2:
            seq = [h["zt"] for h in past[-5:]] + [zt_n]
            lines.append("【近几日涨停数】" + " → ".join(str(x) for x in seq))
        if notes:
            lines.append("【怎么看】" + ";".join(notes[:4]))

    summary = {"day": day, "zt": zt_n, "dt": dt_n, "zb": zb_n,
               "zb_rate": zb_rate, "max_board": max_board, "multi": multi,
               "promotion": promo.get("rate"),
               "fund_total": round(fund_total, 2), "mood": mood}
    return {
        "counts": {"zt": zt_n, "dt": dt_n, "zb": zb_n, "zb_rate": zb_rate,
                   "first": first, "multi": multi},
        "max_board": max_board,
        "ladder": ladder,
        "ladder_counts": {str(k): v for k, v in sorted(ladder_counts.items(), reverse=True)},
        "sectors": sectors,
        "promotion": promo,
        "fund_total": round(fund_total, 2),
        "fund_top": fund_top,
        "one_word": len(one_word),
        "early_seal": len(early),
        "late_seal": len(late),
        "reseal": len(reseal),
        "mood": mood,
        "notes": notes,
        "text": "\n".join(lines),
        "summary": summary,
    }


# ------------------------------------------------------------------ 对外
def snapshot(day: str | None = None, force: bool = False) -> dict:
    """取一天的完整复盘。

    day=None → 自动找最近有数据的交易日(今天盘中也能拿到实时值)。
    返回 ok / day / asked / rows / broken / limit_down / analysis / intraday。
    拿不到任何数据时 ok=False 且 error 说明原因,不抛给界面。

    两个必须挡住的坑(实测):
      · 未来日期:接口不报错,而是把**当天**的池子当成那一天返回 ——
        不拦就会把今天的 146 只涨停挂到明天名下。
      · 今天 09:15 前:还没开盘,接口给的仍是上一交易日的池子,同样张冠李戴。
    """
    asked = day or ""
    resolved = day
    today = dt.date.today()
    if resolved and "-" not in resolved and len(resolved) == 8:
        resolved = f"{resolved[:4]}-{resolved[4:6]}-{resolved[6:8]}"
    if resolved:
        try:
            asked_d = dt.date.fromisoformat(resolved)
        except ValueError:
            return {"ok": False, "asked": asked, "day": resolved,
                    "error": f"日期格式不对:{resolved}"}
        if asked_d > today:
            return {"ok": False, "asked": asked, "day": resolved,
                    "error": "未来的交易日还没有数据,别拿今天的数据冒充"}
        if asked_d == today and dt.datetime.now().time() < dt.time(9, 15):
            return {"ok": False, "asked": asked, "day": resolved,
                    "error": "今天还没开盘(09:15 前),没有当日涨停数据"}
    if not resolved:
        resolved = _find_day(None, force=force)
        if not resolved:
            return {"ok": False, "asked": asked, "day": "",
                    "error": "最近 8 个工作日都没有涨停数据(非交易日或接口不可用)"}
    try:
        pools = _fetch_pools(resolved, force=force)
    except Exception as e:                 # noqa: BLE001
        saved = _load_saved(resolved)
        if not saved:
            return {"ok": False, "asked": asked, "day": resolved,
                    "error": f"涨停池取数失败:{str(e)[:120]}"}
        pools = saved.get("pools") or {}
        saved_at = saved.get("saved_at")
    else:
        saved_at = None

    rows = pools.get("zt") or []
    broken = pools.get("zb") or []
    down = pools.get("dt") or []

    prev_day, prev_rows = "", []
    pd = _prev_day(resolved, force=force)
    if pd:
        try:
            prev_day = pd
            prev_rows = _fetch_pools(pd, force=force).get("zt") or []
        except Exception:                  # noqa: BLE001
            saved_prev = _load_saved(pd)
            if saved_prev:
                prev_day = pd
                prev_rows = (saved_prev.get("pools") or {}).get("zt") or []

    if not rows and not broken and not down:
        saved = _load_saved(resolved)
        if saved:
            rows = (saved.get("pools") or {}).get("zt") or []
            broken = (saved.get("pools") or {}).get("zb") or []
            down = (saved.get("pools") or {}).get("dt") or []
            saved_at = saved_at or saved.get("saved_at")
            prev_rows = (saved.get("prev") or []) or prev_rows
            prev_day = (saved.get("prev_day") or "") or prev_day

    an = analyze(resolved, rows, broken, down, prev_rows)
    out = {
        "ok": bool(rows or broken or down),
        "asked": asked,
        "day": resolved,
        "prev_day": prev_day,
        "rows": rows,
        "broken": broken,
        "limit_down": down,
        "analysis": an,
        "intraday": _intraday(resolved),
        "saved_at": saved_at or dt.datetime.now().isoformat(timespec="seconds"),
        "history": _history(8),
    }
    if out["ok"] and rows:
        _cache[resolved] = out
        try:
            _save(resolved, {"day": resolved, "pools": pools, "prev_day": prev_day,
                             "prev": prev_rows, "saved_at": out["saved_at"],
                             "summary": an["summary"]})
        except Exception:                  # noqa: BLE001
            pass                            # 落盘失败不影响看盘
    elif not out["ok"]:
        out["error"] = "该交易日涨停池为空:可能不是交易日,或数据还没更新"
    return out


def history(n: int = 10) -> list[dict]:
    """本地快照的逐日概览(界面上的小趋势用)。"""
    return _history(n)


def ladder_text(an: dict) -> str:
    """梯队一行摘要:5板 1 · 4板 2 · 3板 1 · 2板 4 · 首板 20。"""
    counts = an.get("ladder_counts") or {}
    parts = []
    for k in sorted(counts, key=lambda x: -int(x)):
        parts.append("首板" if int(k) == 1 else f"{k}板")
        parts[-1] += f" {counts[k]}"
    return " · ".join(parts) or "—"
