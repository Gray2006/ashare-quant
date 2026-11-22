"""数据回流协议：定义字段白名单与校验。

协议版本：ashare-quant.feedback/1
原则：最小必要、聚合、不可识别到个人。
"""
from __future__ import annotations

SCHEMA = "ashare-quant.feedback/1"

TOP_KEYS = {
    "schema", "app_version", "created_at", "consent_version",
    "strategy", "window", "metrics",
}
STRATEGY_KEYS = {"factor", "top_n", "rebalance", "universe"}
WINDOW_KEYS = {"start", "end", "trading_days", "benchmark"}
METRIC_KEYS = {"annual_return", "sharpe", "max_drawdown", "turnover_annual"}

FORBIDDEN_HINTS = (
    "code", "codes", "symbol", "symbols", "holding", "holdings",
    "trade", "trades", "position", "positions", "account", "user",
    "email", "phone", "ip", "device", "token", "key", "password",
    "path", "file", "order", "orders",
)


def _numbers(obj: dict) -> bool:
    for value in obj.values():
        if value is not None and not isinstance(value, (int, float)):
            return False
    return True


def validate(payload: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload 必须是 JSON 对象"]
    if set(payload) - TOP_KEYS:
        errors.append(f"存在未授权字段: {sorted(set(payload) - TOP_KEYS)}")
    if payload.get("schema") != SCHEMA:
        errors.append(f"schema 必须是 {SCHEMA}")
    if not isinstance(payload.get("app_version"), str):
        errors.append("app_version 缺失")
    if not isinstance(payload.get("created_at"), str):
        errors.append("created_at 缺失")
    if not isinstance(payload.get("consent_version"), str):
        errors.append("consent_version 缺失")

    strategy = payload.get("strategy")
    if not isinstance(strategy, dict) or set(strategy) - STRATEGY_KEYS:
        errors.append("strategy 字段不合法")
    elif not isinstance(strategy.get("factor"), str):
        errors.append("strategy.factor 缺失")
    elif not isinstance(strategy.get("top_n"), int) or not 1 <= strategy["top_n"] <= 500:
        errors.append("strategy.top_n 越界")
    elif not isinstance(strategy.get("rebalance"), (str, int)):
        errors.append("strategy.rebalance 缺失")
    elif not isinstance(strategy.get("universe"), int) or strategy["universe"] < 0:
        errors.append("strategy.universe 不合法")

    window = payload.get("window")
    if not isinstance(window, dict) or set(window) - WINDOW_KEYS:
        errors.append("window 字段不合法")
    else:
        for key in ("start", "end"):
            if not isinstance(window.get(key), str):
                errors.append(f"window.{key} 缺失")
        if not isinstance(window.get("trading_days"), int):
            errors.append("window.trading_days 缺失")
        if not isinstance(window.get("benchmark"), (str, type(None))):
            errors.append("window.benchmark 不合法")

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) - METRIC_KEYS:
        errors.append("metrics 字段不合法")
    elif not _numbers(metrics):
        errors.append("metrics 必须是数字或 null")

    text = str(payload).lower()
    for hint in FORBIDDEN_HINTS:
        if hint in text:
            errors.append(f"疑似包含禁止字段关键词: {hint}")
            break
    return errors
