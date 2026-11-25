# -*- coding: utf-8 -*-
"""K 线门面:东财优先,失败自动切腾讯/新浪。

东财 push2his 在高频请求后会直接断连(冷却几分钟),所以必须有备用源;
三家的日线都是「日期 + OHLC + 成交量」,量纲统一成手(1 手=100 股)。
"""
from __future__ import annotations

import datetime as dt
import time

from ..utils import normalize_code, safe_float, sina_symbol
from . import em, http


def _mark(name: str, ok: bool, note: str = "") -> None:
    """记录数据源健康度。惰性导入:import 期间并发加载 health 时不会拿不到名字。"""
    try:
        from . import health
        health.mark(name, ok, note)
    except Exception:
        pass

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_K_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "CN_MarketData.getKLineData")

_KL_MAP = {"day": 101, "week": 102, "month": 103}


def _tx_kline(sym: str, count: int, period: str = "day", fqt: str = "qfq",
              timeout: float = 6.0) -> list[dict]:
    js = http.get_json(TX_URL, params={"param": f"{sym},{period},,,{count},{fqt}"},
                       retries=1, timeout=timeout)
    node = ((js or {}).get("data") or {}).get(sym) or {}
    rows = node.get(f"{fqt}{period}") or node.get(period) or []
    out = []
    for r in rows:
        if len(r) < 6:
            continue
        out.append({
            "datetime": r[0], "open": safe_float(r[1]), "close": safe_float(r[2]),
            "high": safe_float(r[3]), "low": safe_float(r[4]),
            "volume": safe_float(r[5]),          # 腾讯日线成交量单位:手
            "amount": safe_float(r[6]) if len(r) > 6 and r[6] not in ("", None) else None,
        })
    return out


def _sina_kline(sym: str, count: int, timeout: float = 6.0) -> list[dict]:
    txt = http.get_text(SINA_K_URL, params={"symbol": sym, "scale": 240,
                                            "ma": "no", "datalen": count},
                        headers={"Referer": "https://finance.sina.com.cn/"},
                        retries=1, timeout=timeout)
    try:
        rows = http.parse_jsonp(txt)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        out.append({
            "datetime": r.get("day"), "open": safe_float(r.get("open")),
            "close": safe_float(r.get("close")), "high": safe_float(r.get("high")),
            "low": safe_float(r.get("low")),
            "volume": (safe_float(r.get("volume")) or 0) / 100.0,  # 新浪返回股 → 手
            "amount": None,
        })
    return out


_MEMO: dict[tuple, tuple[float, list[dict], str]] = {}
_MEMO_TTL = 90.0          # 进程内缓存:切来切去不用再联网
_CACHE_TTL = 1800.0       # SQLite 缓存:半小时内直接用(日线盘中变化很小)


def _cache_get(code: str, klt: int, fqt: int, count: int,
               want_last: str | None = None) -> list[dict]:
    """读 SQLite 缓存。want_last 给了就校验末根日期。

    以前这里只看「根数够不够」,不看「新不新」—— 结果一只票只要成功取过
    一次,之后永远读缓存,不会再联网。横截面选股时 K 线日期参差不齐
    (有的 11-13、有的 11-18),排名矩阵最后一行大量 NaN,
    pick() 只取 iloc[-1] 就只能选出 1 只票。_CACHE_TTL 那会儿定义了没用上。
    """
    try:
        from .store import store
        rows = store().load_kline(code, klt, fqt)
        if not rows or len(rows) < min(count, 20):
            return []
        if want_last:
            have = str(rows[-1].get("datetime") or "")[:10]
            golf = str(want_last)[:10]
            if have < golf:
                return []          # 太旧,回源
        return rows[-count:]
    except Exception:
        pass
    return []


def _last_trade_day() -> str:
    """今天「应该」能拿到的最新日线日期(YYYY-MM-DD)。

    收盘后 15:10 才有当天日线,盘中拿到的末根是上一交易日。
    粗略按工作日算,不查交易日历 —— 判断缓存新不新够用了,
    差一天也只会多重取一次,不会拿陈旧数据选股。
    """
    now = dt.datetime.now()
    d = dt.date.today()
    if now.hour * 60 + now.minute < 15 * 60 + 10:
        d -= dt.timedelta(days=1)          # 盘中:末根是上一交易日
    while d.weekday() >= 5:                # 跳过周末
        d -= dt.timedelta(days=1)
    return d.isoformat()


_complete_day_cache: dict[int, tuple[float, str]] = {}


def complete_day(min_codes: int = 3000, ttl: float = 300.0) -> str:
    """最近一个「覆盖数足够」的交易日(YYYY-MM-DD)。

    盘中的今天只有十几只票落库,拿它当完整日线会出三类错:
    自检误报覆盖不足、审计拿残缺数据审一整天、补数把五千多只全判成落后。
    统一走这里判断,别各自写 max(bar_time)。

    ttl 秒内复用上次结果 —— 这个查询是 5 天 group by,约 1.7 秒。
    """
    import time as _t
    now = _t.time()
    hit = _complete_day_cache.get(int(min_codes))
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        from .store import store
        rows = store().conn.execute(
            "select bar_time, count(*) n from kline_cache where klt=101 and fqt=1 "
            "group by bar_time order by bar_time desc limit 8").fetchall()
        out = ""
        for r in rows:
            if int(r["n"] or 0) >= int(min_codes):
                out = str(r["bar_time"])
                break
        _complete_day_cache[int(min_codes)] = (now, out)
        return out
    except Exception:  # noqa: BLE001
        return ""


def _cache_put(code: str, klt: int, fqt: int, bars: list[dict]) -> None:
    try:
        from .store import store
        store().save_kline(code, klt, fqt, bars)
    except Exception:
        pass


def get_kline(code: str, klt: int = 101, count: int = 300, fqt: int = 1,
              source: str = "auto", is_index: bool = False) -> tuple[list[dict], str]:
    """返回 (bars, 实际来源)。

    速度优先:进程内 memo → SQLite → 腾讯(最快最稳)→ 东财(只试一次)→ 新浪。
    东财被限流时会先卡重试,所以放在腾讯后面,避免每次点开都等十几秒。
    """
    from ..utils import index_symbol
    code = normalize_code(code)
    sym = index_symbol(code) if is_index else sina_symbol(code)
    # 缓存键:指数加前缀,避免和同号个股互相覆盖
    cache_code = ("idx:" + code) if is_index else code
    key = (cache_code, klt, fqt, count)

    hit = _MEMO.get(key)
    if hit and time.monotonic() - hit[0] < _MEMO_TTL:
        return hit[1], hit[2]

    # 缓存必须新到「上一交易日」。只数根数不看日期的话,一只票取成功过一次
    # 就再也不回源,横截面选股会拿到一堆末根日期各不相同的面板。
    want_last = _last_trade_day() if klt == 101 else None
    cached = _cache_get(cache_code, klt, fqt, count, want_last=want_last)
    # 缓存里可能只有最近 190 根(以前为省空间只存短窗口),
    # 请求 520 根时必须回源,否则历史区间会被切空。
    if cached and len(cached) >= max(60, int(count * 0.8)):
        _MEMO[key] = (time.monotonic(), cached, "cache")
        _mark("K线", True, f"缓存 {len(cached)} 根")
        return cached, "cache"

    order = ["tx", "em", "sina"] if source == "auto" else [source]
    last_err = None
    for src in order:
        try:
            if src == "tx":
                if klt not in (101, 102, 103):
                    continue
                period = {101: "day", 102: "week", 103: "month"}[klt]
                bars = _tx_kline(sym, count, period, "qfq" if fqt == 1 else "")
                if bars:
                    _MEMO[key] = (time.monotonic(), bars, "tx")
                    _cache_put(cache_code, klt, fqt, bars)
                    _mark("K线", True, f"腾讯 {len(bars)} 根")
                    return bars, "tx"
                continue
            if src == "em":
                bars = em.kline(code, klt, count=count, fqt=fqt, is_index=is_index,
                                retries=1, timeout=6.0)
                if bars:
                    _MEMO[key] = (time.monotonic(), bars, "em")
                    _cache_put(cache_code, klt, fqt, bars)
                    _mark("K线", True, f"东财 {len(bars)} 根")
                    return bars, "em"
                continue
            if klt not in (101, 102, 103):
                continue
            bars = _sina_kline(sym, count)
            if bars:
                _MEMO[key] = (time.monotonic(), bars, "sina")
                _cache_put(cache_code, klt, fqt, bars)
                _mark("K线", True, f"新浪 {len(bars)} 根")
                return bars, "sina"
        except Exception as e:  # noqa: BLE001
            last_err = e
    return [], str(last_err or "no source")


def daily(code: str, count: int = 300) -> list[dict]:
    return get_kline(code, 101, count)[0]


TX_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"


def tx_minute(code: str) -> dict:
    """腾讯分时(备用):返回 {pre_close, bars:[{t, price, avg, volume, amount}]}。

    腾讯每行是「HHMM 价格 累计量(手) 累计额(元)」,所以要差分成分钟量,
    均价 = 累计额 / (累计量×100)。之前把累计量当分钟量累加,均价线是错的。
    """
    sym = sina_symbol(code)
    try:
        js = http.get_json(TX_MINUTE, params={"code": sym})
    except Exception:
        return {"pre_close": None, "bars": []}
    node = ((js or {}).get("data") or {}).get(sym) or {}
    inner = node.get("data") or {}
    date = inner.get("date") or ""
    rows = inner.get("data") or []
    qt = (node.get("qt") or {}).get(sym) or []
    pre = safe_float(qt[4]) if len(qt) > 4 else None      # qt[4] = 昨收
    bars: list[dict] = []
    prev_vol = prev_amt = 0.0
    for line in rows:
        parts = str(line).split()
        if len(parts) < 3:
            continue
        hm, price = parts[0], safe_float(parts[1])
        cum_vol = safe_float(parts[2]) or 0.0
        cum_amt = safe_float(parts[3]) if len(parts) > 3 else 0.0
        vol = max(0.0, cum_vol - prev_vol)
        amt = max(0.0, (cum_amt or 0.0) - prev_amt)
        prev_vol, prev_amt = cum_vol, cum_amt or 0.0
        avg = (cum_amt / (cum_vol * 100.0)) if cum_vol and cum_amt else None
        t = f"{date[:4]}-{date[4:6]}-{date[6:8]} {hm[:2]}:{hm[2:]}" if len(date) == 8 else hm
        bars.append({"t": t, "price": price, "avg": avg, "volume": vol, "amount": amt})
    return {"pre_close": pre, "bars": bars, "source": "tx"}
