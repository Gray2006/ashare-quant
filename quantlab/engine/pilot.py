# -*- coding: utf-8 -*-
"""自动交易调度器:定时醒来 → 判断窗口 → 跑一次 AutoTrader。

两条使用路径:
  1. 界面里的「自动交易」页 —— AutoPilot(QThread) 常驻,开关控制;
  2. 命令行/脚本 —— run_due_once() 单次检查(适合 Windows 计划任务)。

调度规则刻意简单,不做复杂 cron:
  * 每个 check_seconds 醒一次(默认 30 秒);
  * 只在 autotrade.window_start~window_end(默认 14:55~15:05)且当日未跑过时执行;
  * force=True 时忽略窗口(界面上的「立即执行」);
  * 跑完记 last_run,同一天不会重复下单。
"""
from __future__ import annotations

import datetime as dt
import threading
import time

from PySide6.QtCore import QThread, Signal

from ..config import cfg
from ..utils import parse_hhmm
from .autotrader import AutoTrader
from .hub import track


def build_strategy(name: str, **params):
    """内置策略名或「rule:自定义名」→ 策略对象。"""
    from .autotrader import build_strategy as _b
    return _b(name, **params)


def strategy_choices() -> list[tuple[str, str]]:
    """界面下拉用:[(显示名, 内部名)]。"""
    from . import rules as rules_mod
    from . import strategy as st_mod
    out = [(f"内置·{s['desc'] or s['name']}", s["name"]) for s in st_mod.list_strategies()]
    out += [(f"自定义·{r['name']}", f"rule:{r['name']}") for r in rules_mod.list_rules()]
    return out


class AutoPilot(QThread):
    """常驻调度线程。"""

    plan_ready = Signal(object)      # Plan
    log = Signal(str)
    status = Signal(str)
    tick = Signal(dict)              # {next_run, last_run, enabled, ...}

    def __init__(self, gateway, parent=None):
        super().__init__(parent)
        self.gw = gateway
        self._stop = threading.Event()
        self._run_now = threading.Event()
        self._last_run_day: str = ""
        self._run_day: str = ""
        self._runs_today: int = 0
        self._last_plan = None
        self._next_check = 0.0
        self._lock = threading.Lock()
        track(self)

    # ------------------------------------------------------------ 配置
    def _conf(self) -> dict:
        c = cfg()
        return {
            "strategy": c.get("autotrade.strategy", "momentum20"),
            "pool": c.get("autotrade.pool", "hs300"),
            "n_stocks": int(c.get("autotrade.n_stocks", 60) or 60),
            "n_hold": int(c.get("autotrade.n_hold", 5) or 5),
            "per_position_pct": float(c.get("autotrade.per_position_pct", 0.18) or 0.18),
            "dry_run": bool(c.get("autotrade.dry_run", True)),
            "check_seconds": max(10, int(c.get("autotrade.check_seconds", 30) or 30)),
            "window_start": parse_hhmm(c.get("autotrade.window_start"), "14:55"),
            "window_end": parse_hhmm(c.get("autotrade.window_end"), "15:05"),
            "max_orders_per_day": int(c.get("autotrade.max_orders_per_day", 0) or 0),
            "account": str(c.get("autotrade.account", "default") or "default"),
            "codes": str(c.get("autotrade.codes", "") or ""),
            "stop_loss": float(c.get("autotrade.stop_loss_pct", 0) or 0) / 100,
            "take_profit": float(c.get("autotrade.take_profit_pct", 0) or 0) / 100,
            "max_runs_per_day": int(c.get("autotrade.max_runs_per_day", 1) or 0),
            # 回撤闸/动态仓位/全自动开关:定时执行也必须带上,
            # 否则界面里配的策略和真正跑的不是一套参数
            "max_drawdown": float(c.get("autotrade.max_drawdown_pct", 0) or 0),
            "dd_action": str(c.get("autotrade.dd_action", "halve") or "halve"),
            "adaptive": bool(c.get("autotrade.adaptive_enabled", True)),
            "adaptive_window": int(c.get("autotrade.adaptive_window", 10) or 10),
            "auto_mode": bool(c.get("autotrade.auto_mode", False)),
            "exec_mode": str(c.get("autotrade.exec_mode", "none") or "none"),
            "weight_mode": str(c.get("autotrade.weight_mode", "equal") or "equal"),
        }

    def _in_window(self, now: dt.datetime, conf: dict) -> bool:
        if now.weekday() >= 5:
            return False
        try:
            h0, m0 = [int(x) for x in conf["window_start"].split(":")]
            h1, m1 = [int(x) for x in conf["window_end"].split(":")]
        except Exception:
            h0, m0, h1, m1 = 14, 55, 15, 5
        cur = now.hour * 60 + now.minute
        return h0 * 60 + m0 <= cur <= h1 * 60 + m1

    # ------------------------------------------------------------ 线程体
    def run(self) -> None:
        self.status.emit("自动交易已启动")
        while not self._stop.is_set():
            conf = self._conf()
            now = dt.datetime.now()
            self._next_check = time.time() + conf["check_seconds"]
            self.tick.emit(self.state())
            self._maybe_learn(now)
            today = now.strftime("%Y-%m-%d")
            if self._run_day != today:      # 换日:执行次数清零
                self._run_day, self._runs_today = today, 0
            quota_left = conf["max_runs_per_day"] <= 0 or self._runs_today < conf["max_runs_per_day"]
            # 每天只跑一次时,last_run_day 防止重复;不限次/多次时只看当天已跑次数
            once_only = 0 < conf["max_runs_per_day"] <= 1
            day_gate = (self._last_run_day != today) if once_only else True
            window_due = self._in_window(now, conf) and quota_left and day_gate
            due = self._run_now.is_set() or window_due
            if due:
                self._run_now.clear()
                try:
                    self._execute(conf, force=self._run_now.is_set())
                except Exception as e:  # noqa: BLE001
                    self.log.emit(f"执行失败:{e}")
                    self.status.emit(f"自动交易异常:{e}")
            # 分片睡眠,能随时停止
            for _ in range(int(conf["check_seconds"] * 10)):
                if self._stop.is_set() or self._run_now.is_set():
                    break
                time.sleep(0.1)

    def _execute(self, conf: dict, force: bool = False) -> None:
        self.status.emit("正在计算调仓计划…")
        try:                     # 按配置切到目标模拟账户
            if conf.get("account"):
                self.gw.switch_account(conf["account"])
        except Exception:
            pass
        # 云端群体学习给的参数建议:开了才用,夹逼在合理区间
        pct = conf["per_position_pct"]
        sl = conf["stop_loss"]
        try:
            if cfg().get("autotrade.use_cloud_params", True):
                from .. import model
                mp = model.params("a", "multifactor")
                if mp.get("per_position_pct"):
                    pct = max(0.02, min(0.30, float(mp["per_position_pct"])))
                if mp.get("stop_loss"):
                    sl = max(0.01, min(0.30, float(mp["stop_loss"])))
        except Exception:
            pass
        at = AutoTrader(
            self.gw, strategy_name=conf["strategy"], pool=conf["pool"],
            n_stocks=conf["n_stocks"], n_hold=conf["n_hold"],
            per_position_pct=pct, dry_run=conf["dry_run"],
            max_orders_per_day=conf["max_orders_per_day"],
            codes=[x.strip() for x in conf["codes"].replace(" ", ",").split(",") if x.strip()] or None,
            stop_loss=sl, take_profit=conf["take_profit"],
            # 下面这几项以前没传 → 定时跑的时候回撤闸/动态仓位/全自动全部失效,
            # 只有界面上点「立即执行一次」才生效。参数从 cfg 兜底。
            max_drawdown=conf.get("max_drawdown"),
            dd_action=conf.get("dd_action"),
            adaptive=conf.get("adaptive"),
            adaptive_window=conf.get("adaptive_window"),
            auto_mode=conf.get("auto_mode"),
            exec_mode=conf.get("exec_mode") or "none",
            weight_mode=conf.get("weight_mode") or "equal",
        )
        plan = at.run_once()
        self._last_plan = plan
        self._last_run_day = dt.datetime.now().strftime("%Y-%m-%d")
        self._runs_today += 1
        self.plan_ready.emit(plan)
        self.status.emit(f"完成:{plan.message or plan.summary()}")
        self.log.emit(plan.summary())

    # ------------------------------------------------------------ 本地学习
    def _maybe_learn(self, now: dt.datetime) -> None:
        """收盘后跑一次本地学习作业。逻辑在 learn_scheduler,和自动交易解耦。

        以前学习挂在这个线程里,不开自动交易就不学习;现在主窗口的
        LearnScheduler 全天常驻,这里保留调用只是兜底(计划任务场景)。
        """
        from .learn_scheduler import maybe_run_daily
        maybe_run_daily(now, log=self.log.emit)

    # ------------------------------------------------------------ 控制
    def run_now(self) -> None:
        self._run_now.set()

    def stop(self) -> None:
        self._stop.set()
        self.wait(3000)

    def state(self) -> dict:
        conf = self._conf()
        return {
            "next_check_in": max(0, int(self._next_check - time.time())) if self._next_check else 0,
            "last_run_day": self._last_run_day,
            "next_run": conf["window_start"] + "~" + conf["window_end"],
            "runs_today": self._runs_today,
            "max_runs_per_day": conf["max_runs_per_day"],
            "max_orders_per_day": conf["max_orders_per_day"],
            "strategy": conf["strategy"],
            "dry_run": conf["dry_run"],
            "last_plan": self._last_plan.summary() if self._last_plan else "尚未执行",
        }


def make_gateway(kind: str | None = None):
    """建网关。公开版只有模拟盘,kind 参数留着兼容旧配置。"""
    from ..broker.sim import SimGateway
    return SimGateway()


def run_due_once(gateway=None) -> dict:
    """计划任务入口:在窗口内且当日未跑过才执行。返回执行摘要。"""
    c = cfg()
    key = "autotrade.last_run_day"
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if c.get(key) == today:
        return {"ran": False, "reason": "今天已经跑过"}
    gw = gateway or make_gateway()
    if getattr(gw, "is_live", False):
        ok, msg = gw.connect()
        if not ok:
            return {"ran": False, "reason": f"交易通道没连上:{msg}"}
    at = AutoTrader(gw, strategy_name=c.get("autotrade.strategy", "momentum20"),
                    pool=c.get("autotrade.pool", "hs300"),
                    n_stocks=int(c.get("autotrade.n_stocks", 60)),
                    n_hold=int(c.get("autotrade.n_hold", 5)),
                    dry_run=bool(c.get("autotrade.dry_run", True)),
                    max_orders_per_day=int(c.get("autotrade.max_orders_per_day", 0) or 0),
                    exec_mode=str(c.get("autotrade.exec_mode", "none") or "none"),
                    weight_mode=str(c.get("autotrade.weight_mode", "equal") or "equal"))
    plan = at.run_once(force=True)
    c.set(key, today, save=True)
    return {"ran": True, "plan": plan}
