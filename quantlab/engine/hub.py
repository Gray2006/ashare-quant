# -*- coding: utf-8 -*-
"""行情中枢与后台任务。

原则:所有网络请求都在后台线程,界面线程只做渲染。
  * QuoteHub  —— 自选股/指数定时轮询(亚秒级),带多源级联与延迟统计
  * TaskRunner —— 一次性后台任务(拉 K 线、新闻、回测…),成功/失败都回到界面

刷新节奏:
  * 交易时段按配置间隔(最小 0.25s,默认 1s)轮询,单次往返耗时从间隔里扣,
    不会出现「间隔 1s + 请求 0.4s = 实际 1.4s」的累积漂移;
  * 连续失败自动退避(×1.5,上限 8 倍),成功了立刻恢复;
  * 非交易时段放慢到 ≥10s,少打接口也少惹限流。
"""
from __future__ import annotations

import time
import traceback
import weakref
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..data import realtime

MIN_INTERVAL = 0.25

# 活着的后台线程登记表。有的线程(加密行情 hub、没登记进页面 _tasks 的
# TaskRunner)关窗时没人管,解释器退出时 Qt 报 "QThread: Destroyed while
# thread is still running" 并以 0xC0000409 退出。退出前统一扫一遍。
_LIVE: "weakref.WeakSet[QThread]" = weakref.WeakSet()


def track(thread: QThread) -> QThread:
    _LIVE.add(thread)
    return thread


def stop_live(wait_ms: int = 1500) -> None:
    """收掉所有还活着的后台线程:先 stop+wait,等不动的 terminate。"""
    live = [t for t in list(_LIVE) if t.isRunning()]
    for t in live:
        try:
            t.stop()          # TaskRunner 没有 stop,交给 AttributeError
        except AttributeError:
            pass
        except Exception:     # noqa: BLE001
            pass
    deadline = time.monotonic() + wait_ms / 1000.0
    for t in live:
        try:
            if t.isRunning():
                t.wait(int(max(50, (deadline - time.monotonic()) * 1000)))
        except Exception:     # noqa: BLE001
            pass
    for t in live:
        try:
            if t.isRunning():
                t.blockSignals(True)
                t.terminate()
                t.wait(300)
        except Exception:     # noqa: BLE001
            pass


class TaskRunner(QThread):
    """跑一个函数,结果通过信号回传。"""

    succeeded = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, int, str)

    def __init__(self, fn: Callable, *args, name: str = "", parent=None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.name = name or getattr(fn, "__name__", "task")
        track(self)

    def run(self) -> None:  # noqa: D102
        try:
            res = self._fn(*self._args, **self._kwargs)
            self.succeeded.emit(res)
        except Exception as e:  # noqa: BLE001
            detail = traceback.format_exc(limit=3)
            self.failed.emit(f"{e}\n{detail}")


class _Progress:
    """把 worker 里的 progress(i, n) 调用变成信号。"""

    def __init__(self, runner: TaskRunner):
        self.runner = runner

    def __call__(self, i: int, n: int) -> None:
        self.runner.progressed.emit(int(i), int(n), "")


class QuoteHub(QThread):

    quotes_ready = Signal(dict)
    indexes_ready = Signal(list)
    feed = Signal(dict)          # {"source","latency_ms","p50","p95","ok","fail_streak"}
    status = Signal(str)

    def __init__(self, codes: list[str], interval: float = 1.0, parent=None):
        super().__init__(parent)
        self._codes = list(codes)
        self._interval = max(MIN_INTERVAL, float(interval))
        self._running = True
        self._paused = False
        self._backoff = 1.0
        self._stats: dict = {}
        self._last: dict = {}
        track(self)

    def _on_mf(self) -> None:
        """资金流后台补到数据:只贴缓存后重推一次行情(不再触发取数)。"""
        if self._last:
            realtime.moneyflow_paste(self._last)
            self.quotes_ready.emit(self._last)

    def set_codes(self, codes: list[str]) -> None:
        self._codes = list(codes)

    def set_interval(self, seconds: float) -> None:
        self._interval = max(MIN_INTERVAL, float(seconds))

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------ 内部
    def _sleep(self, seconds: float) -> None:
        """可中断的睡眠:50ms 一片,stop/pause 能立刻生效。"""
        end = time.monotonic() + max(0.0, seconds)
        while self._running and not self._paused and time.monotonic() < end:
            self.msleep(50)
        if self._paused:
            while self._running and self._paused:
                self.msleep(100)

    def run(self) -> None:  # noqa: D102
        from ..utils import session_state
        next_index = 0.0
        while self._running:
            if self._paused or not self._codes:
                self.msleep(200)
                continue
            t0 = time.monotonic()
            try:
                data, _src = realtime.quotes(self._codes)
                if data:
                    self._last = data
                    # 先贴缓存零等待,行情立刻上屏;缺的交给后台并行补,
                    # 补到后 _on_mf 会再推一帧 —— 资金流永远不拖慢 1 秒一次的刷新
                    realtime.moneyflow_apply(data, on_update=self._on_mf)
                    self.quotes_ready.emit(data)
                self._stats = realtime.stats()
                self.feed.emit(self._stats)
            except Exception as e:  # noqa: BLE001
                self.status.emit(f"行情刷新失败:{e}")

            if time.monotonic() >= next_index:
                try:
                    idx = realtime.indexes()
                    if idx:
                        self.indexes_ready.emit(idx)
                except Exception:  # noqa: BLE001
                    pass
                next_index = time.monotonic() + 3.0

            ok = bool(self._stats.get("ok"))
            self._backoff = 1.0 if ok else min(8.0, self._backoff * 1.5)

            interval = self._interval * self._backoff
            # 非交易时段放慢,别白打接口(但还是让资金流/分时有第二次刷新机会)
            if session_state() not in ("call", "trade"):
                interval = max(interval, 5.0)
            elapsed = time.monotonic() - t0
            self._sleep(interval - elapsed)
