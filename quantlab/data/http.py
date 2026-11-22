# -*- coding: utf-8 -*-
"""HTTP 基础设施:连接复用、UA、重试、限速、JSONP 拆包、内存级短缓存。"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlencode

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_session = requests.Session()
_session.headers.update({
    "User-Agent": _UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
})

_lock = threading.Lock()
_last_call: dict[str, float] = {}
_MIN_INTERVAL = 0.12  # 同一 host 最小间隔,避免被限流

# 熔断:东财 push2 在高频请求后会直接 RST 连接(约 5~30 分钟)。
# 连续失败到阈值就对该 host 冷却,期间快速失败并由调用方切备用数据源。
_fail_streak: dict[str, int] = {}
_cooldown_until: dict[str, float] = {}
_FAIL_THRESHOLD = 3
_COOLDOWN_SECONDS = 90.0

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


class DataError(RuntimeError):
    pass


class HostCoolingDown(DataError):
    """该 host 处于冷却期,调用方应改用备用数据源。"""


def host_of(url: str) -> str:
    return url.split("/")[2] if "//" in url else url


def key_of(url: str) -> str:
    """熔断按 (host, 完整路径) 分桶。

    东财同一 host 下各端点会被单独限流(fflow 被封时 ulist 往往还好),
    按端点熔断才不会因为一个图表接口把行情一起拖死。
    """
    parts = url.split("/")
    if len(parts) < 4:
        return host_of(url)
    return parts[2] + "/" + "/".join(parts[3:])


def is_cooling(url: str) -> bool:
    """该端点是否在熔断冷却期(快速失败,不占用调用方时间)。"""
    until = _cooldown_until.get(key_of(url))
    return bool(until and time.monotonic() < until)


def cooldown_left(url: str) -> float:
    until = _cooldown_until.get(key_of(url))
    return max(0.0, until - time.monotonic()) if until else 0.0


def _throttle(host: str) -> None:
    """按桶限速:锁内只「预约」时间片,真正的等待放到锁外。

    以前 sleep 写在锁里,8 只股票并行取资金流时被这把锁排成一队,
    白白多花近 1 秒。改成预约制后,同桶仍然严格 ≥0.12s,不同桶可以真并行。
    """
    until = _cooldown_until.get(host)
    if until and time.monotonic() < until:
        raise HostCoolingDown(f"{host} 冷却中(剩余 {until - time.monotonic():.0f}s)")
    with _lock:
        now = time.monotonic()
        start = max(now, _last_call.get(host, 0.0) + _MIN_INTERVAL)
        _last_call[host] = start
    if start > now:
        time.sleep(start - now)


def _note_result(host: str, ok: bool) -> None:
    with _lock:
        if ok:
            _fail_streak[host] = 0
            return
        n = _fail_streak.get(host, 0) + 1
        _fail_streak[host] = n
        if n >= _FAIL_THRESHOLD:
            _cooldown_until[host] = time.monotonic() + _COOLDOWN_SECONDS
            _fail_streak[host] = 0


def get_text(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: float = 12.0, retries: int = 3, encoding: str | None = None) -> str:
    bucket = key_of(url)
    last_err: Exception | None = None
    for attempt in range(retries):
        _throttle(bucket)
        try:
            r = _session.get(url, params=params, headers=headers or {}, timeout=timeout)
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            elif r.encoding in (None, "ISO-8859-1"):
                r.encoding = r.apparent_encoding or "utf-8"
            _note_result(bucket, True)
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            _note_result(bucket, False)
            if isinstance(e, HostCoolingDown):
                break
            time.sleep(0.3 * (2 ** attempt) + random.random() * 0.15)
    raise DataError(f"请求失败 {url}: {last_err}")


_JSONP = re.compile(r"^[^({]*\((.*)\)\s*;?\s*$", re.S)


def parse_jsonp(text: str) -> Any:
    """把 'cb({...})' / 'var x={...};' 拆成 python 对象。"""
    t = text.strip()
    m = _JSONP.match(t)
    if m:
        t = m.group(1)
    elif "=" in t and not t.startswith("{"):
        t = t.split("=", 1)[1].strip().rstrip(";")
    return json.loads(t)


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: float = 12.0, retries: int = 3) -> Any:
    txt = get_text(url, params=params, headers=headers, timeout=timeout, retries=retries)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return parse_jsonp(txt)


_CURL = shutil.which("curl") or ("curl.exe" if os.name == "nt" else "curl")


def get_text_curl(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0) -> str:
    """用系统 curl 抓取(带浏览器 TLS 指纹)。

    东财 search-api 对 python-requests 的 TLS 指纹会返回降级结果,
      实测同一 URL:requests 返回 passportWeb(股吧用户搜索),curl 返回 cmsArticleWebOld(真新闻)。
      故此类接口走 curl;系统没有 curl 时由调用方回退到 get_text。
    """
    if params:
        url = url + ("&" if "?" in url else "?") + urlencode(params)
    cmd = [_CURL, "-sS", "--compressed", "--max-time", str(int(timeout)),
           "-H", "Connection: keep-alive"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 5,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise DataError(f"curl 失败({proc.returncode}): {proc.stderr.decode('utf-8', 'ignore')[:200]}")
    return proc.stdout.decode("utf-8", "replace")


def get_json_curl(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0) -> Any:
    txt = get_text_curl(url, params=params, headers=headers, timeout=timeout)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return parse_jsonp(txt)


def cached(key: str, ttl: float, fn):
    """极简 TTL 缓存。fn 抛错时不写缓存。"""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
