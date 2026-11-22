# -*- coding: utf-8 -*-
"""安全地打开外部链接。

QDesktopServices.openUrl 会把 URL 交给系统默认程序处理:
- file:///C:/... .exe → 直接执行本地文件
- 自定义 scheme(ms-msdt、search-ms、javascript …) → 已知的协议处理器攻击面
- 极长/畸形编码 → 交给系统 shell 解析

所以任何来自网络(新闻源、云端反馈、更新清单)的 URL 都必须先过这里:
只放行 http/https,并拒绝本地/内网主机名(防止把用户引到局域网设备)。
(2026-11-22 审计 C3)
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

ALLOWED_SCHEMES = ("http", "https")

# 即使 scheme 合法,这些主机也不该由「外部链接」打开
_BLOCKED_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")


def is_safe(url: str, allow_hosts: list[str] | None = None) -> bool:
    """URL 是否允许交给系统打开。

    allow_hosts 非空时,主机名必须落在白名单里(支持 `*.example.com` 前缀通配)。
    """
    url = (url or "").strip()
    if not url or len(url) > 2048:
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    # 端口只允许 http(s) 默认端口:奇怪端口多半是内网服务
    try:
        port = u.port
    except ValueError:
        return False
    if port not in (None, 80, 443):
        return False
    if _is_private_host(host):
        return False
    if allow_hosts:
        return any(_host_matches(host, pat) for pat in allow_hosts)
    return True


def _host_matches(host: str, pattern: str) -> bool:
    pattern = (pattern or "").strip().lower()
    if not pattern:
        return False
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and host != pattern[2:]
    return host == pattern


def _is_private_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")   # 127.0.0.1. 也是本机
    if not host:
        return True
    if host in ("localhost", "localhost.localdomain"):
        return True
    if any(host.endswith(s) for s in _BLOCKED_SUFFIXES):
        return True
    # 浏览器认各种 IP 变体,ipaddress 不认:
    #   2130706433(十进制)、0x7f.1(十六进制)、0177.0.0.1(八进制)、127.1(缺段)
    if host.isdigit():
        return True
    if host.startswith("0x") or any(p.startswith("0x") for p in host.split(".")):
        return True
    if host.count(".") < 3 and all(p.isdigit() or p.startswith("0x") for p in host.split(".")):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # 单段主机名(如 intranet)只可能是内网/搜索域
        return "." not in host
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def open_external(url: str, parent=None, allow_hosts: list[str] | None = None) -> bool:
    """校验后交给系统浏览器。返回是否真的打开了。

    被拒绝时给用户一句人话,不静默失败 —— 否则看起来像按钮坏了。
    """
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QMessageBox

    if not is_safe(url, allow_hosts=allow_hosts):
        try:
            QMessageBox.warning(parent, "链接不可用",
                                f"这个链接不是安全的网页地址,已拦截:\n{url[:200]}")
        except Exception:
            pass
        return False
    try:
        return bool(QDesktopServices.openUrl(QUrl(url)))
    except Exception:
        return False
