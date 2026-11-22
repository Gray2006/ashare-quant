# -*- coding: utf-8 -*-
"""数据源健康:把 http.py 的熔断状态摆到台面上,并给一句「怎么办」。

设计原则和界面口径对齐:

· 只读 http 的模块级状态,不改它;探活是另开的一次轻量 GET,不参与熔断计数。
· 每条链路分两个维度看——「连接」(能不能通)和「数据」(通不通但有没有内容)。
  两者经常不同步:东财常见的是连接正常、返回空数据。
· 每一条异常都给一句建议,不要只报红。用户看到「已陈旧」时应该知道下一步做什么。

各页面取数成功时可以调 mark() 报个平安,健康页据此算「新鲜度」。
没报过的领域显示「未采集」,不编造。
"""
from __future__ import annotations

import threading
import time

from . import http as _http

# 领域 → 最后一次结果。例:mark("行情", True, "东财 120ms")
_marks: dict[str, dict] = {}
_lock = threading.Lock()

# 探活要跳过的桶:这些端点带了必填参数,裸访问会 400,拿 400 判「挂」是误报
_PROBE_SKIP = ("push2.eastmoney.com/api/qt/stock/fflow",)


def mark(domain: str, ok: bool = True, detail: str = "") -> None:
    """记一次取数结果。ok=False 只累加失败计数,不清掉上次成功时刻。"""
    now = time.time()
    with _lock:
        m = _marks.setdefault(domain, {"ok": None, "last_ok": 0.0, "last_try": 0.0,
                                       "detail": "", "fails": 0, "total": 0})
        m["last_try"] = now
        m["total"] = int(m.get("total") or 0) + 1
        if detail:
            m["detail"] = detail
        if ok:
            m["ok"] = True
            m["last_ok"] = now
            m["fails"] = 0
        else:
            m["ok"] = False
            m["fails"] = int(m.get("fails") or 0) + 1


def domains() -> list[dict]:
    """各业务领域的新鲜度。"""
    now = time.time()
    with _lock:
        items = [(k, dict(v)) for k, v in _marks.items()]
    out = []
    for name, m in items:
        age = (now - m["last_ok"]) if m["last_ok"] else None
        if m["ok"] is False:
            state, adv = "取数失败", "该领域上次取数失败,已自动重试;连续失败会切备用源"
        elif age is None:
            state, adv = "未采集", "本次启动还没取过这一类数据"
        elif age <= 60:
            state, adv = "正常", ""
        elif age <= 300:
            state, adv = "数据延迟", f"最近一次成功在 {age / 60:.0f} 分钟前,盘口可能已变"
        else:
            state, adv = "数据已陈旧", f"最近一次成功在 {age / 60:.0f} 分钟前,建议手动刷新"
        out.append({"name": name, "state": state, "age": age, "advice": adv,
                    "detail": m.get("detail") or "",
                    "fails": m.get("fails") or 0, "total": m.get("total") or 0})
    out.sort(key=lambda r: (r["age"] is not None, -(r["age"] or 0)))
    return out


def links() -> list[dict]:
    """http 层见过的每条 (host, 端点) 桶,带熔断/失败/最近调用。"""
    now = time.monotonic()
    seen: set[str] = set()
    for d in (_http._last_call, _http._cooldown_until, _http._fail_streak):
        seen.update(d.keys())
    out = []
    for bucket in seen:
        host, _, path = bucket.partition("/")
        until = float(_http._cooldown_until.get(bucket) or 0.0)
        cooling = bool(until and now < until)
        left = max(0.0, until - now) if until else 0.0
        fails = int(_http._fail_streak.get(bucket) or 0)
        last = _http._last_call.get(bucket)
        idle = (now - float(last)) if last else None
        if cooling:
            state, adv = "冷却中", (f"连续失败 {_http._FAIL_THRESHOLD} 次,"
                                   f"已自动停 {left:.0f} 秒;期间该端点直接走备用源")
        elif fails:
            state, adv = "重试中", f"已连续失败 {fails}/{_http._FAIL_THRESHOLD} 次,再失败一次就熔断"
        elif idle is not None and idle < 30:
            state, adv = "正常", ""
        elif idle is not None:
            state, adv = "空闲", f"{idle:.0f} 秒没有请求,不是故障"
        else:
            state, adv = "未使用", "本次启动还没调用过"
        out.append({"bucket": bucket, "host": host, "path": path, "state": state,
                    "cooling": cooling, "left": left, "fails": fails,
                    "idle": idle, "advice": adv})
    order = {"冷却中": 0, "重试中": 1, "正常": 2, "空闲": 3, "未使用": 4}
    out.sort(key=lambda r: (order.get(r["state"], 9), -(r["idle"] or 0)))
    return out


def summary() -> dict:
    """给顶部数字卡用的合计。"""
    ls = links()
    ds = domains()
    cooling = sum(1 for r in ls if r["cooling"])
    failing = sum(1 for r in ls if r["fails"] and not r["cooling"])
    ok = sum(1 for r in ls if r["state"] == "正常")
    with _http._cache_lock:
        cache_n = len(_http._cache)
    if cooling:
        verdict = f"{cooling} 条链路熔断中,其余{('可用' if ok else '也在重试')}"
        tone = "warn"
    elif failing:
        verdict = f"{failing} 条链路在重试,尚未影响取数"
        tone = "warn"
    elif ok:
        verdict = "全部链路正常"
        tone = "ok"
    else:
        verdict = "还没有取过数"
        tone = "idle"
    return {"links": len(ls), "cooling": cooling, "failing": failing, "ok": ok,
            "cache": cache_n, "domains": ds, "verdict": verdict, "tone": tone}


def probe(buckets: list[str] | None = None, limit: int = 8,
          timeout: float = 6.0) -> list[dict]:
    """对已知端点各发一次裸 GET,只报「通没通、多快」。

    裸 GET 不带参数,很多接口会返回空数据或 400 —— 这里只判断
    「能不能拿到 HTTP 响应」,拿到就说明网络与 host 是活的。
    探活不写熔断计数,避免把仪表盘变成故障源。
    """
    targets = [b for b in (buckets or [r["bucket"] for r in links()])
               if not any(s in b for s in _PROBE_SKIP)][:max(1, limit)]
    out = []
    for bucket in targets:
        url = bucket if "://" in bucket else f"https://{bucket}"
        t0 = time.perf_counter()
        try:
            _http._session.get(url, timeout=timeout, allow_redirects=False)
            ms = (time.perf_counter() - t0) * 1000
            out.append({"bucket": bucket, "ok": True, "ms": ms,
                        "note": "有响应(裸请求,不代表数据完整)"})
        except Exception as e:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            out.append({"bucket": bucket, "ok": False, "ms": ms,
                        "note": f"{type(e).__name__}: {str(e)[:60]}"})
    return out


def to_text(snap: dict | None = None) -> str:
    snap = snap or summary()
    lines = [f"数据源健康 · {snap['verdict']}", ""]
    lines.append(f"链路 {snap['links']} 条 · 正常 {snap['ok']} · "
                 f"重试 {snap['failing']} · 熔断 {snap['cooling']} · "
                 f"缓存 {snap['cache']} 条")
    lines.append("")
    lines.append("【业务领域新鲜度】")
    for d in snap["domains"]:
        age = "—" if d["age"] is None else f"{d['age']:.0f}s"
        lines.append(f"  {d['name']:<8}{d['state']:<6}距上次成功 {age}"
                     + (f"  {d['advice']}" if d["advice"] else ""))
    lines.append("")
    lines.append("【端点】")
    for r in links():
        tail = ""
        if r["cooling"]:
            tail = f"剩余 {r['left']:.0f}s"
        elif r["fails"]:
            tail = f"失败 {r['fails']}/{_http._FAIL_THRESHOLD}"
        lines.append(f"  {r['state']:<5}{r['bucket'][:70]:<72}{tail}")
    return "\n".join(lines)
