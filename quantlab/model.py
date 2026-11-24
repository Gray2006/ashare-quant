# -*- coding: utf-8 -*-
"""模型包:因子池与权重的来源。

公开版不连云端下发,只读本机 `USER_DIR/model/a-multifactor.json`(可选),
没有就用内置保守默认。自动交易拿不到参数时按默认跑,不会因此停摆。
"""
from __future__ import annotations

import json
import threading

from .config import USER_DIR

MODEL_DIR = USER_DIR / "model"

# 无本地包时的保守默认
DEFAULT_A_FACTORS = ("momentum20", "reversal5", "lowvol20")

_LOCK = threading.Lock()
_MEM: dict[str, dict] = {}


def _path(market: str, strategy: str):
    return MODEL_DIR / f"{market}-{strategy}.json"


def _norm(market: str) -> str:
    return "crypto" if str(market).lower().startswith("c") else "a"


def load(market: str = "a", strategy: str = "multifactor") -> dict:
    """读本地缓存包。没有就返回 {}。"""
    market = _norm(market)
    key = f"{market}-{strategy}"
    with _LOCK:
        if key in _MEM:
            return _MEM[key]
    try:
        doc = json.loads(_path(market, strategy).read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("factors"):
            with _LOCK:
                _MEM[key] = doc
            return doc
    except Exception:
        pass
    return {}


def refresh(market: str = "a", strategy: str = "multifactor",
            force: bool = False) -> dict:
    """公开版没有云端下发,直接返回本地缓存。"""
    return load(market, strategy)


def factors(market: str = "a", strategy: str = "multifactor") -> tuple[str, ...]:
    """参与打分的因子。没有本地包时用保守默认。"""
    doc = load(market, strategy)
    if doc.get("factors"):
        return tuple(str(f.get("key")) for f in doc["factors"] if f.get("key"))
    return DEFAULT_A_FACTORS


def weights(market: str = "a", strategy: str = "multifactor") -> dict[str, float]:
    doc = load(market, strategy)
    w = doc.get("weights") or {}
    return {str(k): float(v) for k, v in w.items()} if w else {}


def params(market: str = "a", strategy: str = "multifactor") -> dict:
    doc = load(market, strategy)
    return dict(doc.get("params") or {})


def start() -> None:
    """公开版不做后台拉取,留空以兼容调用方。"""
    return None
