"""数据回流的同意管理。

默认关闭。开启时必须由用户显式执行命令并看到完整字段清单。
只上传聚合后的回测指标，不上传股票代码、持仓、成交、账号或任何可识别信息。
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from .. import config

CONSENT_VERSION = "1.0"

CONSENT_TEXT = """\
数据回流只上传以下聚合字段，用于汇总不同参数下的策略表现：

- 程序版本、提交时间（精确到分钟）
- 因子名称、持仓数量、调仓频率、股票池数量
- 回测区间、交易日数、基准代码
- 年化收益、夏普、最大回撤、年化换手
- 同意条款版本

不会上传：股票代码、持仓明细、成交记录、资金规模、账号、设备标识、
IP 归属地、本机路径、任何个人身份信息。

数据默认关闭；可随时用 `aq feedback disable` 关闭。关闭后不再发送。
"""


def _valid_endpoint(endpoint: str) -> tuple[bool, str]:
    if not endpoint:
        return False, "未设置接收地址"
    parsed = urlparse(endpoint)
    if parsed.scheme == "https" and parsed.hostname:
        return True, ""
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"):
        return True, ""
    return False, "只允许 https 地址（本机调试可用 http://127.0.0.1）"


def status() -> dict:
    cfg = config.get("feedback", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled")),
        "endpoint": cfg.get("endpoint", ""),
        "consent_version": cfg.get("consent_version", ""),
        "consent_at": cfg.get("consent_at"),
        "current_version": CONSENT_VERSION,
    }


def enable(endpoint: str) -> dict:
    ok, reason = _valid_endpoint(endpoint)
    if not ok:
        raise ValueError(reason)
    cfg = config.load()
    cfg["feedback"] = {
        "enabled": True,
        "endpoint": endpoint,
        "consent_version": CONSENT_VERSION,
        "consent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    config.save(cfg)
    return status()


def disable() -> dict:
    cfg = config.load()
    feedback = cfg.setdefault("feedback", {})
    feedback["enabled"] = False
    feedback["endpoint"] = ""
    config.save(cfg)
    return status()
