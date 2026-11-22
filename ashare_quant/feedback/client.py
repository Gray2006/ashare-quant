"""数据回流客户端：只发送聚合指标，默认不发送。

用法：
    aq feedback preview --factor momentum_20 ...
    aq feedback enable https://example.com/v1/feedback
    aq feedback send ...
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import requests

from .. import __version__
from ..config import get
from ..engine.backtest import BacktestResult
from . import consent, schema

USER_AGENT = f"ashare-quant/{__version__} feedback-client"


class FeedbackError(RuntimeError):
    pass


def build_payload(result: BacktestResult, meta: dict[str, Any]) -> dict:
    config = result.config
    metrics = result.metrics
    start = result.equity.index[0].strftime("%Y-%m-%d") if len(result.equity) else ""
    end = result.equity.index[-1].strftime("%Y-%m-%d") if len(result.equity) else ""
    payload = {
        "schema": schema.SCHEMA,
        "app_version": __version__,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "consent_version": consent.CONSENT_VERSION,
        "strategy": {
            "factor": str(meta.get("factor") or ""),
            "top_n": int(config.top_n),
            "rebalance": config.rebalance,
            "universe": int(meta.get("universe") or 0),
        },
        "window": {
            "start": start,
            "end": end,
            "trading_days": int(len(result.equity)),
            "benchmark": config.benchmark,
        },
        "metrics": {
            "annual_return": _round(metrics.get("annual_return")),
            "sharpe": _round(metrics.get("sharpe")),
            "max_drawdown": _round(metrics.get("max_drawdown")),
            "turnover_annual": _round(metrics.get("turnover_annual")),
        },
    }
    return payload


def _round(value) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def preview(result: BacktestResult, meta: dict[str, Any]) -> str:
    payload = build_payload(result, meta)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def submit(result: BacktestResult, meta: dict[str, Any]) -> dict:
    status = consent.status()
    if not status["enabled"]:
        raise FeedbackError("数据回流未开启。先执行 `aq feedback enable <https地址>`，或保持关闭。")
    payload = build_payload(result, meta)
    errors = schema.validate(payload)
    if errors:
        raise FeedbackError("本地校验失败: " + "; ".join(errors))
    endpoint = status["endpoint"]
    try:
        response = requests.post(
            endpoint,
            json=payload,
            timeout=10,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        raise FeedbackError(f"发送失败: {exc}") from exc
    if response.status_code >= 400:
        raise FeedbackError(f"服务端返回 {response.status_code}: {response.text[:200]}")
    try:
        data = response.json()
    except ValueError:
        data = {"ok": True, "raw": response.text[:200]}
    return {"sent": True, "endpoint": endpoint, "response": data}


def local_only(payload: dict) -> str:
    """不联网时把 payload 落到本地，便于用户核对。"""
    from .. import config

    path = config.ensure_home() / "feedback-preview.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
