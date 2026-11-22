# -*- coding: utf-8 -*-
"""按历史走势自定义策略:条件表达式 → 买卖信号 → 回测/自动交易共用。

语法、函数库、求值全在 `engine/dsl.py`(表达式引擎 v2)。这里只留
「策略这一层」的东西:规则的存取、状态机(RuleStrategy)、示例。

一句话规则,例如:
    base := ma(20)
    买入:close > base and ma(5) > base and volume > vol_ma(20) * 1.5
    卖出:close < base or pnl_pct(买入, close < base) < -8

老写法(`close > ma(20) and ma(5) > ma(20)`)不必改,新引擎照样认。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import dsl
from .dsl import (COLUMNS, DslError, compile_expr, evaluate, evaluate_value,
                  required_bars, validate)
from .strategy import BaseStrategy

#: 全部函数名(给帮助窗口与自检做覆盖检查)
FUNCS = dsl.FUNCS
#: 全部列名(含通达信简写)
COLUMNS_ALL = dsl.COLUMNS_ALL


class RuleError(DslError):
    """兼容旧代码:`except RuleError` 依然能兜住表达式里的错。"""


def _raise(msg: str) -> None:
    raise RuleError(msg)


def warmup_bars(expr: str) -> int:
    """这条表达式要多少根 K 线才稳(预热长度)。"""
    return required_bars(expr)


def describe_rule(buy: str, sell: str) -> str:
    """给界面用的一句话体检:预热长度、用到的函数/列、可能的横截面依赖。"""
    parts: list[str] = []
    progs = []
    saved = 0
    for tag, expr in (("买", buy), ("卖", sell)):
        if not expr:
            continue
        try:
            prog = dsl.compile_expr(expr)
        except DslError as e:
            return f"{tag}点写不通:{str(e).splitlines()[0]}"
        progs.append(prog)
        xs = ",需要全市场一起算" if prog.needs_xs() else ""
        npar = prog.n_params()
        ptag = f"、{npar} 个参数未填" if npar else ""
        saved += prog.saved_nodes()
        parts.append(f"{tag}点预热 {prog.lookback()} 根、"
                     f"{len(prog.funcs())} 个函数{xs}{ptag}")
    if not progs:
        return "两条条件都空"
    cols = sorted(set().union(*(p.names() for p in progs)))
    tips = [s for p in progs for s in p.lint()]
    # 有买点没卖点 = 买入后永不主动出场。max_hold 管的是「最多同时持几只」,
    # 不是持有天数,这里必须点明,否则界面上很容易被读成「持够 N 天就卖」。
    if buy and not sell:
        tips.append("没有卖点:买入后会一直持有,限天数要用 "
                    "hold_bars(买点, 卖点) > N,不是 max_hold")
    head = " · ".join(parts) + f" · 用到列 {' '.join(cols) or '(无)'}"
    if saved:
        head += f" · 编译期省掉 {saved} 个节点"
    return head + ("　⚠ " + ";".join(tips) if tips else "")


# ------------------------------------------------------------------ 状态机

def _to_int(v, default: int = 0) -> int:
    """把配置里的值转成整数:字符串、None、脏数据都不要炸。"""
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_rule(d: dict | str) -> dict:
    if isinstance(d, str):
        return {"name": d, "desc": "", "buy": "", "sell": "", "max_hold": 5,
                "hold_until_sell": True}
    name = str(d.get("name") or "").strip() or "rule"
    return {
        "name": name,
        "desc": d.get("desc") or "",
        "buy": d.get("buy") or "",
        "sell": d.get("sell") or "",
        "max_hold": max(0, _to_int(d.get("max_hold"))),
        "hold_until_sell": bool(d.get("hold_until_sell", True)),
    }


class RuleStrategy(BaseStrategy):
    """逐票状态机:买点入场、卖点出场,中间不动。"""

    name = "rule"

    def __init__(self, **params):
        super().__init__(**params)
        self.rule = _as_rule(self.params.get("rule") or {})

    @classmethod
    def default_params(cls) -> dict:
        return {"rule": {}, "n_long": 0}

    def factor_scores(self, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        buy_expr = self.rule.get("buy") or ""
        sell_expr = self.rule.get("sell") or ""
        if not buy_expr:
            raise RuleError("买入条件为空")
        cols = {}
        warm = max(required_bars(buy_expr), required_bars(sell_expr) if sell_expr else 0)
        for code, df in data.items():
            if df is None or len(df) < 30:
                continue
            try:
                buy = evaluate(buy_expr, df, code, xs=data)
                sell = (evaluate(sell_expr, df, code, xs=data) if sell_expr
                        else np.zeros(len(df), bool))
            except DslError:
                continue
            score = np.where(buy, 2.0, np.where(sell, -1.0, 0.0))
            if warm and warm < len(score):
                score[:warm] = 0.0        # 预热区不投票,避免半条均线发假信号
            idx = pd.DatetimeIndex(df["datetime"])
            cols[code] = pd.Series(score, index=idx)
        if not cols:
            return pd.DataFrame()
        return pd.DataFrame(cols).sort_index()

    def select(self, scores: pd.DataFrame) -> pd.DataFrame:
        """逐日扫描,维护持仓集合(保持 `hold_until_sell` 语义)。"""
        if scores is None or scores.empty:
            return pd.DataFrame()
        max_hold = int(self.rule.get("max_hold") or 0)
        hold_until_sell = bool(self.rule.get("hold_until_sell", True))
        cols = list(scores.columns)
        vals = scores.to_numpy()
        out = np.zeros(vals.shape, dtype=int)
        held: set[int] = set()
        for i in range(len(vals)):
            row = vals[i]
            for j in range(len(cols)):
                v = row[j]
                if v >= 2.0:
                    held.add(j)
                elif v <= -1.0:
                    held.discard(j)
                elif not hold_until_sell:
                    held.discard(j)
            if max_hold > 0 and len(held) > max_hold:
                # 超额时优先保留分数高的
                ranked = sorted(held, key=lambda j: (-(row[j] if row[j] == row[j] else -9), j))
                held = set(ranked[:max_hold])
            for j in held:
                out[i, j] = 1
        return pd.DataFrame(out, index=scores.index, columns=cols)


# ------------------------------------------------------------------ 存取

def list_rules() -> list[dict]:
    from ..config import cfg
    return [_as_rule(r) for r in (cfg().get("rules") or [])]


def get_rule(name: str) -> dict | None:
    for r in list_rules():
        if r["name"] == name:
            return r
    return None


def save_rule(rule: dict) -> dict:
    from ..config import cfg
    r = _as_rule(rule)
    ok, err = validate(r["buy"])
    if not ok:
        raise RuleError(f"买入条件有误:{err}")
    if r["sell"]:
        ok, err = validate(r["sell"])
        if not ok:
            raise RuleError(f"卖出条件有误:{err}")
    c = cfg()
    items = [x for x in (c.get("rules") or []) if x.get("name") != r["name"]]
    items.append(r)
    c.set("rules", items, save=True)
    return r


def delete_rule(name: str) -> bool:
    from ..config import cfg
    c = cfg()
    items = [x for x in (c.get("rules") or []) if x.get("name") != name]
    if len(items) == len(c.get("rules") or []):
        return False
    c.set("rules", items, save=True)
    return True


def build_rule(name: str, **params) -> RuleStrategy:
    r = get_rule(name)
    if not r:
        raise RuleError(f"没有策略 {name}")
    return RuleStrategy(rule=r, **params)


EXAMPLES = [
    {"name": "均线多头", "desc": "收盘站上 20 日线且 5 日线上穿 20 日线时买入,跌破 20 日线卖出",
     "buy": "close > ma(20) and ma(5) > ma(20)", "sell": "close < ma(20)", "max_hold": 5},
    {"name": "20日新高", "desc": "创 20 日新高买入,回撤 8% 卖出",
     "buy": "close > hhv(close, 20) * 0.999", "sell": "close < hhv(close, 20) * 0.92",
     "max_hold": 5},
    {"name": "超跌反弹", "desc": "RSI 低于 30 且站回 5 日线买入,RSI 高于 65 卖出",
     "buy": "rsi(14) < 30 or (rsi(14) < 35 and close > ma(5))", "sell": "rsi(14) > 65",
     "max_hold": 5},
    {"name": "放量突破", "desc": "放量 1.8 倍且突破 20 日高点买入,跌破 10 日线卖出",
     "buy": "volume > vol_ma(20) * 1.8 and close > hhv(close, 20) * 0.995",
     "sell": "close < ma(10)", "max_hold": 5},
    # 下面是 v2 才写得出来的:变量复用 + 带盈亏的离场
    {"name": "趋势跟随带止损",
     "desc": "站上 60 日线且量能放大进场,跌破 20 日线或浮亏 8% 离场(变量 + 状态算子)",
     "buy": "trend := ma(60)\n"
            "close > trend and volume > vol_ma(20) * 1.2 and macd_hist > 0",
     "sell": "trend := ma(60)\n"
             "entry := close > trend\n"
             "close < trend or close < ma(20) or pnl_pct(entry, close < ma(20)) < -8",
     "max_hold": 0},
    {"name": "持有天数上限",
     "desc": "站上 20 日线买入,跌破 10 日线或持有满 10 根卖出(用 hold_bars 代替 max_hold)",
     "buy": "entry := cross(ma(5), ma(20))\n"
            "exit_ := close < ma(10)\n"
            "filter(entry, 3) and close > ma(20)",
     "sell": "entry := cross(ma(5), ma(20))\n"
             "exit_ := close < ma(10)\n"
             "exit_ or hold_bars(entry, exit_) >= 10",
     "max_hold": 0},
    {"name": "横截面动量", "desc": "20 日动量在全市场排前 10% 且站上 20 日线才买(要跑全市场)",
     "buy": "rank(pct(20)) > 0.9 and close > ma(20)", "sell": "close < ma(20)",
     "max_hold": 5},
]
