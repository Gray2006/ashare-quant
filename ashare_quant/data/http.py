"""带限速、重试与熔断的 HTTP 客户端。

边界（重要）：
* 只访问公开、无需登录的行情接口；
* 不伪造 Referer、不附加任何私有 App 标识、不绕过验证码或风控；
* 对同一主机做全局节流，失败指数退避，连续失败触发熔断；
* User-Agent 如实标明项目身份，方便对方在需要时联系。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

import requests

from .. import __version__
from ..config import get

USER_AGENT = (
    f"ashare-quant/{__version__} "
    "(+https://github.com/Gray2006/ashare-quant; open-source research tool)"
)


class HttpError(RuntimeError):
    pass


class CircuitOpen(HttpError):
    pass


class RateLimiter:
    """按主机名串行节流，避免并发把对方服务打疼。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._next_at: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at[host])
            self._next_at[host] = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class HttpClient:
    def __init__(
        self,
        timeout: float | None = None,
        retries: int | None = None,
        min_interval: float | None = None,
    ) -> None:
        self.timeout = float(timeout if timeout is not None else get("http.timeout", 10.0))
        self.retries = int(retries if retries is not None else get("http.retries", 2))
        self.limiter = RateLimiter(
            min_interval if min_interval is not None else get("http.min_interval", 0.15)
        )
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._failures: dict[str, int] = defaultdict(int)
        self._open_until: dict[str, float] = defaultdict(float)

    def _check_circuit(self, host: str) -> None:
        until = self._open_until.get(host, 0.0)
        if until > time.monotonic():
            raise CircuitOpen(f"{host} 连续失败，暂停请求 {until - time.monotonic():.0f}s")

    def _record(self, host: str, ok: bool) -> None:
        if ok:
            self._failures[host] = 0
            self._open_until[host] = 0.0
            return
        self._failures[host] += 1
        if self._failures[host] >= 5:
            self._open_until[host] = time.monotonic() + 60.0
            self._failures[host] = 0

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        encoding: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> requests.Response:
        host = urlparse(url).hostname or ""
        attempts = (self.retries if retries is None else retries) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            self._check_circuit(host)
            self.limiter.wait(host)
            try:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=timeout or self.timeout,
                    headers={"Connection": "keep-alive"},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise HttpError(f"HTTP {response.status_code}")
                response.raise_for_status()
                if encoding:
                    response.encoding = encoding
                self._record(host, True)
                return response
            except (requests.RequestException, HttpError) as exc:
                last_error = exc
                self._record(host, False)
                if attempt + 1 < attempts:
                    time.sleep(min(2.0 ** attempt, 8.0))
        raise HttpError(f"请求失败 {url}: {last_error}")

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs) -> Any:
        response = self.get(url, params=params, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise HttpError(f"返回不是 JSON: {url}") from exc

    def get_text(self, url: str, params: dict[str, Any] | None = None, **kwargs) -> str:
        response = self.get(url, params=params, **kwargs)
        return response.text
