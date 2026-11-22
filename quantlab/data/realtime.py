# -*- coding: utf-8 -*-
"""实时行情门面:多源级联 + 延迟统计。

级联顺序按「实测往返 + 字段完整度」排:
  腾讯 qt.gtimg.cn  →  东财 push2  →  新浪 hq.sinajs.cn

东财 push2 被 RST 时会进入 90 秒冷却(见 data/http.py),这里先判冷却再调用,
避免每次刷新都吃满 3 次重试退避——那正是之前行情"1 秒刷新、实际 4 秒才动"的原因。

统计口径:
  latency_ms  本次请求的墙钟往返(含连接复用后的首字节 + 完整体)
  p50/p95     最近 60 次成功请求的分位值,给界面显示"真实延迟"
"""
from __future__ import annotations

import threading
import time
from collections import deque

from . import em, http, moneyflow, sina, tx

# 顶栏四个指数 + 科创50 / 中证500(有其他页面要用)
INDEX_CODES = ["000001", "399001", "399006", "000300", "000688", "399905"]

_lock = threading.Lock()
_lat: deque[float] = deque(maxlen=60)
_last = {"source": "", "latency_ms": 0.0, "at": 0.0, "ok": False, "detail": ""}
_fail_streak = 0


def _record(source: str, latency_ms: float, ok: bool, detail: str = "") -> None:
    global _fail_streak
    with _lock:
        if ok:
            _lat.append(latency_ms)
            _fail_streak = 0
        else:
            _fail_streak += 1
        _last.update({"source": source, "latency_ms": latency_ms,
                      "at": time.time(), "ok": ok, "detail": detail})


def _pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return s[i]


def stats() -> dict:
    with _lock:
        vals = list(_lat)
        out = dict(_last)
        out["p50"] = _pct(vals, 0.50)
        out["p95"] = _pct(vals, 0.95)
        out["samples"] = len(vals)
        out["fail_streak"] = _fail_streak
    try:
        out["mf"] = moneyflow.stats()
    except Exception:  # noqa: BLE001
        out["mf"] = {}
    return out


def _em_url() -> str:
    return em.PUSH2 + "/api/qt/ulist.np/get"


def _coverage(data: dict, codes: list[str]) -> float:
    if not codes:
        return 0.0
    return sum(1 for c in codes if c in data) / len(codes)


def quotes(codes: list[str], timeout: float = 4.0) -> tuple[dict, str]:
    """按级联顺序取快照。返回 (行情字典, 实际来源)。全部失败时返回 ({}, "")。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}, ""
    t0 = time.perf_counter()
    errors: list[str] = []

    try:
        data = tx.quotes(codes, timeout=timeout, retries=1)
        if data and _coverage(data, codes) >= 0.5:
            el = (time.perf_counter() - t0) * 1000
            _record("腾讯", el, True)
            return data, "腾讯"
        errors.append("腾讯无数据")
    except Exception as e:  # noqa: BLE001
        errors.append(f"腾讯 {type(e).__name__}")

    if not http.is_cooling(_em_url()):
        try:
            data = em.quotes(codes, fallback=False, retries=1)
            if data and _coverage(data, codes) >= 0.5:
                el = (time.perf_counter() - t0) * 1000
                _record("东财", el, True)
                return data, "东财"
            errors.append("东财无数据")
        except Exception as e:  # noqa: BLE001
            errors.append(f"东财 {type(e).__name__}")

    try:
        data = sina.quotes(codes)
        if data and _coverage(data, codes) >= 0.5:
            el = (time.perf_counter() - t0) * 1000
            _record("新浪", el, True)
            return data, "新浪"
        errors.append("新浪无数据")
    except Exception as e:  # noqa: BLE001
        errors.append(f"新浪 {type(e).__name__}")

    _record("失败", (time.perf_counter() - t0) * 1000, False, " / ".join(errors))
    return {}, ""


def indexes(codes: list[str] | None = None, timeout: float = 3.0) -> list[dict]:
    """指数快照。腾讯 → 东财 → 新浪(指数代码显式映射,不串号)。"""
    codes = list(codes or INDEX_CODES)
    t0 = time.perf_counter()
    try:
        got = tx.index_quotes(codes, timeout=timeout, retries=1)
        if got:
            rows = [got[c] for c in codes if c in got]
            if rows:
                _record("腾讯", (time.perf_counter() - t0) * 1000, True)
                return rows
    except Exception:  # noqa: BLE001
        pass

    if not http.is_cooling(_em_url()):
        try:
            rows = em.indexes(retries=1)
            if rows:
                _record("东财", (time.perf_counter() - t0) * 1000, True)
                return rows
        except Exception:  # noqa: BLE001
            pass

    try:
        rows = sina.index_quotes(codes)
        if rows:
            _record("新浪", (time.perf_counter() - t0) * 1000, True)
            return rows
    except Exception:  # noqa: BLE001
        pass
    return []


# ---------------------------------------------------------------- 主力资金
# 实现全在 data/moneyflow.py:缓存 + 批量优先 + 并行兜底 + 负缓存 + 熔断感知。
# 这里只保留 realtime 的老接口,免得各处调用点一起改。

def moneyflow_apply(quotes: dict, max_age: float | None = None, wait: bool = False,
                    on_update=None) -> None:
    """把资金流补进行情字典(先贴缓存,缺的后台并行补,不卡行情刷新)。"""
    moneyflow.apply(quotes, max_age=max_age, wait=wait, on_update=on_update)


def moneyflow_paste(quotes: dict, max_age: float = 600.0) -> int:
    """只贴缓存、不发请求。Hub 重发上一帧时用这个,避免重复取数。"""
    return moneyflow.paste(quotes, max_age=max_age)


def moneyflow_snapshot() -> dict[str, dict]:
    return moneyflow.snapshot()


def moneyflow_stats() -> dict:
    return moneyflow.stats()
