"""腾讯行情公开接口：实时快照 + 日线 K 线，作为东方财富不可用时的备用源。

快照走 qt.gtimg.cn；日线走 web.ifzq.gtimg.cn 的公开 fqkline 接口。
请求不携带 Referer 或任何私有标识。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pandas as pd

from ..utils import date_str, normalize_code, parse_date, safe_float, tx_symbol
from . import http

BASE = "https://qt.gtimg.cn/q="
KLINE_HOSTS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
BATCH = 60
CHUNK = 640

# 1 名称 2 代码 3 现价 4 昨收 5 今开 6 成交量(手) ...
# 31 涨跌额 32 涨跌幅% 33 最高 34 最低 37 成交额(万元) 38 换手率%
# 39 市盈率(动) 43 振幅% 44 流通市值(亿) 45 总市值(亿) 46 市净率
# 47 涨停价 48 跌停价 49 量比 51 均价


def _f(parts: list[str], index: int, scale: float = 1.0) -> float | None:
    value = safe_float(parts[index]) if index < len(parts) else None
    return None if value is None else value * scale


def _parse(payload: str) -> dict | None:
    parts = payload.split("~")
    if len(parts) < 40 or not parts[2]:
        return None
    price = _f(parts, 3)
    pre_close = _f(parts, 4)
    if not price or not pre_close:
        return None
    return {
        "code": normalize_code(parts[2]),
        "name": parts[1],
        "price": price,
        "pre_close": pre_close,
        "open": _f(parts, 5),
        "high": _f(parts, 33),
        "low": _f(parts, 34),
        "change": _f(parts, 31),
        "pct_chg": _f(parts, 32),
        "volume": _f(parts, 6, 100.0),
        "amount": _f(parts, 37, 1e4),
        "turnover": _f(parts, 38),
        "pe_ttm": _f(parts, 39),
        "amplitude": _f(parts, 43),
        "float_cap": _f(parts, 44, 1e8),
        "market_cap": _f(parts, 45, 1e8),
        "pb": _f(parts, 46),
        "limit_up": _f(parts, 47),
        "limit_down": _f(parts, 48),
        "volume_ratio": _f(parts, 49),
        "source": "tencent",
    }


def quotes(codes: Iterable[str], client: http.HttpClient | None = None) -> pd.DataFrame:
    client = client or http.HttpClient()
    wanted = [normalize_code(code) for code in codes if normalize_code(code)]
    rows: list[dict] = []
    for start in range(0, len(wanted), BATCH):
        chunk = wanted[start:start + BATCH]
        text = client.get_text(
            BASE + ",".join(tx_symbol(code) for code in chunk),
            encoding="gbk",
        )
        for line in text.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            payload = line.split('="', 1)[1].rsplit('"', 1)[0]
            parsed = _parse(payload)
            if parsed:
                rows.append(parsed)
    return pd.DataFrame(rows)


def kline(
    code: str,
    start: str | None = None,
    end: str | None = None,
    adjust: str = "qfq",
    client: http.HttpClient | None = None,
    index: bool = False,
    max_bars: int = 5000,
) -> pd.DataFrame:
    """腾讯日线，按 640 根一段向前翻页，拼成完整区间。

    优先用 proxy.finance.qq.com 的 newfqkline（带成交额/换手），
    该域名不可用时依次回退 ifzq.gtimg.cn、web.ifzq.gtimg.cn。
    返回列：code/date/open/high/low/close/volume/amount/turnover/pct_chg/change。
    """
    client = client or http.HttpClient()
    start = date_str(start or "2019-01-01")
    end = date_str(end or date.today())
    symbol = tx_symbol(code, index=index)
    key = "qfqday" if adjust == "qfq" else "day"
    adjust_param = "qfq" if adjust == "qfq" else ""

    collected: dict[str, list] = {}
    cursor = end
    preferred = 0
    last_error: Exception | None = None
    for _ in range(16):
        param = f"{symbol},day,{start},{cursor},{CHUNK},{adjust_param}"
        payload = None
        for offset in range(len(KLINE_HOSTS)):
            host = KLINE_HOSTS[(preferred + offset) % len(KLINE_HOSTS)]
            try:
                payload = client.get_json(host, params={"param": param})
                preferred = (preferred + offset) % len(KLINE_HOSTS)
                break
            except http.HttpError as exc:
                last_error = exc
                continue
        if payload is None:
            raise http.HttpError(f"腾讯 K 线接口不可用: {last_error}")
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            break
        block = (payload.get("data") or {}).get(symbol) or {}
        rows = block.get(key) or block.get("day") or []
        if not rows:
            break
        added = 0
        for row in rows:
            if len(row) < 6:
                continue
            day = str(row[0])[:10]
            if day not in collected:
                padded = list(row[:10]) + [None] * max(0, 10 - len(row))
                collected[day] = padded
                added += 1
        first = str(rows[0][0])[:10]
        if first <= start or added == 0 or len(collected) >= max_bars:
            break
        previous = parse_date(first)
        if previous is None:
            break
        cursor = (previous - timedelta(days=1)).isoformat()

    if not collected:
        return pd.DataFrame()

    frame = pd.DataFrame(
        [collected[key] for key in sorted(collected)],
        columns=["date", "open", "close", "high", "low", "volume",
                 "extra", "turnover", "amount", "tail"],
    )
    for column in ("open", "close", "high", "low", "volume", "turnover", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[frame["date"] >= start].reset_index(drop=True)
    if frame.empty:
        return frame
    if not index:
        frame["volume"] = frame["volume"] * 100.0  # 手 -> 股
    # newfqkline 的成交额单位是万元；没有成交额的旧接口按量价估算
    frame["amount"] = frame["amount"] * 1e4
    if index:
        frame.loc[frame["amount"] <= 0, "amount"] = float("nan")
    else:
        missing = frame["amount"].isna() | (frame["amount"] <= 0)
        frame.loc[missing, "amount"] = frame.loc[missing, "volume"] * frame.loc[missing, "close"]
    frame["pct_chg"] = frame["close"].pct_change() * 100.0
    frame["change"] = frame["close"].diff()
    frame["code"] = normalize_code(code)
    frame["source"] = "tencent"
    return frame[["code", "date", "open", "high", "low", "close", "volume",
                  "amount", "turnover", "pct_chg", "change", "source"]]
