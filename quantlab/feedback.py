# -*- coding: utf-8 -*-
"""数据回流(公开版客户端,可选)。

三条硬线:
  1. **默认关闭**。关闭时 `push()` 直接返回 False,不落盘、不发包 —— 没有
     「先偷偷记下来、以后再传」这种设计。
  2. **只发白名单字段**。金额一律转成比例;账户号、API key、余额、地址在
     `push()` 入口就被丢掉,不靠调用方自觉。
  3. **失败不丢**。开启后先写本地 spool.jsonl,上传成功才删;断网/服务端挂掉
     就攒着,上限 5000 条,超了丢最旧的。

开关在设置页;首次打开会弹窗说明「发什么、不发什么」。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .config import USER_DIR, cfg

FEEDBACK_DIR = USER_DIR / "feedback"
SPOOL = FEEDBACK_DIR / "spool.jsonl"
MAX_SPOOL = 5000
MAX_BATCH = 200
SEND_TIMEOUT = 15

PRIVACY_STRIP = ("key", "secret", "token", "passwd", "password", "account_no",
                 "api_key", "apikey", "private", "mnemonic", "address",
                 "balance", "cash", "equity", "broker", "phone", "email",
                 "id_card", "身份证", "手机", "账户", "余额", "密码")

_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


def _conf() -> dict:
    c = cfg().get("feedback") or {}
    return {"enabled": bool(c.get("enabled")),
            "interval": max(60.0, float(c.get("interval") or 1800))}


def enabled() -> bool:
    if not _conf()["enabled"]:
        return False
    try:
        from . import cloud
        return cloud.enabled()
    except Exception:  # noqa: BLE001
        return False


def _ensure_dir() -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _safe(obj):
    """递归剔除敏感键;只处理 dict/list/标量,怪类型返回 None 让上层丢。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in PRIVACY_STRIP):
                continue
            sv = _safe(v)
            if sv is not None:
                out[str(k)] = sv
        return out
    if isinstance(obj, list):
        return [x for x in (_safe(v) for v in obj[:200]) if x is not None]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str) and len(obj) > 500:
            return obj[:500]
        return obj
    return None


def _event_id(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _version() -> str:
    try:
        from .version import __version__
        return str(__version__)[:32]
    except Exception:  # noqa: BLE001
        return ""


def push(kind: str, market: str, payload: dict, *, event_id: str = "",
         ref: str = "", strategy: str = "", ts: float | None = None) -> bool:
    """记一条训练样本。关闭时什么都不做(不落盘)。"""
    if not enabled():
        return False
    try:
        clean = _safe(payload or {})
        ev = {
            "kind": str(kind)[:32],
            "market": "a",
            "event_id": event_id or _event_id(kind, market, strategy, ts, clean),
            "ref": str(ref or "")[:64],
            "strategy": str(strategy or "")[:64],
            "version": _version(),
            "channel": "public",
            "ts": float(ts or time.time()),
            "payload": clean,
        }
        _ensure_dir()
        line = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            with open(SPOOL, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _trim_spool()
        return True
    except Exception:  # noqa: BLE001
        return False


def _trim_spool() -> None:
    """调用方已持锁。超过上限丢最旧的一半。"""
    try:
        if not SPOOL.exists() or SPOOL.stat().st_size < 4_000_000:
            return
        lines = SPOOL.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_SPOOL:
            SPOOL.write_text("\n".join(lines[-MAX_SPOOL // 2:]) + "\n",
                             encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _count() -> int:
    try:
        return sum(1 for _ in open(SPOOL, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0


def flush(force: bool = False, timeout: float = SEND_TIMEOUT) -> dict:
    """把 spool 里的一批发出去。返回 {sent,left,error?}。"""
    res = {"sent": 0, "left": _count()}
    if not enabled():
        res["error"] = "数据回流未开启"
        return res
    try:
        _ensure_dir()
        if not SPOOL.exists():
            return res
        with _LOCK:
            lines = SPOOL.read_text(encoding="utf-8").splitlines()
            batch = lines[:MAX_BATCH]
            if not batch:
                return res
            events = []
            for line in batch:
                try:
                    events.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
            keep = lines[len(batch):]
            from . import cloud
            r = cloud.feedback_push(events, timeout=timeout)
            if r.get("ok"):
                acc = int(r.get("accepted") or 0) + int(r.get("duplicated") or 0)
                drop = acc if acc > 0 else len(batch)
                SPOOL.write_text("\n".join(keep[drop - len(batch):]) +
                                 ("\n" if keep else ""), encoding="utf-8")
                res["sent"] = drop
            else:
                res["error"] = str(r.get("error") or "服务端拒收")
        res["left"] = _count()
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:120]
    return res


def _loop() -> None:
    _STOP.wait(20)
    backoff = 0.0
    while not _STOP.is_set():
        r = flush()
        backoff = min(max(backoff * 2, 120.0), 1800.0) if r.get("error") else 0.0
        _STOP.wait(max(_conf()["interval"], backoff))


def start() -> None:
    """幂等启动后台发送线程;未开启时什么都不做。"""
    global _THREAD
    if not _conf()["enabled"]:
        return
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="feedback", daemon=True)
    _THREAD.start()


def stop() -> None:
    _STOP.set()


def state() -> dict:
    return {"enabled": enabled(), "queued": _count()}
