"""本地配置：默认写在 ~/.ashare-quant/config.json，可用环境变量覆盖目录。

设计原则：
* 默认完全离线可用，只有显式执行 sync/quote 才访问网络；
* 数据回流默认关闭，必须用户显式开启；
* 配置里不存任何账号、密钥、持仓信息。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "ashare-quant"
CONFIG_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "http": {
        "timeout": 10.0,
        "retries": 2,
        "min_interval": 0.15,
        "max_interval": 5.0,
    },
    "sync": {
        "start": "2019-01-01",
        "page_size": 200,
    },
    "backtest": {
        "top_n": 20,
        "rebalance": "M",
        "fee_bps": 3.0,
        "stamp_bps": 10.0,
        "slippage_bps": 5.0,
        "benchmark": "000300",
    },
    "feedback": {
        "enabled": False,
        "endpoint": "",
        "consent_version": "",
        "consent_at": None,
    },
}


def home() -> Path:
    """配置与数据目录。ASHARE_QUANT_HOME 优先，便于测试与便携使用。"""
    raw = os.environ.get("ASHARE_QUANT_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.home() / ".ashare-quant"


def config_path() -> Path:
    return home() / "config.json"


def db_path() -> Path:
    return home() / "market.db"


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS)
    return _merge(DEFAULTS, raw)


def save(cfg: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def get(key: str, default: Any = None) -> Any:
    """点号路径读取，例如 get("feedback.enabled")。"""
    node: Any = load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_value(key: str, value: Any) -> dict[str, Any]:
    cfg = load()
    node = cfg
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    save(cfg)
    return cfg


def ensure_home() -> Path:
    path = home()
    path.mkdir(parents=True, exist_ok=True)
    return path
