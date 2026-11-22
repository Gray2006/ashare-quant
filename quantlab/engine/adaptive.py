# -*- coding: utf-8 -*-
"""自动交易的资金守卫:回撤闸 + 动态仓位系数。

两件事分开看:

1) **回撤闸**(DrawdownGuard)
   记录账户权益的历史最高点,算当前回撤 = 1 - 权益/峰值。
   回撤超过阈值就按配置动作处理:
     stop_new  —— 停止开新仓(卖照常),等回撤修复再恢复
     halve     —— 新仓预算打五折
     liquidate —— 清仓
   恢复条件是回撤回落到 `resume_ratio × 阈值` 以下(默认一半),避免在阈值边上
   反复触发/恢复来回抖。也可手动重置。

2) **动态仓位系数**(AdaptiveSizer)
   **这不是机器学习**,是一个确定性反馈环:把每次调仓后的权益变化率存成样本,
   看最近 N 次的均值与胜率,映射成一个仓位系数(0.5~1.25),再乘到单票预算上。
   - 样本不足(默认 <4 次)一律 1.0,不给结论
   - 系数变化要跨过 0.15 才动,且两次调整之间至少隔 2 次调仓(滞回,防止抖动)
   - 每次调整都写进 signals 日志,能追溯是哪次调仓改的

为什么用「权益变化率」而不是「已实现盈亏」:位置还在手上时按现价算的变化也是真实
盈亏,只统计平仓会漏掉浮亏期的风险。代价是资金进出(充值/提现)会被当成盈亏,
模拟盘里不影响,接真钱通道要改成扣除出入金。

状态都存 store 的 account 字典里(键 autotrade_guard / autotrade_adaptive),
重启不丢。
"""
from __future__ import annotations

import time

from ..data.store import store

GUARD_KEY = "autotrade_guard"
ADAPT_KEY = "autotrade_adaptive"

ACTIONS = [("停止开新仓", "stop_new"), ("新仓减半", "halve"),
           ("全部清仓", "liquidate")]


def _load(key: str) -> dict:
    try:
        v = store().get_account(key, {})
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(key: str, val: dict) -> None:
    try:
        store().set_account(key, val)
    except Exception:  # noqa: BLE001
        pass


class DrawdownGuard:
    """回撤闸。每次调仓前调 `evaluate(equity)` 拿决策。"""

    def __init__(self, account: str = "default"):
        self.account = account or "default"

    def _all(self) -> dict:
        return _load(GUARD_KEY)

    def _state(self) -> dict:
        all_ = self._all()
        return all_.get(self.account) or {"peak": 0.0, "triggered": False,
                                          "triggered_at": 0.0, "reason": ""}

    def _write(self, st: dict) -> None:
        all_ = self._all()
        all_[self.account] = st
        _save(GUARD_KEY, all_)

    def peek(self, equity: float, max_pct: float = 0.0,
             resume_ratio: float = 0.5) -> dict:
        """只读地算一遍,不写任何状态。

        界面刷新必须走这个 —— 用 evaluate 会让「打开页面」本身改动峰值和触发状态,
        等于看的人改了配置。
        """
        st = self._state()
        out = {"dd": 0.0, "peak": 0.0, "triggered": bool(st.get("triggered")),
               "block_buys": False, "liquidate": False, "scale": 1.0,
               "message": st.get("reason") or ""}
        equity = float(equity or 0)
        if equity <= 0:
            return out
        peak = max(float(st.get("peak") or 0), equity)
        dd = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        out.update({"dd": dd, "peak": peak})
        if max_pct <= 0:
            out["triggered"] = False
            return out
        if out["triggered"]:
            # 显示时按同样的 resume 规则判断「其实已经修复了」
            if dd <= max_pct * max(0.0, min(1.0, resume_ratio)):
                out["triggered"] = False
        elif dd >= max_pct:
            out["triggered"] = True
        return out

    def reset(self) -> None:
        st = self._state()
        st.update({"peak": 0.0, "triggered": False, "triggered_at": 0.0,
                   "reason": "手动重置"})
        self._write(st)

    def evaluate(self, equity: float, max_pct: float = 0.0,
                 action: str = "stop_new", resume_ratio: float = 0.5) -> dict:
        """返回 {dd, peak, triggered, block_buys, liquidate, scale, message}。"""
        out = {"dd": 0.0, "peak": 0.0, "triggered": False, "block_buys": False,
               "liquidate": False, "scale": 1.0, "message": ""}
        equity = float(equity or 0)
        if equity <= 0:
            out["message"] = "权益为 0,回撤闸不判"
            return out

        st = self._state()
        peak = max(float(st.get("peak") or 0), equity)
        dd = 0.0 if peak <= 0 else max(0.0, 1.0 - equity / peak)
        out.update({"dd": dd, "peak": peak})

        if max_pct <= 0:
            # 没开回撤控制,只维护峰值,不做限制
            if abs(peak - float(st.get("peak") or 0)) > 1e-9:
                st["peak"] = peak
                self._write(st)
            return out

        triggered = bool(st.get("triggered"))
        if not triggered and dd >= max_pct:
            triggered = True
            st.update({"triggered": True, "triggered_at": time.time(),
                       "reason": f"回撤 {dd:.2%} 触及上限 {max_pct:.2%}"})
        elif triggered:
            # 回撤修复到 resume 线以下才算恢复,避免在阈值边上来回触发
            if dd <= max_pct * max(0.0, min(1.0, resume_ratio)):
                triggered = False
                st.update({"triggered": False, "reason": "回撤已修复,恢复交易"})

        st["peak"] = peak
        self._write(st)
        out["triggered"] = triggered

        if triggered:
            if action == "liquidate":
                out["liquidate"] = True
                out["block_buys"] = True
                out["message"] = f"回撤 {dd:.2%} ≥ {max_pct:.2%}:清仓"
            elif action == "halve":
                out["scale"] = 0.5
                out["message"] = f"回撤 {dd:.2%} ≥ {max_pct:.2%}:新仓预算减半"
            else:
                out["block_buys"] = True
                out["message"] = f"回撤 {dd:.2%} ≥ {max_pct:.2%}:停止开新仓"
        elif st.get("reason"):
            out["message"] = st["reason"]
        return out


class AdaptiveSizer:
    """动态仓位系数:按最近 N 次调仓的权益变化率给系数。"""

    MIN_SAMPLES = 4          # 少于这么多次不给结论
    STEP = 0.15              # 系数变化至少这么大才动(滞回)
    MIN_GAP = 2              # 两次调整之间至少隔几次调仓
    FLAT_EPS = 0.0002        # 权益变化小于 2bp 视为「没动」,不算盈亏

    def __init__(self, strategy: str = "", account: str = "default"):
        self.strategy = strategy or "default"
        self.account = account or "default"

    def _key(self) -> str:
        return f"{self.account}|{self.strategy}"

    def state(self) -> dict:
        return _load(ADAPT_KEY).get(self._key()) or {
            "samples": [], "multiplier": 1.0, "updated": 0.0, "since": 0,
            "reason": "样本不足,先用 1.0"}

    def _write(self, st: dict) -> None:
        all_ = _load(ADAPT_KEY)
        all_[self._key()] = st
        _save(ADAPT_KEY, all_)

    def record(self, ret: float, window: int = 10) -> dict:
        """记一次调仓后的权益变化率,并按需要调整系数。返回新状态。"""
        st = self.state()
        samples = list(st.get("samples") or [])
        samples.append(round(float(ret or 0.0), 6))
        samples = samples[-max(2, int(window)):]
        st["samples"] = samples
        st["since"] = int(st.get("since") or 0) + 1

        new_mult, reason = self._decide(samples, float(st.get("multiplier") or 1.0))
        cur = float(st.get("multiplier") or 1.0)
        # 回中性(1.0)不受滞回限制:证据不足时不该继续保留之前的加成。
        # 否则 1.10→1.00 只差 0.10 < STEP 0.15,永远回不去,加成会一直挂着。
        neutral = abs(new_mult - 1.0) < 1e-9 and abs(cur - 1.0) > 1e-9
        if neutral or (st["since"] >= self.MIN_GAP
                       and abs(new_mult - cur) >= self.STEP - 1e-9):
            st["multiplier"] = new_mult
            st["since"] = 0
            st["updated"] = time.time()
            st["reason"] = reason
        else:
            # 说明文字每次都要刷新:它描述的是「现在怎么看」,不是「上次改成了什么」。
            # 只在调档时写会让提示卡在「样本 1 次」这种过期措辞上。
            st["reason"] = (reason if abs(new_mult - cur) < 1e-9
                            else f"{reason};当前仍为 {cur:.2f},待下次调整")
        self._write(st)
        return st

    def _decide(self, samples: list[float], cur: float) -> tuple[float, str]:
        n = len(samples)
        if n < self.MIN_SAMPLES:
            return 1.0, f"样本 {n} 次,不足 {self.MIN_SAMPLES} 次,系数保持 1.0"
        mean = sum(samples) / n
        # 权益几乎没动(空仓、没成交、当天无行情)不能算亏损。只看真正动过的样本,
        # 否则「一直没交易」会被当成「一直输」,系数被莫名压到 0.6。
        moved = [x for x in samples if abs(x) > self.FLAT_EPS]
        if len(moved) < self.MIN_SAMPLES:
            return 1.0, (f"近 {n} 次里只有 {len(moved)} 次真正有盈亏,"
                         f"不足 {self.MIN_SAMPLES} 次,系数保持 1.0")
        win = sum(1 for x in moved if x > 0) / len(moved)
        tag = f"近 {n} 次均值 {mean:+.2%}、胜率 {win:.0%}"
        if mean >= 0.005 and win >= 0.55:
            return 1.25, f"{tag}:加到 1.25"
        if mean > 0 and win > 0.5:
            return 1.10, f"{tag}:微调到 1.10"
        if mean <= -0.005 or win <= 0.35:
            return 0.60, f"{tag}:降到 0.60"
        return 0.85, f"{tag}:降到 0.85"

    def summary(self) -> dict:
        st = self.state()
        s = list(st.get("samples") or [])
        moved = [x for x in s if abs(x) > self.FLAT_EPS]
        return {"multiplier": float(st.get("multiplier") or 1.0),
                "n": len(s),
                "n_moved": len(moved),
                "mean": (sum(s) / len(s)) if s else 0.0,
                "win": (sum(1 for x in moved if x > 0) / len(moved)) if moved else 0.0,
                "reason": st.get("reason") or "",
                "updated": float(st.get("updated") or 0)}


# ---------------------------------------------------------------- 权益记录
LAST_EQ_KEY = "autotrade_last_equity"


def last_equity(account: str = "default") -> float:
    """上一次调仓时的权益,用于算这一次的权益变化率。"""
    return float(_load(LAST_EQ_KEY).get(account or "default") or 0.0)


def set_last_equity(equity: float, account: str = "default") -> None:
    all_ = _load(LAST_EQ_KEY)
    all_[account or "default"] = float(equity or 0.0)
    _save(LAST_EQ_KEY, all_)
