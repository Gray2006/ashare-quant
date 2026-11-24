# -*- coding: utf-8 -*-
"""本地学习调度:全天常驻,收盘后跑一次学习作业。

以前学习挂在 AutoPilot 线程里 —— 不开自动交易就不学习。这里独立出来,
主窗口启动就常驻,和自动交易开关解耦。

作业内容(收盘后 15:30 起):
  1. 回填到期预测(sl_predictions)
  2. 补 5 只预测(按日期轮转,池子只拉 60 只)
  3. 出当日学习日报

防重复:config `learn.last_day` 记当天是否跑过;模块级 `_running` 防并发。
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from ..config import cfg

_lock = threading.Lock()
_running = False


def _backfill_decisions(em, sina, log=None) -> int:
    """把到期未回填的决策用现价补上结果,返回回填条数。

    pending() 按 account+strategy 过滤,所以按库里出现的组合逐个回填 ——
    只查当前配置那一组会漏掉别的策略记的决策。
    """
    from ..data.store import store
    from . import selflearn as sl

    rows = store().conn.execute(
        "SELECT DISTINCT account, strategy FROM sl_decisions "
        "WHERE outcome IS NULL AND dropped=0").fetchall()
    total = 0
    for r in rows:
        acc = str(r["account"] or "default")
        strat = str(r["strategy"] or "")
        dlog = sl.DecisionLog(acc, strat)
        pend = dlog.pending(limit=1000, oldest_first=True)
        if not pend:
            continue
        codes = sorted({str(d["code"]) for d in pend if d.get("code")})
        if not codes:
            continue
        quotes: dict = {}
        try:
            quotes = em.quotes(codes) or {}
        except Exception:  # noqa: BLE001
            try:
                quotes = sina.quotes(codes) or {}
            except Exception:  # noqa: BLE001
                quotes = {}
        if not quotes:
            if log:
                log(f"学习:决策回填取价失败({acc}/{strat})")
            continue
        n = int(dlog.backfill_by_quotes(quotes) or 0)
        total += n
        if log and n:
            log(f"学习:回填决策 {n} 条({acc}/{strat})")
    return total


def _close_backfill(em, sina, log=None) -> int:
    """收盘口径回填:当天下的单按收盘价打一次分。

    和 _backfill_decisions 分开跑:那条走 20 小时门槛,今天下的单今天不算;
    这条只看今天,收盘作业跑完就上传,让人当场看到「今天这批决定怎么样」。
    结果写 close_outcome,不进 ParamTuner 的样本。
    """
    import datetime as dt

    from ..data.store import store
    from . import selflearn as sl

    today0 = dt.datetime.combine(dt.date.today(), dt.time(0, 0)).timestamp()
    rows = store().conn.execute(
        "SELECT DISTINCT account, strategy FROM sl_decisions "
        "WHERE close_outcome IS NULL AND dropped=0 AND ts>=?",
        (today0,)).fetchall()
    total = 0
    for r in rows:
        acc = str(r["account"] or "default")
        strat = str(r["strategy"] or "")
        pend = store().conn.execute(
            "SELECT * FROM sl_decisions WHERE account=? AND strategy=? "
            "AND close_outcome IS NULL AND dropped=0 AND ts>=? "
            "ORDER BY ts ASC LIMIT 1000", (acc, strat, today0)).fetchall()
        codes = sorted({str(d["code"]) for d in pend if d["code"]})
        if not codes:
            continue
        quotes: dict = {}
        try:
            quotes = em.quotes(codes) or {}
        except Exception:  # noqa: BLE001
            try:
                quotes = sina.quotes(codes) or {}
            except Exception:  # noqa: BLE001
                quotes = {}
        if not quotes:
            if log:
                log(f"学习:收盘回填取价失败({acc}/{strat})")
            continue
        n = int(sl.DecisionLog(acc, strat).backfill_close(quotes, day_start=today0) or 0)
        total += n
        if log and n:
            log(f"学习:收盘回填 {n} 条({acc}/{strat})")
    return total


def run_daily_job(log=None) -> dict:
    """跑一次学习作业。log 是可选回调(接 UI 状态栏)。"""
    from . import predict_learn as pl
    from . import selflearn as sl
    from . import universe

    c = cfg()
    acc = str(c.get("autotrade.account", "default") or "default")
    strat = str(c.get("autotrade.strategy", "") or "")
    out = {"decided": 0, "reviewed": 0, "predicted": 0, "closed": 0, "report": ""}

    # 0) 收盘口径:先给今天的单打个分,快、当天就能看到结果(独立于 20 小时口径)
    try:
        from ..data import em, sina
        out["closed"] = _close_backfill(em, sina, log)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"学习:收盘回填失败 {e}")

    # 0.1) 回填决策:用现价评估之前的买卖决定,给参数迭代攒样本。
    #    以前只在 AutoTrader.run_once 里做,不开自动交易就永远不回填。
    try:
        from ..data import em, sina
        out["decided"] = _backfill_decisions(em, sina, log)
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"学习:决策回填失败 {e}")

    try:
        rv = pl.review_pending(acc, limit=100)
        out["reviewed"] = int(rv.get("reviewed") or 0)
        if log:
            log(f"学习:回填预测 {out['reviewed']} 条(到期 {rv.get('due', 0)})")
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"学习:回填失败 {e}")

    try:
        # 只取 60 只做轮转池。universe.build("all") 有 400 只下限,
        # 拉一次要几十秒;这里直接 top_by 并显式 fetch,别放大成 3 倍。
        rows = universe.top_by("amount", 60, fetch=60)
        codes = [r["code"] for r in rows if r.get("code")]
        if codes:
            n = 5
            off = (dt.date.today().toordinal() * n) % len(codes)
            codes = (codes[off:] + codes[:off])[:n]
            pr = pl.daily_predict(codes, horizon=5, account=acc)
            out["predicted"] = int(pr.get("ok") or 0)
            if log:
                log(f"学习:预测 {pr.get('ok', 0)}/{pr.get('total', 0)}"
                    + (f",跳过 {pr['skipped']}" if pr.get("skipped") else ""))
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"学习:预测失败 {e}")

    try:
        rep = sl.daily_report(acc, strat)
        sl.save_report(rep)
        out["report"] = "ok"
        if log:
            log("学习:日报已更新")
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"学习:日报失败 {e}")
    return out


def maybe_run_close(now: dt.datetime | None = None, log=None) -> bool:
    """收盘后 15:05 先单独跑一次「收盘口径打分」,不等 15:30 的大作业。

    为什么要拆开:大作业要拉 60 只票、跑 5 只预测,要好几分钟,那段时间
    spool 里什么都没有 —— 用户看到的是一整个收盘后「没有可上传」。这条只
    回填当天决策,几秒就完,上传得早,界面立刻能看到今天的结果。

    幂等:回填过的 close_outcome 不为空,15:30 大作业再跑时返回 0。
    """
    global _running
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    if now.hour * 60 + now.minute < 15 * 60 + 5:
        return False
    today = now.strftime("%Y-%m-%d")
    c = cfg()
    if str(c.get("learn.last_close_day", "") or "") == today:
        return False
    with _lock:
        if _running:
            return False
        _running = True
    c.set("learn.last_close_day", today, save=True)

    def _job() -> None:
        global _running
        try:
            from ..data import em, sina
            n = _close_backfill(em, sina, log)
            if log and n:
                log(f"学习:收盘打分 {n} 条已上传")
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"学习:收盘打分失败 {e}")
        finally:
            _running = False

    threading.Thread(target=_job, name="gray-close", daemon=True).start()
    return True


def maybe_run_daily(now: dt.datetime | None = None, log=None) -> bool:
    """到点且当天没跑过就起后台线程跑一次。返回是否启动。"""
    global _running
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    # 15:30 再跑:收盘后行情数据要几分钟才更新完
    if now.hour * 60 + now.minute < 15 * 60 + 30:
        return False
    today = now.strftime("%Y-%m-%d")
    c = cfg()
    if str(c.get("learn.last_day", "") or "") == today:
        return False
    with _lock:
        if _running:
            return False
        _running = True
    c.set("learn.last_day", today, save=True)

    def _job() -> None:
        global _running
        try:
            run_daily_job(log)
        finally:
            _running = False

    threading.Thread(target=_job, name="gray-learn", daemon=True).start()
    return True


class LearnScheduler(threading.Thread):
    """全天常驻:每 60 秒看一次表,到点跑作业。"""

    def __init__(self, log=None, check_seconds: int = 60):
        super().__init__(name="gray-learn-sched", daemon=True)
        self._stop = threading.Event()
        self._log = log
        self._check = max(30, int(check_seconds))

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                # 先 15:05 收盘打分(几秒),再 15:30 大作业(几分钟)
                maybe_run_close(log=self._log)
                maybe_run_daily(log=self._log)
            except Exception as e:  # noqa: BLE001
                if self._log:
                    self._log(f"学习调度异常:{e}")
            for _ in range(self._check * 10):
                if self._stop.is_set():
                    return
                time.sleep(0.1)
