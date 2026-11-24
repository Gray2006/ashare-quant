# -*- coding: utf-8 -*-
"""Point-in-time 股票池:按「当时的真实上市状态」取池子,含退市股。

## 为什么需要它

原来的做法是「现在有哪些票,就拿这些票回测过去」。这叫幸存者偏差:
退市的票(退市前往往跌 60%~90%)根本不在池子里,回测收益被系统性抬高。
实测本地缓存:1432 只票,末日早于全市场的 **0 只** —— 一只退市股都没有。

另一个坑是「前段没有横截面」:2020 年每日中位只有 1 只票有日线,
那段算出来的 IC 是伪样本,不是"市场没机会",是"数据没覆盖"。

## 做法

1. **回补**:把全市场(含已退市)日线拉进 `kline_cache`,一只一次,增量更新。
   退市股靠「日线接口仍能返回历史」这一点拿到 —— 实测 300104 乐视网、
   600086 东方金钰都能取到退市前最后一根。
2. **重建**:给定日期 d,只取「首根 ≤ d ≤ 末日 + 容忍」的票。
   退市股在退市日之后自然从池子里消失,不是一开始就剔除。
3. **可交易性**:d 日必须有收盘价,且前 20 个交易日有成交(停牌太久的剔掉)。

## 用法

    from quantlab.engine import pitpool

    pitpool.backfill(limit=600)          # 回补(增量,可反复跑)
    codes = pitpool.members("2023-06-30")  # 当天真实可交易的票
    data  = pitpool.panel("2023-01-01", "2023-12-31", codes=codes)

回补不覆盖已有更新数据:`kline_cache` 是 INSERT OR REPLACE 按 (code,klt,fqt,bar_time),
重复跑只会补新根,不会破坏已有缓存。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from ..data import em
from ..data.store import store
from ..utils import normalize_code

# 退市股在退市日之后多久彻底移出池子。A 股退市整理期 30 个交易日,
# 这里给 30 个自然日,够了。取太大等于把退市股留在池子里假装还能买。
DELIST_GRACE_DAYS = 30

# 回补时每只票抓多少根。10000 够覆盖 1990 年至今所有 A 股(600519 是 6045 根)。
BACKFILL_COUNT = 10000

_MEM: dict[str, dict] = {}


# ==================================================================== 回补

def universe_codes(include_delisted: bool = True) -> list[str]:
    """全市场代码清单(当前在册的)。

    退市股的代码拿不到独立名单接口(东财几个候选报表名都不存在),
    但**已在缓存里的退市股会被保留** —— 回补时不清表,所以跑过一轮之后
    退市股就一直在。想主动补某只退市股,直接把它加进 extra 参数。
    """
    codes: list[str] = []
    seen: set[str] = set()
    try:
        rows = em.market_list_all("f6", asc=False, limit=6000)
        for r in rows:
            c = normalize_code(r.get("code") or "")
            if c and c not in seen:
                seen.add(c)
                codes.append(c)
    except Exception:  # noqa: BLE001
        pass
    if include_delisted:
        # 缓存里已有的(含历史退市股)一起保留
        try:
            rows = store().conn.execute(
                "select distinct code from kline_cache where klt=101 and fqt=1"
            ).fetchall()
            for r in rows:
                c = str(r[0])
                if c.startswith("idx:"):
                    continue
                if c and c not in seen:
                    seen.add(c)
                    codes.append(c)
        except Exception:  # noqa: BLE001
            pass
    return codes


def _have_last(code: str) -> str | None:
    try:
        return store().last_kline_time(code, 101, 1)
    except Exception:  # noqa: BLE001
        return None


def backfill_one(code: str, force: bool = False, retries: int = 2,
                 pause: float = 0.0, source: str = "sina") -> tuple[str, int, str]:
    """补一只票的全历史。返回 (code, 新增/总根数, 状态)。

    数据源选择(实测结论,别乱改):

    * **新浪**(默认)—— 唯一同时满足「全历史」和「含退市股」的源。
      600519 能取到 6045 根(2001-08 起),乐视网 300104 能取到 1701 根
      (退市到 2021-06)。这是修幸存者偏差的关键。
    * 东财 push2his —— 数据最全,但对并发极敏感:实测 8 线程并行 5582 只
      全部失败,还会把本机 IP 打进冷却(90 秒)甚至直接断连。只在别处单个取数时用。
    * 腾讯 —— 只回 800 根(约 3 年),且**退市股返回 0 根**,补历史没用。

    所以这里默认走新浪、串行。
    """
    code = normalize_code(code)
    if not code:
        return code, 0, "空代码"
    if pause:
        time.sleep(pause)
    last = _have_last(code) if not force else None
    bars = None
    err = ""
    for attempt in range(max(1, int(retries) + 1)):
        try:
            if source == "sina":
                from ..data import kline as _k
                from ..utils import sina_symbol
                bars = _k._sina_kline(sina_symbol(code), BACKFILL_COUNT)
            else:
                bars = em.kline(code, klt=101, count=BACKFILL_COUNT, fqt=1)
            if bars:
                break
        except Exception as e:  # noqa: BLE001
            err = str(e)[:60]
        time.sleep(0.4 * (attempt + 1))     # 退避
    if not bars:
        return code, 0, f"取数失败:{err}" if err else "无数据"
    # 新浪只回 6 个字段(day/open/high/low/close/volume),**没有成交额**。
    # 用 close×volume 估算:volume 已由 _sina_kline 转成手,乘 100 还原成股。
    # 这是近似 —— 真实成交额是每笔价格加权的 VWAP,这里用收盘价代替,
    # 只用于「按流动性排序/筛池子」,不用于算收益,误差可接受。
    for b in bars:
        if b.get("amount") is None and b.get("volume") and b.get("close"):
            b["amount"] = float(b["volume"]) * 100.0 * float(b["close"])
    # 一律全量写入(INSERT OR REPLACE 按 code+klt+fqt+bar_time 去重)。
    #
    # 这里曾经写过错:原来做「只写比缓存末根更新的部分」的增量,
    # 但回补要的正是**更早的历史** —— 末根不动,历史全被丢掉。
    # 实测 600519 取到 6045 根,库里只落了 1403 根(2021 年起),就是这么来的。
    # 代价是重跑要重写一遍,可接受:INSERT OR REPLACE 是幂等的。
    old = 0
    if last:
        old = store().conn.execute(
            "select count(*) from kline_cache where code=? and klt=101 and fqt=1",
            (code,)).fetchone()[0]
    store().save_kline(code, 101, 1, bars)
    gained = max(0, len(bars) - int(old))
    return code, gained, ("全量" if gained or not old else "已最新")


def backfill(codes: list[str] | None = None, limit: int | None = None,
             workers: int = 1, progress=None, force: bool = False,
             pause: float = 0.05) -> dict:
    """批量回补。默认全市场、**串行**。

    并发会被东财限流(8 线程实测 100% 失败),别调高 workers。
    pause=0.05 是请求间隔,全市场约 5582 × (0.137+0.05) ≈ 17 分钟。
    中途失败可以重跑,已最新的会跳过。
    """
    todo = list(codes) if codes else universe_codes()
    if limit:
        todo = todo[:int(limit)]
    t0 = time.time()
    done = {"全量": 0, "增量": 0, "已最新": 0, "无数据": 0, "取数失败": 0}
    added = 0
    errs: list[str] = []
    total = len(todo)

    def _report(i: int) -> None:
        if progress and (i % 100 == 0 or i == total):
            progress(i, total)

    if int(workers) > 1:
        # 只在小批量时给并发(补几只退市股),大批量一律串行
        with ThreadPoolExecutor(max_workers=int(workers)) as ex:
            for i, (code, n, status) in enumerate(
                    ex.map(lambda c: backfill_one(c, force, pause=pause), todo), 1):
                added += n
                key = status.split(":")[0]
                done[key] = done.get(key, 0) + 1
                if key == "取数失败":
                    errs.append(f"{code}:{status}")
                _report(i)
    else:
        for i, code in enumerate(todo, 1):
            c, n, status = backfill_one(code, force, pause=pause)
            added += n
            key = status.split(":")[0]
            done[key] = done.get(key, 0) + 1
            if key == "取数失败":
                errs.append(f"{c}:{status}")
            _report(i)
    el = time.time() - t0
    return {"total": total, "added_bars": added, "elapsed_s": round(el, 1),
            "counts": done, "errors": errs[:20]}


# ==================================================================== 按日期重建

def _coverage() -> pd.DataFrame:
    """每只票的首末根日期与根数。进程内缓存 10 分钟,回补后自动失效。"""
    hit = _MEM.get("cov")
    if hit and time.time() - hit["ts"] < 600:
        return hit["df"]
    rows = store().conn.execute(
        "select code, min(bar_time) a, max(bar_time) b, count(*) n, "
        "count(distinct substr(bar_time,1,4)) ny from kline_cache "
        "where klt=101 and fqt=1 and code not like 'idx:%' group by code").fetchall()
    df = pd.DataFrame([{"code": r[0], "start": r[1], "end": r[2],
                        "bars": r[3], "years": r[4]} for r in rows])
    if not df.empty:
        df["start"] = pd.to_datetime(df["start"])
        df["end"] = pd.to_datetime(df["end"])
        df = df.set_index("code")
    _MEM["cov"] = {"ts": time.time(), "df": df}
    return df


def clear_cache() -> None:
    _MEM.clear()


def members(date: str | pd.Timestamp, min_bars: int = 120,
            grace_days: int = DELIST_GRACE_DAYS,
            include_st: bool = True) -> list[str]:
    """date 当天「真实存在且可交易」的票。

    条件:首根 ≤ d、末日 ≥ d - grace(退市/停牌容忍)、累计根数 ≥ min_bars。
    退市股在退市日之后会自然掉出,不是一开始就剔除 —— 这就是 point-in-time。
    """
    d = pd.Timestamp(date)
    cov = _coverage()
    if cov.empty:
        return []
    m = (cov["start"] <= d) & ((cov["end"] + pd.Timedelta(days=int(grace_days))) >= d) \
        & (cov["bars"] >= int(min_bars))
    out = list(cov.index[m])
    if not include_st:
        out = [c for c in out if not _is_st(c, d)]
    return out


_ST_CACHE: dict[str, pd.DataFrame] = {}


def _is_st(code: str, d) -> bool:
    """用日线涨跌幅反推 ST(阈 5%):ST 股涨跌幅限制 5%,普通股 10%/20%。

    没有 ST 名称的历史存档,这是唯一不依赖外部接口的近似。
    只在 include_st=False 时用,且只数「涨跌幅恰好在 4.8%~5.2% 的根数占比」。
    """
    df = _ST_CACHE.get(code)
    if df is None:
        try:
            rows = store().conn.execute(
                "select bar_time, close from kline_cache where klt=101 and fqt=1 "
                "and code=? order by bar_time", (code,)).fetchall()
            df = pd.DataFrame(rows, columns=["d", "c"])
            df["d"] = pd.to_datetime(df["d"])
            df["r"] = df["c"].pct_change().abs() * 100
        except Exception:  # noqa: BLE001
            df = pd.DataFrame(columns=["d", "c", "r"])
        _ST_CACHE[code] = df
    if df.empty:
        return False
    w = df[(df["d"] <= pd.Timestamp(d)) & (df["d"] > pd.Timestamp(d) - pd.Timedelta(days=120))]
    if len(w) < 20:
        return False
    hit = ((w["r"] >= 4.7) & (w["r"] <= 5.3)).sum()
    return bool(hit >= 3)


def panel(start: str, end: str, codes: list[str] | None = None,
          min_bars: int = 120) -> dict[str, pd.DataFrame]:
    """取 [start, end] 区间的日线面板。codes 缺省用区间起点的池子。"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    use = list(codes) if codes else members(s, min_bars=min_bars)
    out: dict[str, pd.DataFrame] = {}
    c = store().conn
    for code in use:
        rows = c.execute(
            "select bar_time, open, high, low, close, volume, amount from kline_cache "
            "where klt=101 and fqt=1 and code=? and bar_time>=? and bar_time<=? "
            "order by bar_time", (code, s.strftime("%Y-%m-%d"),
                                  e.strftime("%Y-%m-%d"))).fetchall()
        if len(rows) < 30:
            continue
        df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low",
                                         "close", "volume", "amount"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        out[code] = df.reset_index(drop=True)
    return out


def coverage_report() -> dict:
    """池子健康度:一眼看出还有没有幸存者偏差。"""
    cov = _coverage()
    if cov.empty:
        # 键保持一致,调用方不用判断空库
        return {"n": 0, "n_long": 0, "pct_long": 0.0, "n_delisted": 0,
                "pct_delisted": 0.0, "delisted_samples": [],
                "earliest": None, "latest": None, "n_before_2020": 0}
    all_end = cov["end"].max()
    cutoff = all_end - pd.Timedelta(days=60)
    dead = cov[cov["end"] < cutoff]
    long_ = cov[cov["bars"] >= 600]
    return {
        "n": len(cov),
        "n_long": len(long_),
        "pct_long": round(len(long_) / len(cov), 4),
        "n_delisted": len(dead),
        "pct_delisted": round(len(dead) / len(cov), 4),
        "delisted_samples": list(dead.index[:10]),
        "earliest": str(cov["start"].min())[:10],
        "latest": str(all_end)[:10],
        "n_before_2020": int((cov["start"] <= "2020-01-01").sum()),
    }
