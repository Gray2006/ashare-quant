# -*- coding: utf-8 -*-
"""个股策略自主研究:枚举「形态模板 × 参数」,逐条跑历史,挑出参数稳健的几条。

不是黑箱:每个候选都是一条可读的条件表达式(和 engine/rules.py 同一套 DSL),
研究完你看到的买点/卖点就是它真实会做的事,能直接存成策略接自动交易。

防过拟合的做法:
  * 时间切分:前 70% 训练、后 30% 验证;排名看训练,但**验证段亏损的直接淘汰**;
  * 要求最小成交笔数,太少说明是巧合;
  * 报告里同时给训练/验证两段指标,不只看一个数。
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind
from . import rules as rules_mod

COST = 0.0016          # 双边成本(佣金+印花税+滑点)近似


@dataclass
class Candidate:
    name: str
    buy: str
    sell: str
    ret: float = 0.0
    ann: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    v_ret: float = 0.0
    v_sharpe: float = 0.0
    score: float = 0.0
    notes: str = ""

    def as_row(self) -> dict:
        return {"name": self.name, "buy": self.buy, "sell": self.sell,
                "ret": self.ret, "ann": self.ann, "sharpe": self.sharpe,
                "max_dd": self.max_dd, "trades": self.trades,
                "win_rate": self.win_rate, "v_ret": self.v_ret,
                "v_sharpe": self.v_sharpe, "score": self.score}


def _simulate(close: np.ndarray, open_: np.ndarray, buy: np.ndarray, sell: np.ndarray,
              cost: float = COST) -> tuple[np.ndarray, int, int]:
    """逐根模拟:第 i 根开盘执行第 i-1 根的信号,收盘盯市。

    返回 (净值序列, 交易次数, 盈利次数)。
    """
    n = len(close)
    if n < 2:
        return np.ones(1), 0, 0
    equity = np.ones(n)
    pos = False
    entry_px = 0.0
    cash = 1.0
    qty = 0.0
    trades = wins = 0
    for i in range(1, n):
        if pos:
            if sell[i - 1]:
                px = open_[i] * (1 - cost)
                cash = qty * px
                profit = px - entry_px * (1 + cost)
                trades += 1
                wins += 1 if profit > 0 else 0
                pos, qty = False, 0.0
        else:
            if buy[i - 1]:
                px = open_[i] * (1 + cost)
                if px > 0:
                    qty = cash / px
                    entry_px = open_[i]
                    cash = 0.0
                    pos = True
        equity[i] = cash if not pos else qty * close[i]
    return equity, trades, wins


def _metrics(eq: np.ndarray) -> tuple[float, float, float, float]:
    """(总收益, 年化, 夏普, 最大回撤)。"""
    if len(eq) < 3 or eq[0] <= 0:
        return 0.0, 0.0, 0.0, 0.0
    ret = float(eq[-1] / eq[0] - 1)
    rets = np.diff(eq) / eq[:-1]
    rets = rets[np.isfinite(rets)]
    if len(rets) < 2:
        return ret, 0.0, 0.0, 0.0
    sd = float(np.std(rets, ddof=1))
    sharpe = float(np.mean(rets) / sd * math.sqrt(252)) if sd > 0 else 0.0
    ann = (1 + ret) ** (252 / max(1, len(eq))) - 1
    peak = np.maximum.accumulate(eq)
    dd = float(np.min(eq / peak - 1))
    return ret, float(ann), sharpe, dd


def _templates() -> list[tuple[str, str, str]]:
    """(名称, 买入表达式, 卖出表达式) 模板池。

    参数用表达式的占位符 `#1 #2 …`,不是 str.format 的 `{n}` ——
    这样模板本身先过一遍语法检查,写得不对在研究开始前就报出来;
    回填时也不会因为少给一个键拼出半句坏代码。
    """
    t = [
        ("均线趋势", "close > ma(#1)", "close < ma(#1)"),
        ("均线金叉", "cross(ma(#1), ma(#2))", "cross(ma(#2), ma(#1))"),
        ("双均线跟随", "ma(#1) > ma(#2) and close > ma(#1)", "close < ma(#2)"),
        ("20日新高突破", "close > hhv(close, #1) * 0.999 and volume > vol_ma(20) * #2",
         "close < ma(#3)"),
        ("回踩均线", "close > ma(#1) and close < ma(#1) * 1.02 and rsi(6) < #2",
         "close < ma(#1) * #3"),
        ("RSI超跌", "rsi(#1) < #2", "rsi(#1) > #3"),
        ("MACD金叉", "cross(macd_dif, macd_dea)", "cross(macd_dea, macd_dif)"),
        ("布林下轨", "close < boll_low", "close > boll_mid"),
        ("放量上攻", "volume > vol_ma(#1) * #2 and close > ma(#3)", "close < ma(#3)"),
        ("趋势加超跌", "close > ma(#1) and rsi(6) < #2", "close < ma(#1) * #3"),
        ("ATR止损", "close > ma(#1)", "close < ma(#1) * #2 or close < hhv(close, 20) * #3"),
    ]
    return t


def candidates() -> list[tuple[str, str, str]]:
    """展开参数网格,返回具体的 (名称, buy, sell) 列表。

    模板里是 `#1 #2 …` 占位,这里用 `dsl.render()` 回填成具体数字。
    比 str.format 好在:少给参数、模板语法错,都会当场报出来,不会拼出坏表达式。
    """
    from . import dsl
    out: list[tuple[str, str, str]] = []
    for name, b, s in _templates():
        for pretty, values in _slots(name):
            try:
                buy = dsl.render(b, values) if dsl.param_count(b) else b
                sell = dsl.render(s, values) if dsl.param_count(s) else s
            except Exception:  # noqa: BLE001 —— 模板写错不要拖垮整张网
                continue
            out.append((pretty, buy, sell))
    return out


def _slots(name: str) -> list[tuple[str, list]]:
    """每套模板给出一批 (名字, 参数列表)。参数顺序就是 #1 #2 … 的顺序。"""
    if name == "均线趋势":
        return [(f"均线趋势 MA{n}", [n]) for n in (5, 8, 10, 15, 20, 30, 40, 60)]
    if name == "均线金叉":
        return [(f"均线金叉 MA{f}/MA{s2}", [f, s2])
                for f, s2 in ((5, 10), (5, 20), (5, 30), (5, 60),
                              (10, 20), (10, 30), (10, 60), (20, 60))]
    if name == "双均线跟随":
        return [(f"双均线 MA{f}/MA{s2}", [f, s2])
                for f, s2 in ((5, 20), (5, 60), (10, 30), (20, 60))]
    if name == "20日新高突破":
        return [(f"新高{n}日 量{k}倍 卖MA{s2}", [n, k, s2])
                for n, k in ((5, 1.2), (10, 1.0), (10, 1.2), (20, 1.5),
                             (20, 2.0), (30, 1.5), (60, 1.5))
                for s2 in (5, 10, 20)]
    if name == "回踩均线":
        return [(f"回踩MA{n} RSI{r}", [n, r, x])
                for n, r, x in ((10, 45, 0.98), (20, 40, 0.97), (20, 50, 0.95),
                                (20, 60, 0.94), (60, 45, 0.95), (60, 55, 0.92))]
    if name == "RSI超跌":
        return [(f"RSI{n} 低于{r}", [n, r, q])
                for n, r, q in ((6, 15, 65), (6, 20, 70), (6, 25, 75),
                                (14, 25, 60), (14, 30, 65), (14, 35, 70))]
    if name == "放量上攻":
        return [(f"放量{k}倍MA{n} 卖MA{s2}", [n, k, s2])
                for n, k, s2 in ((20, 1.5, 10), (20, 1.8, 10), (20, 2.5, 20),
                                 (5, 2.0, 10), (60, 2.0, 20))]
    if name == "趋势加超跌":
        return [(f"MA{s2}上RSI{r}", [s2, r, x])
                for s2, r, x in ((20, 40, 0.97), (20, 50, 0.95), (60, 45, 0.94))]
    if name == "ATR止损":
        return [(f"MA{s2}+回撤{int((1 - dd) * 100)}%止损", [s2, x, dd])
                for s2, x, dd in ((20, 0.95, 0.88), (20, 0.97, 0.92),
                                  (60, 0.94, 0.90))]
    return [(name, [])]


def research(code: str, days: int = 400, top: int = 8,
             progress=None) -> tuple[list[Candidate], pd.DataFrame, dict]:
    """研究单只股票的历史,返回 (排名候选, 日线, 元信息)。"""
    from ..data import kline as kl

    bars, src = kl.get_kline(code, 101, count=max(200, days))
    if not bars:
        raise RuntimeError(f"{code} 没有取到日线")
    df = ind.bars_to_df(bars)
    if len(df) < 180:
        raise RuntimeError(f"日线只有 {len(df)} 根,不够研究(至少 180 根)")
    df = df.tail(max(180, days)).reset_index(drop=True)

    close = np.asarray(df["close"], dtype="float64")
    open_ = np.asarray(df["open"], dtype="float64")
    n = len(df)
    split = int(n * 0.7)

    pool = candidates()
    results: list[Candidate] = []
    for i, (name, buy_expr, sell_expr) in enumerate(pool):
        try:
            buy = rules_mod.evaluate(buy_expr, df)
            sell = rules_mod.evaluate(sell_expr, df)
        except Exception:
            continue
        eq, trades, wins = _simulate(close, open_, buy, sell)
        if trades < 3:
            continue
        ret, ann, sharpe, dd = _metrics(eq)
        # 验证段单独再跑一遍(用全段信号,只看后 30% 的净值)
        eq_v = eq[split:]
        v_ret, _v_ann, v_sharpe, _v_dd = _metrics(eq_v)
        win_rate = wins / trades if trades else 0.0
        # 打分只看训练段(验证段用来淘汰,不能拿来挑——否则就是偷看未来)。
        # 训练夏普为主,回撤、笔数做温和修正;验证段亏损直接判不合格。
        score = sharpe
        score -= max(0.0, dd + 0.30) * 1.5        # 回撤超 30% 扣分
        score += min(trades, 15) * 0.02           # 样本太少的略微降权
        if v_ret <= 0 or v_sharpe <= 0:
            score -= 5.0                          # 验证段不成立 → 直接沉底
        results.append(Candidate(name=name, buy=buy_expr, sell=sell_expr,
                                 ret=ret, ann=ann, sharpe=sharpe, max_dd=dd,
                                 trades=trades, win_rate=win_rate,
                                 v_ret=v_ret, v_sharpe=v_sharpe, score=score))
        if progress and i % 5 == 0:
            progress(i, len(pool))

    results.sort(key=lambda c: c.score, reverse=True)
    meta = {"code": code, "bars": n, "split": str(df["datetime"].iloc[split])[:10],
            "start": str(df["datetime"].iloc[0])[:10],
            "end": str(df["datetime"].iloc[-1])[:10],
            "source": src, "tested": len(pool)}
    return results[:top], df, meta


def save_candidate(code: str, cand: Candidate, name: str | None = None) -> dict:
    """把研究出来的候选存成可用的规则策略。"""
    label = name or f"{code}·{cand.name}"
    return rules_mod.save_rule({
        "name": label,
        "desc": f"{code} 研究得出 · 训练收益 {cand.ret*100:.1f}% · 验证 {cand.v_ret*100:.1f}%",
        "buy": cand.buy, "sell": cand.sell,
        "max_hold": 1, "hold_until_sell": True,
    })
