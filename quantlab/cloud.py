# -*- coding: utf-8 -*-
"""云端客户端(公开版,只做数据回流)。

和私有版的区别:这里只有「上传训练样本」和「拉先验」两条路,没有交易、
没有券商、没有账户接口。地址与 key 都由用户在设置页自己填,默认关闭。

安全边界:
  * 只允许 https;本机调试放行 127.0.0.1 / localhost 的 http
  * key 存在 config.json 的 cloud.key,落盘按 SECRET_PATHS 自动加密
  * 所有请求带超时;失败抛 CloudError,由调用方决定要不要提示
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import cfg

TIMEOUT = 15


class CloudError(RuntimeError):
    pass


def _defaults() -> dict:
    """随包分发的默认回流服务(cloud_defaults.json)。

    这是「只收回流样本」的公共 key,写死在开源仓库里也没关系:
    服务端限制了 scope,拿它调模型/信号接口一律 403。
    """
    for base in (Path(__file__).resolve().parent.parent,
                 Path(__file__).resolve().parent):
        f = base / "cloud_defaults.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _conf() -> dict:
    c = cfg().get("cloud") or {}
    d = _defaults()
    # 优先级:环境变量 > 用户配置 > 随包默认
    url = str(os.environ.get("GRAY_CLOUD_URL") or c.get("base_url")
              or d.get("base_url") or "")
    key = str(os.environ.get("GRAY_CLOUD_KEY") or c.get("key")
              or d.get("public_key") or "")
    return {
        "base_url": url.rstrip("/"),
        "key": key,
        "enabled": bool(c.get("enabled")),
        "timeout": float(c.get("timeout") or TIMEOUT),
    }


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme == "https":
        return
    if u.scheme == "http" and (u.hostname or "") in ("127.0.0.1", "localhost"):
        return
    raise CloudError("服务端地址必须是 https(本机调试可用 127.0.0.1)")


def enabled() -> bool:
    c = _conf()
    return bool(c["enabled"] and c["base_url"] and c["key"])


def _call(method: str, path: str, *, params: dict | None = None,
          body: dict | None = None, timeout: float | None = None) -> dict:
    c = _conf()
    if not c["base_url"]:
        raise CloudError("还没填服务端地址")
    if not c["key"]:
        raise CloudError("还没填访问 key")
    url = f"{c['base_url']}{path}"
    _check_url(url)
    headers = {"Authorization": f"Bearer {c['key']}"}
    try:
        r = requests.request(method, url, headers=headers, params=params,
                             json=body, timeout=timeout or c["timeout"])
    except requests.RequestException as e:
        raise CloudError(f"连不上服务端:{str(e)[:80]}") from e
    if r.status_code == 401:
        raise CloudError("访问 key 无效或已失效")
    if r.status_code == 402:
        raise CloudError("额度不足")
    if r.status_code == 429:
        raise CloudError("请求过频,稍后重试")
    if r.status_code >= 400:
        detail = ""
        try:
            j = r.json()
            detail = (j.get("error") or {}).get("message") or j.get("detail") or ""
        except Exception:  # noqa: BLE001
            detail = r.text[:120]
        raise CloudError(f"服务端返回 {r.status_code}:{str(detail)[:160]}")
    try:
        return r.json()
    except ValueError as e:
        raise CloudError("服务端返回的不是 JSON") from e


def health() -> dict:
    c = _conf()
    if not c["base_url"]:
        raise CloudError("还没填服务端地址")
    try:
        r = requests.get(f"{c['base_url']}/health", timeout=5)
        return r.json()
    except requests.RequestException as e:
        raise CloudError(f"连不上服务端:{str(e)[:80]}") from e


def feedback_push(events: list[dict], timeout: float = 20) -> dict:
    return _call("POST", "/v1/feedback/events", body={"events": events},
                 timeout=timeout)


def feedback_priors(market: str = "a", strategy: str = "") -> dict:
    params = {"market": market}
    if strategy:
        params["strategy"] = strategy
    return _call("GET", "/v1/feedback/priors", params=params)
