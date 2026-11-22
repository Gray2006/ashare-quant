"""数据同步：股票列表、日线、基准指数。

策略：
* 首次同步从 config.sync.start 开始，之后按每只股票的最后日期增量拉取；
* 单线程 + 限速，宁可慢，不并发轰炸公开接口；
* 单只失败只记错误，不中断整体任务；
* 东方财富不可用时回退新浪列表 + 腾讯日线，两处都是公开接口；
* 默认不抓新闻、公告、资金流榜单等非必要数据。
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Callable, Iterable

import pandas as pd

from .. import config
from ..utils import date_str, market_of, normalize_code
from . import eastmoney, sina, tencent
from .http import HttpClient, HttpError
from .store import Store

BENCHMARKS = ("000300", "000905", "000852", "000001")


def _next_day(value: str) -> str:
    try:
        return (date.fromisoformat(value) + timedelta(days=1)).isoformat()
    except ValueError:
        return value


def sync_universe(store: Store, client: HttpClient | None = None) -> int:
    """同步全市场股票列表与最新快照（只存代码/名称/行业）。

    东方财富不可用时回退新浪列表；两处都是公开接口，不伪装请求头。
    """
    client = client or HttpClient()
    source = "eastmoney"
    try:
        frame = eastmoney.list_stocks(client=client)
    except (HttpError, ValueError):
        frame = sina.list_stocks(client=client)
        source = "tencent"
    if frame.empty:
        return 0
    frame["market"] = frame["code"].map(market_of)
    count = store.save_stocks(frame)
    store.set_meta("universe_synced_at", pd.Timestamp.now().isoformat(timespec="seconds"))
    store.set_meta("data_source", source)
    return count


def sync_bars(
    store: Store,
    codes: Iterable[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    full: bool = False,
    limit: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    client: HttpClient | None = None,
    source: str | None = None,
) -> dict:
    """增量同步日线。返回统计信息。

    source='eastmoney' 走东财，'tencent' 走腾讯日线；默认读上次全量同步记下的源。
    """
    client = client or HttpClient()
    start = start or config.get("sync.start", "2019-01-01")
    end = end or date_str(date.today())
    source = source or store.get_meta("data_source") or "eastmoney"

    if codes is None:
        stocks = store.stocks()
        codes = list(stocks["code"]) if not stocks.empty else []
    else:
        codes = [normalize_code(code) for code in codes]
    codes = [code for code in codes if code]
    if limit:
        codes = codes[:limit]

    last_dates = {} if full else store.last_dates()
    total = len(codes)
    stats = {"codes": total, "bars": 0, "failed": 0, "skipped": 0,
             "source": source, "errors": []}
    started = time.time()

    for index, code in enumerate(codes, 1):
        beg = start
        if not full and code in last_dates:
            beg = _next_day(last_dates[code])
        if beg > end:
            stats["skipped"] += 1
            if progress:
                progress(index, total, code)
            continue
        try:
            if source == "tencent":
                bars = tencent.kline(code, start=beg, end=end, adjust="qfq", client=client)
            else:
                bars = eastmoney.kline(code, start=beg, end=end, adjust="qfq", client=client)
            if bars.empty:
                stats["skipped"] += 1
            else:
                stats["bars"] += store.save_bars(bars)
        except (HttpError, ValueError) as exc:
            stats["failed"] += 1
            if len(stats["errors"]) < 20:
                stats["errors"].append(f"{code}: {exc}")
        if progress:
            progress(index, total, code)

    stats["seconds"] = round(time.time() - started, 1)
    store.set_meta("bars_synced_at", pd.Timestamp.now().isoformat(timespec="seconds"))
    return stats


def sync_benchmarks(
    store: Store,
    codes: Iterable[str] = BENCHMARKS,
    start: str | None = None,
    end: str | None = None,
    client: HttpClient | None = None,
    source: str | None = None,
) -> int:
    client = client or HttpClient()
    start = start or config.get("sync.start", "2019-01-01")
    end = end or date_str(date.today())
    source = source or store.get_meta("data_source") or "eastmoney"
    saved = 0
    for code in codes:
        try:
            if source == "tencent":
                frame = tencent.kline(code, start=start, end=end, adjust="none",
                                      index=True, client=client)
            else:
                frame = eastmoney.kline(code, start=start, end=end, adjust="none",
                                        index=True, client=client)
            if not frame.empty:
                saved += store.save_index_bars(frame)
        except (HttpError, ValueError):
            continue
    return saved


def sync_all(
    store: Store,
    start: str | None = None,
    full: bool = False,
    limit: int | None = None,
    with_universe: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    client = HttpClient()
    result: dict = {}
    if with_universe:
        result["universe"] = sync_universe(store, client=client)
    source = store.get_meta("data_source") or "eastmoney"
    result["source"] = source
    result["bars"] = sync_bars(store, start=start, full=full, limit=limit,
                               progress=progress, client=client, source=source)
    result["benchmarks"] = sync_benchmarks(store, start=start, client=client, source=source)
    return result


def realtime_quotes(codes: Iterable[str]) -> pd.DataFrame:
    """实时快照：东方财富优先，失败时回退腾讯。"""
    codes = list(codes)
    try:
        frame = eastmoney.quotes(codes)
        if not frame.empty:
            return frame
    except HttpError:
        pass
    return tencent.quotes(codes)
