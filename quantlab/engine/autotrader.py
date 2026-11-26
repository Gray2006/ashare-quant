# -*- coding: utf-8 -*-
"""自动交易执行器:策略 → 目标持仓 → 增量下单(只走模拟盘)。

这是「让 AI/程序直接控制模拟盘」的执行层:
- 决策基于**日线收盘**数据,与回测同一套因子和同一套规则,避免回测实盘不一致
- 下单前逐笔过 RiskEngine(整手、金额、仓位、偏离、涨跌停、T+1)
- 目标持仓与当前持仓求差,只做必要调整;卖出在前(先腾出现金,再买)
- 每次运行留一条记录(数据库 signals 表),可追溯

注意:默认 dry-run。真跑要先在 config 里把 autotrade.enabled 打开。
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

from ..broker.risk import RiskEngine
from ..config import cfg
from ..data.store import store
from ..utils import normalize_code
from . import rules as rules_mod
from . import exchange
from . import strategy as st_mod
from . import universe


def _in_trading_hours(now: dt.datetime | None = None) -> bool:
    """连续竞价时段(9:30-11:30, 13:00-14:57)。

    集合竞价(9:15-9:25, 14:57-15:00)和盘后固定价格(15:05-15:30)都不算 ——
    模拟盘按现价撮合,这些时段下单要么挂着不成交,要么按错误价格成交。
    """
    from ..utils import is_trading_time
    return is_trading_time(now)


def build_strategy(name: str, **params):
    """内置策略名,或 "rule:自定义名"。"""
    if name.startswith("rule:"):
        return rules_mod.build_rule(name.split(":", 1)[1], **params)
    return st_mod.build(name, **params)


@dataclass
class Plan:
    """一次调仓的计划与结果。"""
    strategy: str = ""
    ts: float = field(default_factory=time.time)
    target: list[tuple[str, int, str]] = field(default_factory=list)   # (code, qty, name)
    sells: list[dict] = field(default_factory=list)
    buys: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    dry_run: bool = True
    message: str = ""
    guard: dict = field(default_factory=dict)        # 回撤闸决策(含 dd/peak/触发)
    adaptive: dict = field(default_factory=dict)     # 动态系数(含本轮变化率)
    budget_scale: float = 1.0                        # 本轮预算系数
    auto: dict = field(default_factory=dict)         # 全自动模式算出的参数包
    capital: dict = field(default_factory=dict)      # 按现有资金算的方案

    def summary(self) -> str:
        parts = [f"策略 {self.strategy}"]
        if self.target:
            parts.append("目标 " + "、".join(f"{n}({c})" for c, _q, n in self.target[:8]))
        parts.append(f"卖 {len(self.sells)} 买 {len(self.buys)}")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)}")
        if self.guard:
            dd = float(self.guard.get("dd") or 0)
            if self.guard.get("triggered"):
                parts.append(f"回撤 {dd:.2%} 已触发")
            elif dd > 0:
                parts.append(f"回撤 {dd:.2%}")
        if self.adaptive and self.adaptive.get("multiplier") is not None:
            m = float(self.adaptive.get("multiplier") or 1.0)
            if abs(m - 1.0) > 1e-9:
                parts.append(f"仓位系数 {m:.2f}")
        if self.dry_run:
            parts.append("【演练,未下单】")
        return " · ".join(parts)


def apply_learning(tuned: dict, per_pct: float, sl: float, tp: float,
                   note: str = "") -> tuple[float, float, float, str]:
    """把 ParamTuner 的结果落到本轮参数上(纯函数,便于测试)。

    strategy_halt 时单票仓位归零 = 只平不新开;止损止盈保持原值。
    """
    tuned = tuned or {}
    params = tuned.get("params") or {}
    if tuned.get("changed"):
        per_pct = float(params.get("per_position_pct", per_pct))
        sl = float(params.get("stop_loss", sl))
        tp = float(params.get("take_profit", tp))
        note = "学习:" + str(tuned.get("reason") or "")
    if params.get("strategy_halt"):
        per_pct = 0.0
        note = ((note + " · ") if note else "") + "策略暂停:新开仓预算归零,止损止盈照常"
    return float(per_pct), float(sl), float(tp), note


class AutoTrader:
    """把策略接到模拟盘上。"""

    def __init__(self, gateway, strategy_name: str = "momentum20",
                 pool: str = "hs300", n_stocks: int = 60,
                 n_hold: int = 5, per_position_pct: float = 0.18,
                 days: int = 120, dry_run: bool = True, config=None,
                 max_orders_per_day: int | None = None,
                 codes: list[str] | None = None,
                 stop_loss: float = 0.0, take_profit: float = 0.0,
                 max_drawdown: float | None = None,
                 dd_action: str | None = None,
                 dd_resume: float | None = None,
                 adaptive: bool | None = None,
                 adaptive_window: int | None = None,
                 auto_mode: bool | None = None,
                 judge: bool | None = None,
                 exec_mode: str = "none",
                 weight_mode: str = "equal",
                 cost_avg: bool | None = None,
                 cost_avg_band: float | None = None,
                 cost_avg_take: float | None = None,
                 cost_avg_ratio: float | None = None,
                 account_name: str = ""):
        self.gw = gateway
        self.cfg = config or cfg()
        self.strategy_name = strategy_name
        self.pool = pool
        self.n_stocks = n_stocks
        self.n_hold = n_hold
        self.per_position_pct = per_position_pct
        self.days = days
        self.dry_run = dry_run
        if max_orders_per_day is None:
            max_orders_per_day = int(self.cfg.get("autotrade.max_orders_per_day", 0) or 0)
        self.max_orders_per_day = int(max_orders_per_day or 0)   # <=0 不限
        # 自定义股票池:给了代码就不再走 universe,直接盯这几只
        self.codes = [normalize_code(c) for c in (codes or []) if c]
        self.stop_loss = float(stop_loss or 0)      # 浮亏超过该比例卖出,0=关
        self.take_profit = float(take_profit or 0)  # 浮盈超过该比例卖出,0=关
        self.risk = RiskEngine(self.cfg)
        # 综合评判:策略选出的票再过一道市场/资金/走势/估值。
        # 默认开,config 里 autotrade.judge_enabled=false 可关。
        if judge is None:
            judge = bool(self.cfg.get("autotrade.judge_enabled", True))
        self.judge = bool(judge)
        # 拆单方式:none / twap / vwap。模拟盘也拆,记录多笔。
        self.exec_mode = str(exec_mode or "none").lower()
        # 组合权重:equal / risk_parity / mean_variance / max_div
        self.weight_mode = str(weight_mode or "equal").lower()

        # ---- 成本摊薄(做T):不是独立策略,是持仓上顺手压成本的动作
        # 只在有底仓的票上做;偏离持仓成本到 band 才高抛,回落到 take 再买回。
        # 不做「越跌越买」的低吸加仓 —— 那是摊薄陷阱,不是降成本。
        if cost_avg is None:
            cost_avg = bool(self.cfg.get("autotrade.cost_avg_enabled", False))
        self.cost_avg = bool(cost_avg)
        if cost_avg_band is None:
            cost_avg_band = float(self.cfg.get("autotrade.cost_avg_band_pct", 5.0) or 5.0) / 100.0
        if cost_avg_take is None:
            cost_avg_take = float(self.cfg.get("autotrade.cost_avg_take_pct", 2.0) or 2.0) / 100.0
        if cost_avg_ratio is None:
            cost_avg_ratio = float(self.cfg.get("autotrade.cost_avg_ratio", 0.30) or 0.30)
        self.cost_avg_band = max(0.01, float(cost_avg_band))
        self.cost_avg_take = max(0.005, float(cost_avg_take))
        self.cost_avg_ratio = min(0.8, max(0.1, float(cost_avg_ratio)))
        # 已高抛、等回落的腿:{code: {sell_px, qty, day}}
        self._t_pending: dict[str, dict] = {}

        # ---- 回撤闸 + 动态仓位系数(都为 0/关时不参与任何计算)
        if max_drawdown is None:
            max_drawdown = float(self.cfg.get("autotrade.max_drawdown_pct", 0.0) or 0.0)
        if dd_action is None:
            dd_action = str(self.cfg.get("autotrade.dd_action", "stop_new") or "stop_new")
        if dd_resume is None:
            dd_resume = float(self.cfg.get("autotrade.dd_resume_ratio", 0.5) or 0.5)
        if adaptive is None:
            # 默认值从 DEFAULTS 取:硬编码 False 会让「config 里默认 True」
            # 对老配置完全无效,而自适应仓位恰恰是震荡市的保护(2026-11-26 修)
            from ..config import DEFAULTS as _DEF
            _ad_def = bool((_DEF.get("autotrade") or {}).get(
                "adaptive_enabled", True))
            adaptive = bool(self.cfg.get("autotrade.adaptive_enabled", _ad_def))
        if adaptive_window is None:
            adaptive_window = int(self.cfg.get("autotrade.adaptive_window", 10) or 10)
        self.max_drawdown = max(0.0, float(max_drawdown or 0))
        self.dd_action = dd_action if dd_action in ("stop_new", "halve", "liquidate") \
            else "stop_new"
        self.dd_resume = max(0.0, min(1.0, float(dd_resume)))
        self.adaptive = bool(adaptive)
        self.adaptive_window = max(2, int(adaptive_window))
        self.account_name = (account_name or
                             str(self.cfg.get("autotrade.account", "default") or "default"))
        from .adaptive import AdaptiveSizer, DrawdownGuard
        self.guard = DrawdownGuard(self.account_name)
        self.sizer = AdaptiveSizer(self.strategy_name, self.account_name)
        # 全自动:仓位/止盈止损/回撤上限都由数据算(见 engine/autopilot.py)
        if auto_mode is None:
            auto_mode = bool(self.cfg.get("autotrade.auto_mode", False))
        self.auto_mode = bool(auto_mode)
        # 按现有资金自动定方案:买几只、每只多少、首笔下多少
        self.capital_plan_on = bool(self.cfg.get("autotrade.capital_plan", True))
        self.cash_floor_pct = float(self.cfg.get("autotrade.cash_floor_pct", 0.10) or 0)
        from .autopilot import AutoPilot
        self.pilot = AutoPilot(self.account_name, self.cfg, strategy=self.strategy_name)

    # ------------------------------------------------------------ 决策
    def pick(self) -> tuple[list[tuple[str, str]], str]:
        """用最新的收盘数据选股。返回 [(code, name), ...] 与说明。"""
        if self.codes:
            return [(c, "") for c in self.codes], f"自定义池 {len(self.codes)} 只"
        rows, src = universe.build(self.pool, limit=self.n_stocks)
        if not rows:
            return [], "股票池为空(数据源限流?)"
        names = {normalize_code(r["code"]): (r.get("name") or "") for r in rows}
        codes = list(names)[:self.n_stocks]
        # 资金量硬过滤:一手买不起 / 成交额撑不住单票预算 / ST / 次新 / 市值过小,
        # 先剔掉再算因子 —— 算完再剔是白算,还容易被这些票的高波动吸进前十。
        if bool(self.cfg.get("autotrade.capital_filter", True)):
            try:
                kept = self._capital_ok(codes, rows)
            except Exception:  # noqa: BLE001
                kept = codes
            if not kept:
                return [], "资金量过滤后没有可选标的"
            codes = kept

        from .indicators import bars_to_df
        from ..data.kline import get_kline
        data = {}
        for c in codes:
            try:
                bars, _ = get_kline(c, 101, count=self.days + 70)
                if bars and len(bars) >= 60:
                    data[c] = bars_to_df(bars)
            except Exception:
                continue
        if not data:
            return [], "没有可用日线"

        # 对齐末根日期:各只票的日线末根可能差好几天(缓存陈旧/某源滞后),
        # 拼成面板后最后一列只有少数几只非空,rank 出来就只剩那几只,
        # 选股直接退化成一只。
        #
        # 基准不能用面板里的 max —— 盘中某些源会吐出「当天」那根还没走完的
        # 日线,别的票都是上一交易日,一用 max 就把多数票当滞后剔掉(实测
        # 60 只剔掉 13 只)。基准要用「此刻应该能拿到的最新交易日」。
        def _last_day(df) -> str:
            try:
                return str(df["datetime"].iloc[-1])[:10]
            except Exception:
                return ""

        from ..data.kline import _last_trade_day
        base = _last_trade_day()
        days = {c: _last_day(df) for c, df in data.items()}
        # 比基准旧的算滞后;比基准新的(盘中当日半根)保留 —— 它至少是最新的
        stale = [c for c, d in days.items() if d and d < base]
        if stale:
            for c in stale:
                data.pop(c, None)
            note_align = f" · 剔除 {len(stale)} 只日线滞后"
        else:
            note_align = ""
        if not data:
            return [], "没有可用日线"

        st = build_strategy(self.strategy_name, n_long=self.n_hold)
        st.params["n_long"] = self.n_hold
        try:
            scores = st.factor_scores(data)
        except Exception as e:  # noqa: BLE001
            return [], f"因子计算失败:{e}"
        if scores is None or scores.empty:
            return [], "因子矩阵为空"

        # 用策略自己的 select 逻辑,和回测完全一致 —— 横截面策略有滞后带,
        # 规则策略是逐日状态机,不能在这里用同一套 rank 阈值硬套。
        try:
            sel = st.select(scores)
            if sel is not None and not sel.empty:
                last_sel = sel.iloc[-1]
                keep = list(last_sel[last_sel == 1].index)[:self.n_hold]
                if keep:
                    picks = [(c, names.get(c, "")) for c in keep]
                    return picks, (f"来自 {src} · {len(data)} 只有效"
                                   + note_align)
        except Exception:  # noqa: BLE001
            pass
        # 回退:直接取分数最高的
        last = scores.dropna(how="all").iloc[-1].dropna()
        if last.empty:
            return [], "最新一天无有效因子"
        top = last.sort_values(ascending=False).head(self.n_hold)
        picks = [(c, names.get(c, "")) for c in top.index]
        return picks, f"来自 {src} · {len(data)} 只有效" + note_align

    def _capital_ok(self, codes: list[str], rows: list[dict]) -> list[str]:
        """按当前权益做资金量硬过滤,返回还买得起的代码。

        参数从 config 读,默认值保守:单票预算不超过日均成交额的 5%
        —— 超过这个量级,回测里的成交价根本拿不到。
        过滤后一只不剩时回退到原列表(不能因为池子里全是大票就把选股清空)。
        """
        from . import screener as sc

        try:
            acct = self.gw.account()
            equity = float(acct.get("total") or acct.get("cash") or 0)
        except Exception:  # noqa: BLE001
            equity = 0.0
        if equity <= 0:
            return codes
        by_code = {normalize_code(r.get("code") or ""): r for r in rows}
        frame = []
        for c in codes:
            r = by_code.get(c) or {}
            frame.append({
                "code": c, "name": r.get("name") or "",
                "price": r.get("price"), "amount": r.get("amount"),
                "market_cap": r.get("market_cap") or r.get("float_cap"),
                "list_days": r.get("list_days"),
            })
        import pandas as pd

        df = pd.DataFrame(frame).set_index("code")
        why = sc.capital_filter(
            df, equity, n_hold=max(int(self.n_hold), 1),
            part=float(self.cfg.get("autotrade.capacity_pct", 0.05) or 0.05),
            min_cap=float(self.cfg.get("autotrade.min_market_cap", 5e9) or 5e9),
            min_list_days=int(self.cfg.get("autotrade.min_list_days", 250) or 250))
        keep = [c for c in codes if not (why.get(c) or "")]
        return keep or codes

    # ------------------------------------------------------------ 执行
    def _min_hold(self) -> int:
        """策略的最短持有天数。读不到就用 3 —— 跟 backtest 的默认值一致。

        auto_mode 下实盘和回测必须是同一套换手约束,否则胜率没法比。
        """
        try:
            from ..config import cfg as _cfg
            c = _cfg()
            v = c.get("autotrade.min_hold_days", None)
            if v is not None:
                return max(0, int(v))
        except Exception:  # noqa: BLE001
            pass
        try:
            from .strategy import REGISTRY
            cls = REGISTRY.get(self.strategy)
            if cls is not None:
                p = cls.default_params() or {}
                return max(0, int(p.get("min_hold", 3) or 0))
        except Exception:  # noqa: BLE001
            pass
        return 3

    def _sl(self) -> bool:
        """私有学习引擎的惰性初始化。不改 __init__ 签名,免得动到调用方。"""
        if not hasattr(self, "_sl_ready"):
            from . import selflearn as sl
            self._sl_on = bool(self.cfg.get("selflearn.enabled", False))
            self._sl_planner = sl.TranchePlanner(self.account_name)
            self._sl_log = sl.DecisionLog(self.account_name, self.strategy_name)
            self._sl_tuner = sl.ParamTuner(self.account_name, self.strategy_name)
            # 单笔金额低于这个就不拆(最低佣金保护),0 = 关
            self._sl_min_amount = float(
                self.cfg.get("selflearn.min_amount", 5000) or 0)
            self._sl_ready = True
        return self._sl_on

    def _maybe_report(self) -> None:
        """刷新当日学习报告。默认 5 分钟一次 —— 报告是给人看的,
        每分钟重算一遍(要扫 5000 条委托)纯属浪费。"""
        try:
            import time as _t

            from ..data.store import store as _store
            from . import selflearn as sl

            now = _t.time()
            gap = float(self.cfg.get("selflearn.report_interval_s", 300) or 300)
            last = float(_store().get_account("sl_report_ts", 0) or 0)
            if now - last < gap:
                return
            # 顺手回填到期的预测,几秒的事,让学习链不断
            try:
                from . import predict_learn as pl
                pl.review_pending(self.account_name, limit=50)
            except Exception:  # noqa: BLE001
                pass
            # 分批状态兜底清理:过期或已完成的,别让表一直涨
            try:
                self._sl_planner.prune(7)
            except Exception:  # noqa: BLE001
                pass
            rep = sl.daily_report(self.account_name, self.strategy_name)
            sl.save_report(rep)
            _store().set_account("sl_report_ts", now)
        except Exception:  # noqa: BLE001
            pass

    def _weights(self, picks: list) -> dict[str, float]:
        """按权重方案算目标权重。数据不足返回空(回退等权)。"""
        try:
            import pandas as pd
            from ..data.kline import get_kline
            from . import portfolio as pf
        except Exception:  # noqa: BLE001
            return {}
        rets = {}
        for code, _ in picks[:20]:
            try:
                bars, _ = get_kline(code, 101, count=120)
                if bars and len(bars) > 30:
                    s = pd.Series([float(b.get("close") or 0) for b in bars])
                    rets[code] = s.pct_change().replace(
                        [float("inf"), float("-inf")], 0).dropna()
            except Exception:  # noqa: BLE001
                continue
        if len(rets) < 2:
            return {}
        df = pd.DataFrame(rets)
        try:
            w = {}
            if self.weight_mode == "risk_parity":
                w = pf.risk_parity(df)
            elif self.weight_mode == "mean_variance":
                w = pf.mean_variance(df)
            elif self.weight_mode == "max_div":
                w = pf.max_diversification(df)
            if w:
                cap = float(self.cfg.get("autotrade.max_position_pct", 0.25) or 0.25)
                w = pf.cap_weights(w, max_single=cap)
            return w
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _cost_average(self, plan: Plan, positions: dict, quotes: dict,
                      acct: dict) -> set[str]:
        """成本摊薄:在已有底仓上做价差,赚到的差价等于把持仓成本压下来。

        不规定先买还是先卖,哪个方向先到阈值就走哪个:
          · 浮盈到 band → 先卖(高抛),回落到 take 再买回
          · 浮亏到 band → 先买(低吸),反弹到 take 再卖出(卖的是原底仓,T+1 允许)
        也不限轮次:一轮平掉后继续按同样规则判断,能走几轮走几轮。
        唯一的硬约束是 T+1 —— 当天买进的份额当天不能卖,所以低吸那一轮的
        卖出只能用原有底仓,可卖数量不够时这一轮就做不成。

        只对已持仓的票做,空仓不参与;没到阈值就不动,不动才是常态。
        """
        if not self.cost_avg or self.cost_avg_band <= 0:
            return set()

        today = time.strftime("%Y-%m-%d")
        # 隔日的挂单作废:不跨夜等
        self._t_pending = {c: v for c, v in self._t_pending.items()
                           if v.get("day") == today}
        done: set[str] = set()
        cash = float(acct.get("cash") or 0)

        def _check(code, side, px, qty, p) -> bool:
            d = self.risk.check_order(code, side, px, qty, quote=quotes.get(code) or {},
                                      positions=list(positions.values()),
                                      daily_orders=self._daily_orders(),
                                      account=acct, live=False,
                                      max_daily_orders=(self.max_orders_per_day or None))
            if not d.ok:
                plan.skipped.append({"code": code,
                                     "reason": f"成本摊薄·{side}:{d.reason}"})
            return d.ok

        for code, p in positions.items():
            q = quotes.get(code) or {}
            px = float(q.get("price") or p.last_price or 0)
            cost = float(getattr(p, "cost", 0) or 0)
            if px <= 0 or cost <= 0 or int(p.qty or 0) <= 0:
                continue

            pend = self._t_pending.get(code)
            if pend:
                qty = int(pend["qty"])
                if pend["side"] == "sell":             # 高抛过,等回落买回
                    if px > float(pend["px"]) * (1 - self.cost_avg_take):
                        continue
                    if cash < qty * px * 1.002:
                        plan.skipped.append({"code": code,
                                             "reason": "成本摊薄:现金不够买回"})
                        continue
                    if not _check(code, "buy", px, qty, p):
                        continue
                    item = {"code": code, "name": p.name, "qty": qty, "price": px,
                            "reason": (f"成本摊薄·买回(高抛 {pend['px']:.2f}"
                                       f" → 现价 {px:.2f})")}
                    if not self.dry_run:
                        o = self.gw.place_order(code, "buy", px, qty, name=p.name)
                        item.update(order_id=o.id, status=o.status, remark=o.remark)
                    plan.buys.append(item)
                    cash -= qty * px
                else:                                   # 低吸过,等反弹卖出
                    if px < float(pend["px"]) * (1 + self.cost_avg_take):
                        continue
                    avail = exchange.round_qty(code, int(p.avail_qty or 0))
                    qty = min(qty, avail)
                    if qty <= 0:
                        plan.skipped.append({"code": code,
                                             "reason": "成本摊薄:低吸那轮"
                                                       "没有可卖的底仓(T+1)"})
                        self._t_pending.pop(code, None)
                        continue
                    if not _check(code, "sell", px, qty, p):
                        continue
                    item = {"code": code, "name": p.name, "qty": qty, "price": px,
                            "reason": (f"成本摊薄·卖出(低吸 {pend['px']:.2f}"
                                       f" → 现价 {px:.2f})")}
                    if not self.dry_run:
                        o = self.gw.place_order(code, "sell", px, qty, name=p.name)
                        item.update(order_id=o.id, status=o.status, remark=o.remark)
                    plan.sells.append(item)
                self._t_pending.pop(code, None)
                done.add(code)
                continue

            pnl = px / cost - 1
            if pnl >= self.cost_avg_band:               # 涨够 → 先卖
                qty = exchange.round_qty(
                    code, int(int(p.avail_qty or 0) * self.cost_avg_ratio))
                if qty <= 0:
                    plan.skipped.append({"code": code,
                                         "reason": "成本摊薄:T+1 可卖为 0"})
                    continue
                if not _check(code, "sell", px, qty, p):
                    continue
                item = {"code": code, "name": p.name, "qty": qty, "price": px,
                        "reason": (f"成本摊薄·高抛(成本 {cost:.2f} → 现价 {px:.2f},"
                                   f"浮盈 {pnl:+.1%})")}
                if not self.dry_run:
                    o = self.gw.place_order(code, "sell", px, qty, name=p.name)
                    item.update(order_id=o.id, status=o.status, remark=o.remark)
                plan.sells.append(item)
                self._t_pending[code] = {"side": "sell", "px": px, "qty": qty,
                                         "day": today}
                done.add(code)
            elif pnl <= -self.cost_avg_band:            # 跌够 → 先买
                budget = (float(acct.get("total") or 0)
                          * self.per_position_pct * self.cost_avg_ratio)
                qty = exchange.round_qty(code, int(budget // px)) if budget > 0 else 0
                if qty <= 0:
                    plan.skipped.append({"code": code,
                                         "reason": "成本摊薄:预算不够低吸一手"})
                    continue
                if cash < qty * px * 1.002:
                    plan.skipped.append({"code": code,
                                         "reason": "成本摊薄:现金不够低吸"})
                    continue
                if not _check(code, "buy", px, qty, p):
                    continue
                item = {"code": code, "name": p.name, "qty": qty, "price": px,
                        "reason": (f"成本摊薄·低吸(成本 {cost:.2f} → 现价 {px:.2f},"
                                   f"浮亏 {pnl:+.1%})")}
                if not self.dry_run:
                    o = self.gw.place_order(code, "buy", px, qty, name=p.name)
                    item.update(order_id=o.id, status=o.status, remark=o.remark)
                plan.buys.append(item)
                cash -= qty * px
                self._t_pending[code] = {"side": "buy", "px": px, "qty": qty,
                                         "day": today}
                done.add(code)
        return done

    def run_once(self, force: bool = False) -> Plan:
        plan = Plan(strategy=self.strategy_name, dry_run=self.dry_run)
        # 非交易时间不下单:以前 15:00 后 pilot 还在窗口里跑,会往模拟盘
        # 塞收盘后的单;手动「立即执行」也可能在半夜触发。dry_run 只算不下单,
        # 不受影响。
        if not self.dry_run and not _in_trading_hours():
            plan.message = "非交易时间,不下单"
            return plan
        picks, note = self.pick()
        # 选不出票时**不能直接返回**:止损止盈是保本动作,跟选股成功与否无关。
        # 以前这里 `if not picks: return plan` —— 池子取数一失败(限流/非交易时段),
        # 满仓的浮亏就再也不会被卖出,这才是"自动交易不会卖出"的主因。
        picking = bool(picks)
        if not picks:
            note = (note or "选不出票") + " · 本轮只执行止损止盈,不动目标仓位"
            picks = []

        acct = self.gw.account()
        equity = float(acct.get("total") or acct.get("cash") or 0)

        # 全自动:先让 pilot 按波动/回撤把参数算出来,再走同一套执行逻辑。
        # 它只改参数,不改策略信号 —— 决策链和手动模式完全一致,便于对账。
        auto = None
        per_pct = float(self.per_position_pct)
        dd_max, dd_action = self.max_drawdown, self.dd_action
        if self.auto_mode:
            try:
                from ..data.kline import get_kline
                cache: dict[str, list] = {}

                def bars_of(code: str) -> list:
                    if code not in cache:
                        try:
                            bars, _ = get_kline(code, 101, count=self.days)
                            cache[code] = bars or []
                        except Exception:
                            cache[code] = []
                    return cache[code]

                auto = self.pilot.decide(equity, self.gw.positions(), bars_of,
                                         base_pct=self.per_position_pct)
                per_pct = float(auto["per_position_pct"])
                dd_max = float(auto["max_drawdown"])
                dd_action = str(auto["dd_action"])
            except Exception as e:  # noqa: BLE001
                note = (note + " · " if note else "") + f"全自动参数回退:{e}"
                auto = None

        # 私有学习:按历史表现调参数(有滞回,不会每轮都动)
        sl_cfg, tp_cfg = self.stop_loss, self.take_profit
        # 学习调整前的基准。auto_mode 下 ATR 定止损止盈的绝对值,
        # 学习调出的比例要乘到 ATR 结果上,不能直接覆盖(以前就是直接覆盖,
        # 学习调的止损止盈白调)。
        sl_base, tp_base = sl_cfg, tp_cfg
        sl_note = ""
        if self._sl():
            try:
                tuned = self._sl_tuner.tune({"per_position_pct": per_pct,
                                             "stop_loss": sl_cfg,
                                             "take_profit": tp_cfg})
                per_pct, sl_cfg, tp_cfg, sl_note = apply_learning(
                    tuned, per_pct, sl_cfg, tp_cfg, sl_note)
            except Exception as e:  # noqa: BLE001
                sl_note = f"学习参数回退:{e}"

        # 资金守卫:先判安全,再谈选股。清仓时直接把目标清空,
        # 后面的「不在目标里的持仓全卖」会自然把仓位平掉,不用另写一套。
        g = self.guard.evaluate(equity, dd_max, dd_action, self.dd_resume)
        mult = self.sizer.summary()["multiplier"] if self.adaptive else 1.0
        budget_scale = float(g.get("scale") or 1.0) * (mult if self.adaptive else 1.0)
        if g.get("liquidate"):
            picks = []
            note = (note + " · " if note else "") + g["message"]
        elif g.get("message"):
            note = (note + " · " if note else "") + g["message"]
        if auto:
            note = (note + " · " if note else "") + "全自动:" + ";".join(auto["notes"][:2])

        # 单票预算 = min(权益×单票比例, 单笔金额上限) × 守卫系数。
        # 不取 min 的话,每笔都会撞风控的"单笔超限"而全部失败。
        max_order = float(self.cfg.get("risk.max_order_amount", 50000) or 50000)

        # 取现价(下单要用现价,不能用收盘价)
        from ..data import em, sina
        want = [c for c, _ in picks] + [p.code for p in self.gw.positions()]
        quotes = {}
        try:
            quotes = em.quotes(want) or {}
        except Exception:
            try:
                quotes = sina.quotes(want) or {}
            except Exception:
                quotes = {}

        # 把现价标到持仓上。少了这一步,position.market_value 会用成本价,
        # 权益快照记下来的就是「成本价市值」—— 持仓涨跌完全没进总账,
        # 当日盈亏永远显示 0,而用户眼睛看的是账户市值在动。
        if quotes and hasattr(self.gw, "set_mark_prices"):
            try:
                self.gw.set_mark_prices(quotes)
            except Exception:  # noqa: BLE001
                pass

        # 综合评判:策略选出的票再过一道市场/资金/走势/估值。
        # 拦下的票不进目标集;止损止盈照常执行(保本动作不受影响)。
        #
        # 大盘弱(mscore<=-0.5)时不逐只淘汰,而是「整体减半仓」——
        # 以前大盘分算进每只票的 score,一跌破 MA20 就全票击穿 -0.5 阈值,
        # 结果 5 只被拦 4 只,报告却写成「这几只票差」,调参永远调不对。
        if picks and self.judge:
            try:
                from . import judge as jd
                m = jd.market_state()
                mscore = float(m.get("score") or 0) if m.get("ok") else 0.0
                keep, dropped = jd.filter_picks(picks, quotes)
                if dropped:
                    note = (note + " · " if note else "") + \
                        f"综合评判拦下 {len(dropped)} 只"
                    for d in dropped[:3]:
                        plan.skipped.append({
                            "code": d["code"],
                            "reason": "综合评判:" + ";".join(d["reasons"][:2])})
                picks = keep
                if mscore <= -0.5 and picks:
                    # 大盘走弱:减半仓,但仍保留信号最强的名次
                    half = max(1, len(picks) // 2)
                    if half < len(picks):
                        note = (note + " · " if note else "") + \
                            f"大盘走弱减半仓({len(picks)}→{half})"
                        for c, _n in picks[half:]:
                            plan.skipped.append({
                                "code": c, "reason": "大盘走弱:本轮减半仓"})
                        picks = picks[:half]
                picking = bool(picks)
            except Exception as e:  # noqa: BLE001
                note = (note + " · " if note else "") + f"综合评判回退:{e}"

        # 组合层面风控:单票仓位、行业集中度。只提示,不拦单 ——
        # 拦单交给单笔风控,组合层是给用户看的预警。
        try:
            warns = self.risk.check_portfolio(self.gw.positions(), acct)
            for w in warns:
                note = (note + " · " if note else "") + w["msg"]
        except Exception:  # noqa: BLE001
            pass

        # 私有学习:用现价回填之前的决策,让参数迭代有样本可用
        if self._sl():
            try:
                self._sl_log.backfill_by_quotes(quotes)
            except Exception:  # noqa: BLE001
                pass

        # 按现有资金定方案:先算清楚买得起几只,再决定目标集。
        # 不这么做的话,单票预算买不到一手时,整轮会全部撞「买不到一手」空转。
        cap: dict = {}
        if self.capital_plan_on:
            try:
                from . import capital_plan as cp
                px_list = [float((quotes.get(c) or {}).get("price") or 0)
                           for c, _ in picks]
                px_list = [x for x in px_list if x > 0]
                min_lot = min(px_list) * 100 if px_list else 0.0
                cap = cp.plan(equity, float(acct.get("cash") or 0), per_pct,
                              max_order, self.n_hold,
                              cash_floor_pct=self.cash_floor_pct,
                              min_lot_cost=min_lot)
                if cap["max_positions"] < len(picks):
                    picks = picks[:cap["max_positions"]]
                # 单票预算以资金方案为准:它已经考虑了一手成本和现金底线,
                # 比「权益×比例」更贴近实际能买到的量。
                note = (note + " · " if note else "") + cp.one_line(cap)
            except Exception as e:  # noqa: BLE001
                note = (note + " · " if note else "") + f"资金方案回退:{e}"

        target_codes = {c for c, _ in picks}
        plan.target = [(c, 0, n) for c, n in picks]

        positions = {p.code: p for p in self.gw.positions()}
        sold_in_sl: set[str] = set()

        # 0) 止损止盈:先于策略信号处理,保住本金优先。
        #    全自动模式下每只票按自己的 ATR 单独定阈值(高波动的给宽一点),
        #    手动模式沿用配置里的固定百分比。
        # 先把「昨天该止损但卖不掉」的票处理掉:只要还亏着就卖,
        # 不等它重新跌破止损线(反弹回去就永远不卖了)
        try:
            from . import pending_stop
            pend = pending_stop.pending()
        except Exception:  # noqa: BLE001
            pend = {}
        for code in list(pend):
            p = positions.get(code)
            if p is None:
                pending_stop.clear(code)       # 已经没持仓了
                continue
            q = quotes.get(code) or {}
            px = float(q.get("price") or p.last_price or 0)
            cost = float(getattr(p, "cost", 0) or 0)
            if px <= 0 or cost <= 0:
                continue
            pnl = px / cost - 1
            if pnl >= 0:                       # 已经回本,不用强制卖
                continue
            qty = exchange.round_qty(code, int(p.avail_qty or 0))
            if qty <= 0:                       # 还是 T+1,继续等
                pending_stop.record(code, "T+1 仍不可卖", pnl)
                continue
            o = self.gw.place_order(code, "sell", px, qty, name=p.name) \
                if not self.dry_run else None
            item = {"code": code, "name": p.name, "side": "sell", "qty": qty,
                    "price": px, "reason": f"补执行止损(浮亏 {pnl:+.1%})"}
            if o is not None:
                item.update(order_id=o.id, status=o.status, remark=o.remark)
            plan.sells.append(item)
            pending_stop.clear(code)

        for code, p in (list(positions.items())
                        if (sl_cfg > 0 or tp_cfg > 0 or self.auto_mode)
                        else []):
            sl, tp = sl_cfg, tp_cfg
            if self.auto_mode:
                try:
                    from ..data.kline import get_kline
                    bars, _ = get_kline(code, 101, count=self.days)
                    a_sl, a_tp, _why = self.pilot.stop_take(bars or [])
                    # ATR 给基准,学习给缩放:学习把止损从 2% 调到 1.6%,
                    # 这里就把 ATR 算出的阈值乘 0.8。直接覆盖的话,
                    # auto_mode 下学习调的止损止盈永远不生效。
                    if a_sl:
                        sl = a_sl * (sl_cfg / sl_base) if sl_base > 0 else a_sl
                    if a_tp:
                        tp = a_tp * (tp_cfg / tp_base) if tp_base > 0 else a_tp
                    sl = max(0.01, min(0.30, sl))
                    tp = max(0.02, min(0.60, tp))
                except Exception:  # noqa: BLE001
                    pass
            if sl <= 0 and tp <= 0:
                continue
            q = quotes.get(code) or {}
            px = float(q.get("price") or p.last_price or 0)
            cost = float(getattr(p, "cost", 0) or 0)
            if px <= 0 or cost <= 0:
                continue
            pnl = px / cost - 1
            hit = ((sl > 0 and pnl <= -sl) or (tp > 0 and pnl >= tp))
            if not hit:
                continue
            qty = exchange.round_qty(code, int(p.avail_qty or 0))
            if qty <= 0:
                # 记下来:第二天要优先处理,否则反弹回止损线以上就永远不卖了
                try:
                    from . import pending_stop
                    pending_stop.record(code, "T+1 不可卖", pnl)
                except Exception:  # noqa: BLE001
                    pass
                plan.skipped.append({"code": code, "reason": "T+1 不可卖(止损未执行)"})
                continue
            d = self.risk.check_order(code, "sell", px, qty, quote=q,
                                      positions=list(positions.values()),
                                      daily_orders=self._daily_orders(),
                                      account=acct, live=False,
                                      max_daily_orders=(self.max_orders_per_day or None))
            if not d.ok:
                plan.skipped.append({"code": code, "reason": f"止损:{d.reason}"})
                continue
            item = {"code": code, "name": p.name, "qty": qty, "price": px,
                    "reason": ("止损" if pnl < 0 else "止盈")
                              + (f"({sl:.1%}/{tp:.1%})" if self.auto_mode else "")}
            if not self.dry_run:
                o = self.gw.place_order(code, "sell", px, qty, name=p.name)
                item.update(order_id=o.id, status=o.status)
            plan.sells.append(item)
            target_codes.discard(code)
            sold_in_sl.add(code)      # 已按止损止盈处理过的,别再重复卖一遍

        # 0.5) 成本摊薄:有底仓的票浮盈够多就高抛一部分,回落再买回。
        #      排在目标调仓之前 —— 它不改目标仓位,只是把持仓成本压下来。
        #      处理过的票本轮不再参与目标买卖,免得刚卖掉又被补回来。
        t_done = self._cost_average(plan, positions, quotes, acct)
        if t_done:
            sold_in_sl |= t_done

        # 1) 先卖:不在目标里的持仓。
        #    只在"本轮真的选出票"或"回撤闸要求清仓"时执行 —— 选股失败时
        #    目标集是空的,照这个规则会把满仓全部清掉,不能算调仓。
        # 最短持有:回测里 min_hold 强制(backtest.py),实盘原来没有 ——
        # 导致每天重算目标就换手,avg_hold 0.92 天,回测能拿 3 天。
        # 只约束「目标漂移卖出」;止损/止盈/清仓在上面已经处理完,不受影响。
        min_hold = self._min_hold()
        today = time.strftime("%Y-%m-%d")
        for code, p in (positions.items() if (picking or g.get("liquidate")) else ()):
            if code in target_codes or code in sold_in_sl or p.qty <= 0:
                continue
            if min_hold > 0 and not g.get("liquidate"):
                try:
                    opened = float(getattr(p, "opened_at", 0) or 0)
                except (TypeError, ValueError):
                    opened = 0.0
                if opened > 0:
                    held_days = (time.time() - opened) / 86400.0
                    if held_days < min_hold:
                        plan.skipped.append({
                            "code": code,
                            "reason": (f"最短持有 {min_hold} 天(已持 "
                                       f"{held_days:.1f} 天),本轮不卖")})
                        continue
            q = quotes.get(code) or {}
            px = float(q.get("price") or p.last_price or 0)
            if px <= 0:
                plan.skipped.append({"code": code, "reason": "取不到现价"})
                continue
            qty = exchange.round_qty(code, int(p.avail_qty // 100 * 100))
            if qty <= 0:
                plan.skipped.append({"code": code, "reason": "T+1 不可卖"})
                continue
            tranche_info: dict = {}
            if self._sl():
                try:
                    ti = self._sl_planner.plan_sell(code, qty, 0.5,
                                                    exchange.min_qty(code),
                                                    price=px,
                                                    min_amount=self._sl_min_amount)
                    if ti["qty"] <= 0:
                        plan.skipped.append({
                            "code": code,
                            "reason": "分批:本轮不卖" + (f"({ti['why']})" if ti.get("why") else "")})
                        continue
                    qty = int(ti["qty"])
                    tranche_info = ti
                except Exception as e:  # noqa: BLE001
                    plan.skipped.append({"code": code, "reason": f"分批回退:{e}"})
            d = self.risk.check_order(code, "sell", px, qty, quote=q,
                                      positions=list(positions.values()),
                                      daily_orders=self._daily_orders(),
                                      account=acct, live=False,
                                      max_daily_orders=(self.max_orders_per_day or None))
            if not d.ok:
                plan.skipped.append({"code": code, "reason": d.reason})
                continue
            item = {"code": code, "name": p.name, "qty": qty, "price": px}
            if not self.dry_run:
                o = self.gw.place_order(code, "sell", px, qty, name=p.name)
                item.update(order_id=o.id, status=o.status, remark=o.remark)
                if self._sl():
                    try:
                        self._sl_log.record(
                            code, "sell", qty, px, name=p.name,
                            tranche=int(tranche_info.get("tranche") or 1),
                            tranches=int(tranche_info.get("tranches") or 1),
                            strength=0.5,
                            params={"per_position_pct": per_pct,
                                    "stop_loss": sl_cfg, "take_profit": tp_cfg})
                    except Exception:  # noqa: BLE001
                        pass
            plan.sells.append(item)

        # 2) 再买:目标里没有或不足的(回撤闸要求停手时整段跳过)
        if g.get("block_buys"):
            plan.skipped.append({"code": "*", "reason": g["message"] or "回撤闸拦截"})
        cash = float(self.gw.account()["cash"])
        # 组合权重:等权用 per_pct,其他方案按历史收益分配
        weights = self._weights(picks) if self.weight_mode != "equal" else {}
        for code, name in ([] if g.get("block_buys") else picks):
            q = quotes.get(code) or {}
            px = float(q.get("price") or 0)
            if px <= 0:
                plan.skipped.append({"code": code, "reason": "取不到现价"})
                continue
            # 单票当日买入次数上限 —— 603389 两小时被买 36 次的教训
            try:
                _cap = int(self.cfg.get("autotrade.max_buys_per_code_per_day", 3) or 0)
            except Exception:  # noqa: BLE001
                _cap = 3
            if _cap > 0 and self._buys_today(code) >= _cap:
                plan.skipped.append({
                    "code": code,
                    "reason": f"当日已买 {self._buys_today(code)} 次,"
                              f"达单票上限 {_cap}"})
                continue
            held = positions.get(code)
            held_qty = held.qty if held else 0
            w = float(weights.get(code, per_pct)) if weights else per_pct
            buget_i = min(equity * w, max_order) * budget_scale
            want_qty = exchange.round_qty(code, int(buget_i // px))
            delta = want_qty - held_qty
            if delta < exchange.min_qty(code):
                plan.skipped.append({
                    "code": code, "reason": f"单票预算 {buget_i:,.0f} 买不到一手"
                                             f"({px:.2f}×{exchange.min_qty(code)})"})
                continue
            # 目标缓动:差额太小就不新开分批计划,
            # 防止「目标随权益漂移 → 反复从头分批 → 碎单连买」
            # 默认值从 DEFAULTS 取 —— 硬编码 0 会让「config 改默认值」
            # 对老配置完全无效(2026-11-26 修)
            try:
                from ..config import DEFAULTS as _DEF
                _h_def = float((_DEF.get("selflearn") or {}).get(
                    "target_hysteresis_pct") or 0.15)
                _hyst = float(self.cfg.get(
                    "selflearn.target_hysteresis_pct", _h_def) or _h_def)
                if not sl.topup_ok(want_qty, held_qty, exchange.min_qty(code), _hyst):
                    plan.skipped.append({
                        "code": code,
                        "reason": f"差额 {delta} 小于目标缓动线"
                                  f"({_hyst:.1%} of {want_qty})"})
                    continue
            except Exception:  # noqa: BLE001
                pass
            tranche_info: dict = {}
            if self._sl():
                try:
                    # 信号强度用排名近似:越靠前越强。不额外算指标,省一次取数。
                    order_codes = [c for c, _ in picks]
                    rank = order_codes.index(code) if code in order_codes else 0
                    strength = 1.0 - rank / max(1, len(order_codes))
                    ti = self._sl_planner.plan_buy(code, want_qty, held_qty,
                                                   strength, exchange.min_qty(code),
                                                   price=px,
                                                   min_amount=self._sl_min_amount)
                    if ti["qty"] <= 0:
                        plan.skipped.append({
                            "code": code,
                            "reason": "分批:本轮不买" + (f"({ti['why']})" if ti.get("why") else "")})
                        continue
                    delta = int(ti["qty"])
                    tranche_info = ti
                except Exception as e:  # noqa: BLE001
                    plan.skipped.append({"code": code, "reason": f"分批回退:{e}"})
            cost = px * delta
            if cost > cash:
                delta = exchange.round_qty(code, int(cash // px))
                if delta < exchange.min_qty(code):
                    plan.skipped.append({"code": code, "reason": "现金不足"})
                    continue
            d = self.risk.check_order(code, "buy", px, delta, quote=q,
                                      positions=list(self.gw.positions()),
                                      daily_orders=self._daily_orders(),
                                      account=self.gw.account(), live=False,
                                      max_daily_orders=(self.max_orders_per_day or None))
            if not d.ok:
                plan.skipped.append({"code": code, "reason": d.reason})
                continue
            item = {"code": code, "name": name, "qty": delta, "price": px}
            if not self.dry_run:
                parts = [delta]
                if self.exec_mode in ("twap", "vwap") and delta >= 200:
                    from . import execution as ex
                    if self.exec_mode == "twap":
                        parts = ex.twap(delta, 5)
                    else:
                        parts = ex.vwap(delta, [1, 1, 1, 1, 1])
                filled = 0
                for part in parts:
                    o = self.gw.place_order(code, "buy", px, part, name=name)
                    if o.status == "filled":
                        filled += part
                        cash -= o.filled_price * part
                item.update(status="filled" if filled == delta else "partial",
                            remark=f"拆 {len(parts)} 笔" if len(parts) > 1 else "")
                if self._sl():
                    try:
                        self._sl_log.record(
                            code, "buy", delta, px, name=name,
                            tranche=int(tranche_info.get("tranche") or 1),
                            tranches=int(tranche_info.get("tranches") or 1),
                            strength=float(tranche_info.get("strength") or 0.5),
                            params={"per_position_pct": per_pct,
                                    "stop_loss": sl_cfg, "take_profit": tp_cfg})
                    except Exception:  # noqa: BLE001
                        pass
            plan.buys.append(item)

        if sl_note:
            note = (note + " · " if note else "") + sl_note
        plan.message = note
        plan.guard = g
        plan.budget_scale = budget_scale
        plan.auto = auto or {}
        plan.capital = cap
        # 动态系数:用这一轮的权益变化率当样本(含浮盈浮亏,不只看平仓)
        try:
            from .adaptive import last_equity, set_last_equity
            prev = last_equity(self.account_name)
            now_eq = float(self.gw.account().get("total") or 0)
            if prev > 0 and now_eq > 0:
                ret = now_eq / prev - 1
                if self.adaptive:
                    st = self.sizer.record(ret, self.adaptive_window)
                    plan.adaptive = {"ret": ret,
                                     "multiplier": float(st.get("multiplier") or 1.0),
                                     "reason": st.get("reason") or ""}
                else:
                    plan.adaptive = {"ret": ret, "multiplier": 1.0,
                                     "reason": "动态调整未开启"}
            if now_eq > 0:
                set_last_equity(now_eq, self.account_name)
        except Exception:  # noqa: BLE001
            pass
        # 权益快照:当日盈亏靠它算总账,和学不学习无关,总是记
        try:
            from . import daily_review as dr
            _acct = self.gw.account()
            dr.snapshot(self.account_name,
                        equity=float(_acct.get("total") or 0),
                        cash=float(_acct.get("cash") or 0),
                        market_value=float(_acct.get("market_value") or 0))
        except Exception:  # noqa: BLE001
            pass

        if self._sl():
            self._maybe_report()
        self._log(plan)
        return plan

    def _buys_today(self, code: str) -> int:
        """这只票今天买过几次。"""
        try:
            acct = getattr(self.gw, "account_name", None)
            return store().orders_today_count(account=acct, code=code)
        except Exception:  # noqa: BLE001
            return 0

    def _daily_orders(self) -> int:
        """当日已发委托数。

        **计数和执行层上限解耦**:`max_orders_per_day=0` 只表示「执行层不限」,
        不代表「今天没下过单」。原来这里跟着返回 0,导致风控层的
        `risk.max_daily_orders` 永远看到 0 笔、永远不拦 ——
        实测 603389 一天被买 36 次就是这个原因(2026-11-26 修)。
        """
        if self.dry_run:
            return 0
        try:
            acct = getattr(self.gw, "account_name", None)
            return store().orders_today_count(account=acct)
        except Exception:  # noqa: BLE001
            return 0

    def _log(self, plan: Plan) -> None:
        try:
            import json
            store().add_signal({
                "ts": plan.ts, "source": "autotrader", "code": "*",
                "name": plan.strategy,
                "score": float(len(plan.buys) - len(plan.sells)),
                "reason": json.dumps({
                    "target": [c for c, _q, _n in plan.target],
                    "sells": plan.sells, "buys": plan.buys,
                    "skipped": plan.skipped, "dry_run": plan.dry_run,
                    "message": plan.message,
                    "guard": plan.guard, "adaptive": plan.adaptive,
                    "budget_scale": plan.budget_scale,
                }, ensure_ascii=False),
            })
        except Exception:
            pass


def should_run_now(now: dt.datetime | None = None) -> tuple[bool, str]:
    """只在收盘后跑一次(默认 15:10~15:40),避免盘中反复追单。"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False, "周末不调仓"
    t = now.hour * 60 + now.minute
    if 15 * 60 + 10 <= t <= 15 * 60 + 40:
        return True, "收盘后调仓窗口"
    return False, f"非调仓窗口({now:%H:%M}),收盘后 15:10~15:40 才自动跑"
