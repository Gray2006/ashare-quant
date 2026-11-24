# -*- coding: utf-8 -*-
"""个股走势反向推算:从「哪些条件之后涨得多」倒推策略。

正向研究是「给定规则跑历史」;这里是反的——先定义结果(未来 N 日涨幅),
再回看每根 K 线上的指标分档,统计「条件成立后」的实际胜率与平均涨幅,
按提升度(lift = 条件胜率 / 基准胜率)排序,把最高的几组翻译回 DSL 条件。

产出是条件片段 + 实测统计,不是拟合权重:
看清「这只票历史上,RSI 低于 25 之后 5 日平均涨 2.1%、胜率 63%」这种事实,
再决定要不要把它写进策略。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from . import rules as rules_mod

HORIZON = 5            # 默认看未来 5 个交易日
THRESHOLD = 0.03       # 涨跌判定:±3%
MIN_SAMPLES = 8        # 样本太少不算数


@dataclass
class Finding:
    """一条反推结论。"""
    name: str
    cond: str                       # 条件片段(可直接拼进规则)
    samples: int = 0
    win_rate: float = 0.0           # 条件成立后上涨的概率
    avg_ret: float = 0.0            # 条件成立后 N 日平均收益
    base_rate: float = 0.0          # 全样本上涨概率
    lift: float = 0.0               # win_rate / base_rate
    avg_base: float = 0.0           # 全样本 N 日平均收益
    up_after: int = 0
    down_after: int = 0

    def as_row(self) -> dict:
        return {"name": self.name, "cond": self.cond, "samples": self.samples,
                "win_rate": self.win_rate, "avg_ret": self.avg_ret,
                "lift": self.lift, "base_rate": self.base_rate}


def _features(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """每根 K 线上的指标,全部因果。"""
    c = pd.Series(np.asarray(df["close"], dtype="float64"))
    h = pd.Series(np.asarray(df["high"], dtype="float64"))
    l = pd.Series(np.asarray(df["low"], dtype="float64"))
    v = pd.Series(np.asarray(df["volume"], dtype="float64"))
    dif, dea, hist = ind.macd(c)
    k, d, j = ind.kdj(h, l, c)
    bu, bm, bl = ind.boll(c)
    rsi6, rsi14 = ind.rsi(c, 6), ind.rsi(c, 14)
    ma5, ma10, ma20, ma60 = (ind.ma(c, n) for n in (5, 10, 20, 60))
    atr14 = ind.atr(h, l, c, 14)
    vol_ma20 = ind.ma(v, 20)
    pos = np.where(bu > bl, (c - bl) / (bu - bl), np.nan)      # 布林带内位置 0~1
    return {
        "rsi6": np.asarray(rsi6), "rsi14": np.asarray(rsi14),
        "ma20_gap": np.asarray(c / ma20 - 1), "ma60_gap": np.asarray(c / ma60 - 1),
        "ma5_gap": np.asarray(c / ma5 - 1),
        "macd_hist": np.asarray(hist), "kdj_j": np.asarray(j),
        "boll_pos": pos, "vol_ratio": np.asarray(v / vol_ma20),
        "atr_pct": np.asarray(atr14 / c),
        "ret5": np.asarray(c / c.shift(5) - 1), "ret20": np.asarray(c / c.shift(20) - 1),
        "cross_up": np.asarray(ind.ma(c, 5) > ind.ma(c, 20)),
        "price": np.asarray(c),
    }


def _fwd_return(close: np.ndarray, horizon: int) -> np.ndarray:
    """未来 N 日收益(最后一根没有未来,置 NaN)。"""
    out = np.full(len(close), np.nan)
    if len(close) > horizon:
        out[:-horizon] = close[horizon:] / close[:-horizon] - 1
    return out


def _buckets() -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """(特征名, 说明, [(条件片段, 显示名, 分档)])。

    条件片段里的 {x} 会被替换成 f(x) 形式,便于拼进规则表达式。
    """
    return [
        ("rsi6", "RSI(6)", [
            ("rsi(6) < 20", "RSI6<20", ""), ("rsi(6) < 30", "RSI6<30", ""),
            ("rsi(6) > 70", "RSI6>70", ""), ("rsi(6) > 80", "RSI6>80", "")]),
        ("rsi14", "RSI(14)", [
            ("rsi(14) < 30", "RSI14<30", ""), ("rsi(14) > 70", "RSI14>70", "")]),
        ("ma20_gap", "离 20 日线", [
            ("close < ma(20) * 0.95", "低于MA20 5%", ""),
            ("close < ma(20) * 0.90", "低于MA20 10%", ""),
            ("close > ma(20) * 1.05", "高于MA20 5%", ""),
            ("close > ma(20) * 1.10", "高于MA20 10%", "")]),
        ("ma60_gap", "离 60 日线", [
            ("close < ma(60) * 0.90", "低于MA60 10%", ""),
            ("close > ma(60) * 1.10", "高于MA60 10%", "")]),
        ("boll_pos", "布林带位置", [
            ("close < boll_low", "跌破布林下轨", ""),
            ("close > boll_up", "突破布林上轨", ""),
            ("close < boll_mid * 0.97", "低于中轨 3%", "")]),
        ("vol_ratio", "量比(20日)", [
            ("volume > vol_ma(20) * 2", "放量 2 倍", ""),
            ("volume > vol_ma(20) * 3", "放量 3 倍", ""),
            ("volume < vol_ma(20) * 0.6", "缩量 40%", "")]),
        ("macd_hist", "MACD 柱", [
            ("macd_hist > 0 and ref(macd_hist, 1) <= 0", "MACD 翻红", ""),
            ("macd_hist < 0 and ref(macd_hist, 1) >= 0", "MACD 翻绿", "")]),
        ("kdj_j", "KDJ 的 J", [
            ("kdj_j < 0", "J<0", ""), ("kdj_j > 100", "J>100", "")]),
        ("ret5", "5 日涨幅", [
            ("pct(5) < -8", "5日跌超8%", ""), ("pct(5) > 8", "5日涨超8%", "")]),
        ("cross_up", "均线状态", [
            ("ma(5) > ma(20)", "MA5 在 MA20 上", ""),
            ("cross(ma(5), ma(20))", "MA5 上穿 MA20", "")]),
    ]


def _measure(fv: np.ndarray, mask: np.ndarray, fwd: np.ndarray,
             horizon: int) -> tuple[int, int, int]:
    """条件成立的那些根里,未来 N 日上涨/下跌的根数。"""
    ok = mask & np.isfinite(fwd) & np.isfinite(fv)
    if not ok.any():
        return 0, 0, 0
    r = fwd[ok]
    return int(ok.sum()), int((r > 0).sum()), int((r < 0).sum())


def reverse_model(code: str, days: int = 400, horizon: int = HORIZON,
                  top: int = 10) -> tuple[list[Finding], pd.DataFrame, dict]:
    """反推:这只股票历史上,哪些条件之后涨得多。"""
    from ..data import kline as kl

    bars, src = kl.get_kline(code, 101, count=max(200, days))
    if not bars:
        raise RuntimeError(f"{code} 没有取到日线")
    df = ind.bars_to_df(bars)
    if len(df) < 120:
        raise RuntimeError(f"日线只有 {len(df)} 根,不够反推(至少 120 根)")
    df = df.tail(max(120, days)).reset_index(drop=True)

    close = np.asarray(df["close"], dtype="float64")
    fwd = _fwd_return(close, horizon)
    valid = np.isfinite(fwd)
    base_rate = float((fwd[valid] > 0).mean()) if valid.any() else 0.0
    base_avg = float(np.nanmean(fwd)) if valid.any() else 0.0

    findings: list[Finding] = []
    for feat, label, conds in _buckets():
        try:
            fv = _features(df)[feat]
        except Exception:
            continue
        for cond, disp, _extra in conds:
            try:
                mask = rules_mod.evaluate(cond, df)
            except Exception:
                continue
            n, up, down = _measure(fv, mask, fwd, horizon)
            if n < MIN_SAMPLES:
                continue
            win = up / n
            avg = float(np.nanmean(fwd[mask & valid])) if n else 0.0
            findings.append(Finding(
                name=disp, cond=cond, samples=n, win_rate=win, avg_ret=avg,
                base_rate=base_rate, lift=(win / base_rate) if base_rate else 0.0,
                avg_base=base_avg, up_after=up, down_after=down))

    # 组合两条条件,找更强的交集(只在高提升的单条之间配)
    singles = [f for f in findings if f.samples >= MIN_SAMPLES * 2 and f.lift >= 1.15]
    singles.sort(key=lambda f: f.lift, reverse=True)
    combos: list[Finding] = []
    for a, b in itertools.combinations(singles[:6], 2):
        if a.cond == b.cond or a.name in b.cond or b.name in a.cond:
            continue
        combo = f"({a.cond}) and ({b.cond})"
        try:
            mask = rules_mod.evaluate(combo, df)
        except Exception:
            continue
        n, up, down = _measure(np.zeros(len(df)), mask, fwd, horizon)
        if n < MIN_SAMPLES:
            continue
        win = up / n
        avg = float(np.nanmean(fwd[mask & valid])) if n else 0.0
        combos.append(Finding(
            name=f"{a.name} + {b.name}", cond=combo, samples=n, win_rate=win,
            avg_ret=avg, base_rate=base_rate,
            lift=(win / base_rate) if base_rate else 0.0, avg_base=base_avg,
            up_after=up, down_after=down))

    allf = findings + combos
    allf.sort(key=lambda f: (f.lift, f.avg_ret), reverse=True)
    meta = {"code": code, "bars": len(df), "horizon": horizon,
            "start": str(df["datetime"].iloc[0])[:10],
            "end": str(df["datetime"].iloc[-1])[:10],
            "base_rate": base_rate, "base_avg": base_avg,
            "source": src, "tested": sum(len(c[2]) for c in _buckets())}
    return allf[:top], df, meta


def suggest_rule(finding: Finding, holding_days: int = HORIZON) -> dict:
    """把一条反推结论翻成可保存的策略(买点=该条件,卖点=持有 N 日后的常识止损)。"""
    return {
        "name": f"反推·{finding.name}",
        "desc": (f"历史反推:{finding.cond};"
                 f"命中 {finding.samples} 次,{finding.win_rate*100:.0f}% 上涨,"
                 f"平均 {finding.avg_ret*100:+.1f}%"),
        "buy": finding.cond,
        "sell": "close < hhv(close, 5) * 0.95",
        "max_hold": 0,
        "hold_until_sell": True,
    }
