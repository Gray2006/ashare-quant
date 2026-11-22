# -*- coding: utf-8 -*-
"""资金流统一取数层:一条缓存 + 批量优先 + 并行兜底 + 负缓存。

为什么单独一层:

  之前资金流散在 realtime._fetch_flow、em.moneyflow_now、macro 抽样三处,
  各自串行逐只请求 —— 8 只自选要 8 次同花顺分时,首屏 4 秒才出数字;
  东财挂掉时每轮还要先白等一次连接超时。这里收口成一条链路:

    1) 批量优先:东财 ulist 一次请求带 f62,健康时 1 次覆盖全部代码;
    2) 并行兜底:东财不可用时,同花顺/新浪/腾讯按只并行,整轮有截止时间,
       过时就把已经拿到的先给界面,不再等最慢的那只;
    3) 单一缓存:main/pct/source/ts,TTL 跟 config.refresh_seconds.moneyflow,
       行情自带的 f62 直接回填缓存,不重复请求;
    4) 负缓存:取不到的票 90 秒内不再重试,停牌股不会每轮拖慢整批;
    5) 熔断感知:东财在冷却期直接跳过,不再每次先吃一次连接失败。

口径:
  main = 主力净流入 = 东财 f62 / 同花顺分时 / 新浪 r0+r1 / 腾讯分时近似。
  source 字段如实标注来源,界面按来源提示,不混口径。
"""
from __future__ import annotations

import threading
import time

from ..utils import normalize_code
from . import em, http, sina, txf

_TTL_FLOOR = 5.0
_TTL_DEFAULT = 20.0
_DISPLAY_TTL = 600.0     # 缓存最多展示 10 分钟前的值(行情停了也还能看)
_FAIL_TTL = 90.0         # 取不到的票,90 秒内不再打接口
_INTRA_TTL = 12.0        # 分时资金流曲线缓存
_DAILY_TTL = 300.0       # 日频资金流(区间合计用)缓存
_RANK_TTL = 20.0         # 榜单缓存
_MAX_PARALLEL = 12       # 单轮最多并行补多少只
_BG_DEADLINE = 5.0       # 后台整轮的截止时间
_SYNC_DEADLINE = 1.2     # 同步补数(脚本/自检)的截止时间

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_fail: dict[str, float] = {}
_intra: dict[str, tuple[float, dict]] = {}
_daily: dict[str, tuple[float, list]] = {}
# 每个代码最近一次日频数据的来源。不写进 bar —— bar 会被 pandas 当数值列,
# 多一个字符串键会让因子计算整列变 NaN。
_day_src: dict[str, str] = {}
_rank: dict[tuple, tuple[float, list]] = {}
_busy = False
_rr = 0                  # 轮转游标:自选超过单轮上限时,保证每只都轮得到

# 东财本地熔断:每次失败要白等 0.7~1.3 秒,连续两次失败就整段跳过,
# 比只靠 http 层「三连败才冷却 90 秒」更省 —— 资金流每 20 秒就要取一次。
_EM_ULIST = em.PUSH2 + "/api/qt/ulist.np/get"
_EM_FFLOW = em.PUSH2 + "/api/qt/stock/fflow/kline/get"
_EM_DAYFLOW = em.PUSH2HIS + "/api/qt/stock/fflow/daykline/get"
_EM_CLIST = em.PUSH2 + "/api/qt/clist/get"
_EM_DEAD = 120.0
_em_dead: dict[str, float] = {}
_em_fails: dict[str, int] = {}


def _em_ok(url: str) -> bool:
    """东财这个接口现在能不能试:本地熔断 + http 层冷却都过了才行。"""
    if time.monotonic() < _em_dead.get(url, 0.0):
        return False
    return not http.is_cooling(url)


def _em_result(url: str, ok: bool) -> None:
    with _lock:
        if ok:
            _em_fails[url] = 0
            _em_dead.pop(url, None)
            return
        n = _em_fails.get(url, 0) + 1
        _em_fails[url] = n
        if n >= 2:
            _em_dead[url] = time.monotonic() + _EM_DEAD

_stat = {"batches": 0, "requests": 0, "fallback": 0, "miss": 0,
         "rank_calls": 0, "intra_calls": 0, "last_ms": 0.0}


# ---------------------------------------------------------------- 配置 / 统计

def ttl() -> float:
    """资金流缓存时长:跟界面「资金流刷新」配置走,最低 5 秒。"""
    try:
        from ..config import cfg
        return max(_TTL_FLOOR, float(cfg().get("refresh_seconds.moneyflow", _TTL_DEFAULT)))
    except Exception:  # noqa: BLE001
        return _TTL_DEFAULT


def _bump(key: str, n: int = 1) -> None:
    with _lock:
        _stat[key] = _stat.get(key, 0) + n


def stats() -> dict:
    """取数统计:界面/自检看「打了多少次接口、命中多少」。"""
    now = time.time()
    with _lock:
        out = dict(_stat)
        cached = len(_cache)
        fresh = sum(1 for v in _cache.values() if now - v.get("ts", 0) <= ttl())
        fails = sum(1 for until in _fail.values() if until > now)
    out.update({"cached": cached, "fresh": fresh, "failed": fails, "ttl": ttl()})
    return out


def reset() -> None:
    """清空全部缓存与统计(自检/探针用)。"""
    global _busy, _rr
    with _lock:
        _cache.clear()
        _fail.clear()
        _intra.clear()
        _daily.clear()
        _rank.clear()
        _em_dead.clear()
        _em_fails.clear()
        _busy = False
        _rr = 0
        for k in _stat:
            _stat[k] = 0


# ---------------------------------------------------------------- 缓存读写

def _record(main: float | None, pct: float | None, src: str,
            super_: float | None = None, big: float | None = None,
            inflow: float | None = None, outflow: float | None = None) -> dict:
    return {"main": main, "pct": pct, "src": src, "super": super_, "big": big,
            "in": inflow, "out": outflow, "ts": time.time()}


def _put(code: str, rec: dict) -> None:
    with _lock:
        _cache[code] = rec
        _fail.pop(code, None)


def peek(code: str, max_age: float | None = None) -> dict | None:
    """只读缓存,不触发请求。"""
    code = normalize_code(code)
    age = ttl() if max_age is None else float(max_age)
    with _lock:
        hit = _cache.get(code)
        if hit and time.time() - hit.get("ts", 0) <= age:
            return dict(hit)
    return None


def _public(rec: dict | None) -> dict:
    if not rec:
        return {}
    return {"main": rec.get("main"), "pct": rec.get("pct"),
            "source": rec.get("src"), "ts": rec.get("ts"),
            "super": rec.get("super"), "big": rec.get("big"),
            "inflow": rec.get("in"), "outflow": rec.get("out")}


def _mark_fail(code: str) -> None:
    with _lock:
        _fail[code] = time.time() + _FAIL_TTL
        _stat["miss"] = _stat.get("miss", 0) + 1


def _is_failed(code: str) -> bool:
    with _lock:
        return _fail.get(code, 0.0) > time.time()


# ---------------------------------------------------------------- 单只取数

def _from_em_batch(codes: list[str]) -> dict[str, dict]:
    """东财批量 f62:一次请求覆盖全部代码。熔断/冷却期直接返回空,不占用时间。"""
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return {}
    if not _em_ok(_EM_ULIST):
        return {}
    _bump("requests")
    try:
        raw = em.quotes(codes, fallback=False, retries=1, timeout=4.0)
    except Exception:  # noqa: BLE001
        _em_result(_EM_ULIST, False)
        return {}
    out: dict[str, dict] = {}
    for code, q in (raw or {}).items():
        if not isinstance(q, dict) or q.get("main_inflow") is None:
            continue
        rec = _record(float(q["main_inflow"]), q.get("main_ratio"), "em")
        _put(code, rec)
        out[code] = rec
    _em_result(_EM_ULIST, bool(out))
    return out


def _fetch_one(code: str, deadline: float | None = None) -> dict:
    """单只按 同花顺 → 新浪 → 腾讯 顺序兜底。到截止时间就放弃剩余来源。"""
    code = normalize_code(code)
    for mod in (sina, txf):
        if deadline is not None and time.monotonic() >= deadline:
            break
        _bump("requests")
        try:
            d = mod.moneyflow(code) or {}
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("main") is not None:
            rec = _record(float(d["main"]), d.get("pct"),
                          d.get("source") or mod.__name__.rsplit(".", 1)[-1],
                          d.get("super"), d.get("big"),
                          d.get("inflow"), d.get("outflow"))
            _put(code, rec)
            return rec
    _mark_fail(code)
    return {}


def _fetch_chunk(codes: list[str], deadline: float) -> dict[str, dict]:
    """一小批并行取数:每条一个守护线程,到点先返回手上有的。"""
    out: dict[str, dict] = {}
    out_lock = threading.Lock()

    def work(c: str) -> None:
        rec = _fetch_one(c, deadline)
        if rec:
            with out_lock:
                out[c] = rec

    threads = [threading.Thread(target=work, args=(c,), daemon=True,
                                name=f"mf-{c}") for c in codes]
    for i, t in enumerate(threads):
        t.start()
        # 同花顺的限速桶按路径分(bucket 里带股票代码),多个线程会同时开火;
        # 起步错开 0.06 秒:并发不丢,又不至于八个请求同一瞬间打到同花顺
        if i < len(threads) - 1:
            time.sleep(0.06)
    for t in threads:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        t.join(left)
    with out_lock:
        return dict(out)


def _rotate(items: list[str], limit: int) -> list[str]:
    """从轮转游标处切一段,保证大自选池里每只都会轮到。"""
    global _rr
    n = len(items)
    with _lock:
        start = _rr % n
        _rr = (start + limit) % n
    return (items[start:] + items[:start])[:limit]


def batch(codes: list[str], deadline: float = _BG_DEADLINE,
          force: bool = False) -> dict[str, dict]:
    """批量取资金流:缓存优先 → 东财一次批量 → 剩余并行兜底。

    返回 {code: {main, pct, source, ts}};取不到的 code 不在结果里。
    """
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return {}
    now = time.time()
    t = ttl()
    need: list[str] = []
    with _lock:
        for c in codes:
            hit = _cache.get(c)
            if hit and not force and now - hit.get("ts", 0) <= t:
                continue
            if _is_failed(c) and not force:
                continue
            need.append(c)
    if not need:
        with _lock:
            return {c: dict(_cache[c]) for c in codes if c in _cache}

    _bump("batches")
    t0 = time.perf_counter()
    got = _from_em_batch(need)
    rest = [c for c in need if c not in got]
    if rest:
        _bump("fallback", len(rest))
        end = time.monotonic() + max(0.4, float(deadline))
        for i in range(0, len(rest), _MAX_PARALLEL):
            if time.monotonic() >= end:
                break
            got.update(_fetch_chunk(rest[i:i + _MAX_PARALLEL], end))
    _stat["last_ms"] = (time.perf_counter() - t0) * 1000
    with _lock:
        return {c: dict(_cache[c]) for c in codes if c in _cache}


def now(code: str, fast: bool = False, deadline: float = 2.0) -> dict:
    """单只资金流。fast=True 跳过东财(批量抽样时省一次白等)。"""
    code = normalize_code(code)
    hit = peek(code)
    if hit:
        return _public(hit)
    if _is_failed(code):
        return {}
    rec = {} if fast else _from_em_batch([code]).get(code, {})
    if not rec:
        rec = _fetch_one(code, time.monotonic() + max(0.3, float(deadline)))
    return _public(rec)


# ---------------------------------------------------------------- 贴回行情

def paste(quotes: dict, max_age: float = _DISPLAY_TTL) -> int:
    """只把缓存贴进行情字典(零请求)。返回贴了几只。"""
    now = time.time()
    filled = 0
    with _lock:
        for code, q in quotes.items():
            if not isinstance(q, dict) or q.get("main_inflow") is not None:
                continue
            hit = _cache.get(normalize_code(code))
            if not hit or now - hit.get("ts", 0) > max_age:
                continue
            q["main_inflow"] = hit.get("main")
            if hit.get("pct") is not None:
                q["main_ratio"] = hit.get("pct")
            q["_mf_src"] = hit.get("src")
            filled += 1
    return filled


def _harvest(quotes: dict) -> None:
    """行情自带 f62(东财快照)时直接回填缓存,不用再为资金流多打一次接口。"""
    for code, q in quotes.items():
        if not isinstance(q, dict):
            continue
        v = q.get("main_inflow")
        if v is None or q.get("_mf_src"):
            # _mf_src 是 paste 打上的标记,说明这个值是缓存贴回来的,不能再当新数据
            continue
        src = str(q.get("source") or "em")
        if src in ("tx", "sina"):
            continue
        _put(normalize_code(code), _record(float(v), q.get("main_ratio"), src))


def apply(quotes: dict, max_age: float | None = None, wait: bool = False,
          on_update=None) -> None:
    """把资金流补进行情字典。

    先贴缓存(零等待),再把没命中/过期的丢给后台线程并行补。
    wait=True 时同步补一批(脚本与自检用),界面路径永远不等。
    """
    global _busy
    if not quotes:
        return
    age = ttl() if max_age is None else float(max_age)
    _harvest(quotes)
    paste(quotes)

    now_ts = time.time()
    stale: list[str] = []
    with _lock:
        for code, q in quotes.items():
            if not isinstance(q, dict):
                continue
            hit = _cache.get(normalize_code(code))
            if hit and now_ts - hit.get("ts", 0) <= age:
                continue
            if _is_failed(code):
                continue
            stale.append(normalize_code(code))
    if not stale:
        return
    if len(stale) > _MAX_PARALLEL:
        # 自选超过 12 只时轮着补:否则排在后面的永远轮不到
        stale = _rotate(stale, _MAX_PARALLEL)
    else:
        stale = stale[:_MAX_PARALLEL]

    if wait:
        _busy = True
        try:
            batch(stale, deadline=_SYNC_DEADLINE)
            paste(quotes)
        finally:
            _busy = False
        return

    if _busy:
        return
    _busy = True

    def worker() -> None:
        global _busy
        try:
            batch(stale, deadline=_BG_DEADLINE)
            # 取到就贴回调用方给的字典:界面不必等下一轮行情也能刷新
            paste(quotes)
            if on_update:
                try:
                    on_update()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            _busy = False

    threading.Thread(target=worker, name="moneyflow", daemon=True).start()


def snapshot() -> dict[str, dict]:
    """当前缓存快照(只读)。"""
    with _lock:
        return {c: dict(v) for c, v in _cache.items()}


# ---------------------------------------------------------------- 分时曲线

def intraday(code: str, force: bool = False) -> dict:
    """个股当日主力资金分时曲线。

    东财 fflow → 同花顺分时 → 腾讯分时,带 12 秒缓存。
    东财在冷却期不再返回空,而是直接走备用源(之前这里会让图表整段空白)。
    """
    code = normalize_code(code)
    now_ts = time.time()
    with _lock:
        hit = _intra.get(code)
        if hit and not force and now_ts - hit[0] < _INTRA_TTL:
            return hit[1]
    _bump("intra_calls")
    name = None
    bars: list[dict] = []
    source = ""
    if _em_ok(_EM_FFLOW):
        _bump("requests")
        bars, name = em.fflow_intraday(code)
        _em_result(_EM_FFLOW, bool(bars))
        if bars:
            source = "em"
    if not bars:
        for mod in (txf,):
            _bump("requests")
            try:
                alt = mod.intraday_flow(code) or {}
            except Exception:  # noqa: BLE001
                alt = {}
            if alt.get("bars"):
                bars = alt["bars"]
                source = alt.get("source") or mod.__name__.rsplit(".", 1)[-1]
                break
    out = {"name": name, "bars": bars, "source": source}
    if bars:
        with _lock:
            _intra[code] = (time.time(), out)
    return out


def gross_today(code: str) -> dict:
    """今日真实买卖毛额(新浪 in/out)。缓存里有就直接用,没有才打一次。

    东财 f62 只有净额,所以「今日」想给流入/流出两个真数,只能走新浪这条。
    取不到返回 {},调用方退回「净额正负拆分」的口径。
    """
    code = normalize_code(code)
    rec = peek(code, max_age=_DISPLAY_TTL) or {}
    if rec.get("in") is not None and rec.get("out") is not None:
        return rec
    _bump("requests")
    try:
        d = sina.moneyflow(code) or {}
    except Exception:  # noqa: BLE001
        return {}
    if d.get("main") is None:
        return {}
    rec = _record(float(d["main"]), None, "sina", d.get("super"), d.get("big"),
                  d.get("inflow"), d.get("outflow"))
    _put(code, rec)
    return rec


# ---------------------------------------------------------------- 日频 / 区间合计

def daily(code: str, days: int = 12, force: bool = False) -> list[dict]:
    """个股日频资金流(东财 daykline)。5 分钟缓存,冷却期直接给缓存不给空。

    每条:{date, main, small, mid, big, super, main_ratio},main 单位元。
    东财只给「净额」,没有买卖毛额 —— 想要「流入/流出」只能按每日净额正负拆。
    """
    code = normalize_code(code)
    want = max(8, int(days))
    now_ts = time.time()
    with _lock:
        hit = _daily.get(code)
        if hit and not force and now_ts - hit[0] < _DAILY_TTL and len(hit[1]) >= min(want, 5):
            return [dict(b) for b in hit[1]]
        stale = [dict(b) for b in hit[1]] if hit else []
    bars: list[dict] = []
    src = ""
    if _em_ok(_EM_DAYFLOW):
        _bump("requests")
        try:
            bars = em.moneyflow_history(code, days=want)
        except Exception:  # noqa: BLE001
            bars = []
        _em_result(_EM_DAYFLOW, bool(bars))
        if bars:
            src = "em"
    if not bars:
        # 东财 push2his 在这台机器上整段断连,新浪的逐日资金流是稳定备胎。
        # 注意:熔断期也必须来这里,不能一上来就 return(当初就是那样,深市全空)
        _bump("requests")
        try:
            bars = sina.moneyflow_history(code, num=want)
        except Exception:  # noqa: BLE001
            bars = []
        if bars:
            src = "sina"
    if bars:
        bars = bars[-max(want, 8):]      # 新浪不理 num,会回全历史,截断再缓存
        with _lock:
            _daily[code] = (time.time(), [dict(b) for b in bars])
            if src:
                _day_src[code] = src     # 来源记在旁边,不写进 bar
        return bars
    return stale


def window(code: str, days: int = 3, force: bool = False) -> dict:
    """近 N 个交易日的资金流合计。

    total   区间主力净流入(元)
    inflow  净流入日的合计(元,正)
    outflow 净流出日的合计(元,取正值)
    bars    参与合计的每日明细
    """
    n = max(1, int(days))
    bars = daily(code, days=max(12, n + 3), force=force)
    tail = bars[-n:] if bars else []
    vals = [float(b.get("main") or 0.0) for b in tail]
    # 今日口径能拿到真实买卖毛额(新浪 in/out),区间口径只有净额,得按正负拆
    gross_in = gross_out = None
    if n == 1:
        rec = gross_today(code)
        gi, go = rec.get("in"), rec.get("out")
        if gi is not None and go is not None:
            gross_in, gross_out = float(gi), float(go)
    return {
        "code": normalize_code(code),
        "days": n,
        "bars": tail,
        "count": len(tail),
        "total": sum(vals),
        "inflow": sum(v for v in vals if v > 0),
        "outflow": sum(-v for v in vals if v < 0),
        "up_days": sum(1 for v in vals if v > 0),
        "down_days": sum(1 for v in vals if v < 0),
        "gross_in": gross_in,
        "gross_out": gross_out,
        "gross": gross_in is not None and gross_out is not None,
        "source": (_day_src.get(normalize_code(code))
                   or (str(tail[-1].get("source") or "") if tail else "")),
    }


# ---------------------------------------------------------------- 批量区间合计

def batch_window(codes: list[str], days: int = 3, deadline: float = 10.0,
                 max_parallel: int = 8, on_progress=None) -> dict[str, dict]:
    """多只票的近 N 日资金流合计(榜单按区间重排用)。

    日频已有缓存的不再请求;缺的并行补,超过 deadline 就先返回手上有的。
    on_progress(done, total) 在后台线程里回调,界面要自己切回主线程。
    """
    days = max(1, int(days))
    codes = [normalize_code(c) for c in codes if c]
    total = len(codes)
    if not codes:
        return {}
    end = time.monotonic() + max(1.0, float(deadline))
    out: dict[str, dict] = {}
    out_lock = threading.Lock()
    counter = {"n": 0}

    def work(c: str) -> None:
        try:
            w = window(c, days)
        except Exception:  # noqa: BLE001
            w = {}
        with out_lock:
            counter["n"] += 1
            done = counter["n"]
            if w.get("count"):
                out[c] = w
        if on_progress:
            try:
                on_progress(done, total)
            except Exception:  # noqa: BLE001
                pass

    threads: list[threading.Thread] = []
    for c in codes:
        if time.monotonic() >= end:
            break
        # 简单并发闸:活着的线程到 max_parallel 就先等一等
        while sum(1 for t in threads if t.is_alive()) >= max_parallel:
            if time.monotonic() >= end:
                break
            time.sleep(0.02)
        t = threading.Thread(target=work, args=(c,), daemon=True, name=f"mfwin-{c}")
        threads.append(t)
        t.start()
    for t in threads:
        left = end - time.monotonic()
        if left <= 0:
            break
        t.join(left)
    with out_lock:
        return dict(out)


# ---------------------------------------------------------------- 榜单

def rank(top: int = 50, asc: bool = False, force: bool = False) -> list[dict]:
    """主力净流入榜。东财 clist → 新浪 MoneyFlow 榜(超大单口径)。"""
    key = ("rank", int(top), bool(asc))
    now_ts = time.time()
    with _lock:
        hit = _rank.get(key)
        if hit and not force and now_ts - hit[0] < _RANK_TTL:
            return [dict(r) for r in hit[1]]
    _bump("rank_calls")
    rows: list[dict] = []
    if _em_ok(_EM_CLIST):
        _bump("requests")
        try:
            rows = em.rank_flow(top, asc, retries=1)
        except Exception:  # noqa: BLE001
            rows = []
        _em_result(_EM_CLIST, bool(rows))
    for r in rows:
        r.setdefault("source", "em")
    if not rows:
        _bump("requests")
        try:
            rows = sina.moneyflow_rank(top, asc)
        except Exception:  # noqa: BLE001
            rows = []
    if rows:
        with _lock:
            _rank[key] = (time.time(), [dict(r) for r in rows])
    return rows


def sectors(top: int = 30, force: bool = False) -> list[dict]:
    """行业板块资金流。东财独有,冷却期直接返回空(不再白等 6 秒)。"""
    key = ("sector", int(top))
    now_ts = time.time()
    with _lock:
        hit = _rank.get(key)
        if hit and not force and now_ts - hit[0] < _RANK_TTL:
            return [dict(r) for r in hit[1]]
    if not _em_ok(_EM_CLIST):
        return []
    _bump("requests")
    try:
        rows = em.sector_flow(top, retries=1)
    except Exception:  # noqa: BLE001
        rows = []
    _em_result(_EM_CLIST, bool(rows))
    if rows:
        with _lock:
            _rank[key] = (time.time(), [dict(r) for r in rows])
    return rows
