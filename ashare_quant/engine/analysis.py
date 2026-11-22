"""单只股票的技术分析：指标、形态与一句话结论。

只用本地日线，不联网、不下单。输出既给 CLI，也给本地仪表盘。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from ..data.store import Store
from ..utils import normalize_code
from . import indicators


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(number):
        return None
    return number


def _signals(row: pd.Series, prev: pd.Series, window: pd.DataFrame) -> list[str]:
    signals: list[str] = []
    close = row["close"]
    ma5, ma10, ma20, ma60 = row["ma5"], row["ma10"], row["ma20"], row["ma60"]
    if all(pd.notna(v) for v in (ma5, ma10, ma20, ma60)):
        if ma5 > ma10 > ma20 > ma60:
            signals.append("均线多头排列")
        elif ma5 < ma10 < ma20 < ma60:
            signals.append("均线空头排列")
    if pd.notna(ma20):
        signals.append("站上20日线" if close >= ma20 else "跌破20日线")
    rsi = row["rsi14"]
    if pd.notna(rsi):
        if rsi >= 70:
            signals.append(f"RSI {rsi:.1f} 超买")
        elif rsi <= 30:
            signals.append(f"RSI {rsi:.1f} 超卖")
    hist, prev_hist = row["macd_hist"], prev["macd_hist"]
    if pd.notna(hist) and pd.notna(prev_hist):
        if prev_hist <= 0 < hist:
            signals.append("MACD 金叉")
        elif prev_hist >= 0 > hist:
            signals.append("MACD 死叉")
    if pd.notna(row["boll_upper"]) and close > row["boll_upper"]:
        signals.append("突破布林上轨")
    elif pd.notna(row["boll_lower"]) and close < row["boll_lower"]:
        signals.append("跌破布林下轨")
    vol_ratio = row["vol_ratio"]
    if pd.notna(vol_ratio):
        if vol_ratio >= 1.8:
            signals.append(f"放量 {vol_ratio:.1f} 倍")
        elif vol_ratio <= 0.5:
            signals.append("明显缩量")
    high = window["high"].max()
    low = window["low"].min()
    if pd.notna(high) and close >= high * 0.995:
        signals.append("阶段新高")
    if pd.notna(low) and close <= low * 1.005:
        signals.append("阶段新低")
    return signals


def analyze(store: Store, code: str, days: int = 250) -> dict:
    """返回 {summary, series}；本地无数据时返回 {'error': ...}。"""
    code = normalize_code(code)
    bars = store.load_bars(codes=[code])
    if bars.empty:
        return {"error": f"本地没有 {code} 的日线，先运行 aq sync --limit 300"}

    bars = bars.sort_values("date").reset_index(drop=True)
    frame = indicators.compute_all(bars)
    latest = frame.iloc[-1]
    previous = frame.iloc[-2] if len(frame) > 1 else latest
    window = frame.tail(max(days, 60))

    stocks = store.stocks()
    name = ""
    if not stocks.empty:
        matched = stocks.loc[stocks["code"] == code, "name"]
        if not matched.empty:
            name = str(matched.iloc[0])

    close = float(latest["close"])
    high = _num(window["high"].max())
    low = _num(window["low"].min())
    summary = {
        "code": code,
        "name": name,
        "date": str(latest["date"]),
        "close": close,
        "pct_chg": _num(latest["pct_chg"]),
        "volume": _num(latest["volume"]),
        "amount": _num(latest["amount"]),
        "turnover": _num(latest["turnover"]),
        "ma5": _num(latest["ma5"]),
        "ma10": _num(latest["ma10"]),
        "ma20": _num(latest["ma20"]),
        "ma60": _num(latest["ma60"]),
        "rsi14": _num(latest["rsi14"]),
        "macd_dif": _num(latest["macd_dif"]),
        "macd_dea": _num(latest["macd_dea"]),
        "macd_hist": _num(latest["macd_hist"]),
        "atr_pct": _num(latest["atr_pct"]),
        "boll_upper": _num(latest["boll_upper"]),
        "boll_mid": _num(latest["boll_mid"]),
        "boll_lower": _num(latest["boll_lower"]),
        "boll_width": _num(latest["boll_width"]),
        "vol_ratio": _num(latest["vol_ratio"]),
        "volatility20": _num(latest["volatility20"]),
        "ret20": _num(latest["close"] / frame["close"].iloc[-21] - 1) if len(frame) > 21 else None,
        "ret60": _num(latest["close"] / frame["close"].iloc[-61] - 1) if len(frame) > 61 else None,
        "high": high,
        "low": low,
        "drawdown_from_high": (close / high - 1.0) if high else None,
        "bars": int(len(frame)),
        "signals": _signals(latest, previous, window),
    }
    summary["trend"] = ("多头" if "均线多头排列" in summary["signals"]
                        else "空头" if "均线空头排列" in summary["signals"]
                        else "震荡")

    series = []
    for _, row in window.iterrows():
        series.append({
            "date": str(row["date"]),
            "open": _num(row["open"]),
            "high": _num(row["high"]),
            "low": _num(row["low"]),
            "close": _num(row["close"]),
            "volume": _num(row["volume"]),
            "ma5": _num(row["ma5"]),
            "ma20": _num(row["ma20"]),
            "ma60": _num(row["ma60"]),
            "dif": _num(row["macd_dif"]),
            "dea": _num(row["macd_dea"]),
            "hist": _num(row["macd_hist"]),
            "rsi": _num(row["rsi14"]),
        })
    return {"summary": summary, "series": series}


def format_summary(result: dict) -> str:
    """CLI 用的紧凑文本。"""
    if "error" in result:
        return result["error"]
    s = result["summary"]
    lines = [
        f"{s['code']} {s['name']}  {s['date']}  收盘 {s['close']:.2f}  "
        f"涨跌 {s['pct_chg'] if s['pct_chg'] is not None else 0:.2f}%  趋势 {s['trend']}",
        f"MA5 {s['ma5']:.2f}  MA10 {s['ma10']:.2f}  MA20 {s['ma20']:.2f}  MA60 {s['ma60']:.2f}"
        if all(s[k] is not None for k in ("ma5", "ma10", "ma20", "ma60")) else "均线数据不足",
        f"RSI14 {s['rsi14']:.1f}  MACD {s['macd_dif']:.3f}/{s['macd_dea']:.3f}/{s['macd_hist']:.3f}  "
        f"ATR {s['atr_pct'] * 100:.2f}%  波动 {s['volatility20'] * 100:.2f}%"
        if all(s[k] is not None for k in ("rsi14", "macd_dif", "macd_dea", "macd_hist",
                                          "atr_pct", "volatility20")) else "指标数据不足",
        f"20日 {s['ret20'] * 100:.2f}%  60日 {s['ret60'] * 100:.2f}%  "
        f"距250日高 {s['drawdown_from_high'] * 100:.2f}%"
        if all(s[k] is not None for k in ("ret20", "ret60", "drawdown_from_high")) else "",
        "信号: " + ("、".join(s["signals"]) if s["signals"] else "无"),
    ]
    return "\n".join(line for line in lines if line)
