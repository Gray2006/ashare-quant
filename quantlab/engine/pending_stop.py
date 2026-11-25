# -*- coding: utf-8 -*-
"""待执行止损:因 T+1 卖不掉的止损,记下来第二天优先处理。

背景(2026-11-25 查实):
  340 个回传样本里 37.9% 亏穿止损线,只有 0.9% 是正常止损。
  根因是 A 股 T+1 —— 当天买入的票当天不能卖,当天暴跌时止损
  在规则上就执行不了,只能等第二天。

  但「等第二天」现在是被动的:下一轮轮询时如果价格反弹回止损线以上,
  这笔止损就永远不会执行了。

做法:卖不掉的时候记一笔,下一轮开盘**优先检查这些票**,
      只要还亏着就直接卖,不等它重新跌破止损线。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "data" / "pending_stop.json"
KEEP_DAYS = 5          # 超过这么多天的记录直接丢,别越攒越多


def _load() -> dict:
    try:
        if PATH.exists():
            d = json.loads(PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(d: dict) -> None:
    try:
        now = time.time()
        for k in list(d):
            if now - float(d[k].get("ts") or 0) > KEEP_DAYS * 86400:
                d.pop(k, None)
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def record(code: str, reason: str = "", pnl: float | None = None) -> None:
    """记一笔「该止损但卖不掉」。"""
    if not code:
        return
    d = _load()
    key = str(code)
    if key in d:                      # 已经有记录,只更新次数
        d[key]["tries"] = int(d[key].get("tries") or 0) + 1
        d[key]["pnl"] = pnl
        d[key]["at"] = time.strftime("%Y-%m-%d %H:%M")
        _save(d)
        return
    d[key] = {"code": key, "reason": str(reason or "")[:60],
              "pnl": pnl, "ts": time.time(), "tries": 1,
              "at": time.strftime("%Y-%m-%d %H:%M")}
    _save(d)


def pending() -> dict:
    """还没执行的止损 {code: {...}}。"""
    return _load()


def clear(code: str) -> None:
    d = _load()
    if str(code) in d:
        d.pop(str(code), None)
        _save(d)


def clear_all() -> None:
    _save({})


def count() -> int:
    return len(_load())
