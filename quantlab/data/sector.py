# -*- coding: utf-8 -*-
"""板块资金流:行业 / 概念 / 地域 榜单 + 成分股 + 板块分时/日K + 个股所属板块。

数据源分工(2026-11 实测,东财 push2 的 /api/qt/clist/get 全线 RST,不可用):

  榜单   新浪 MoneyFlow.ssl_bkzj_bk(fenlei 0 行业 / 1 概念)
         给真实流入、流出、净额、净占比、领涨股,且 category 就是成分股 node,
         所以「榜 → 成分股」这条链路 100% 通。
  地域   腾讯 getRank(board_type=dy)。新浪没有地域节点,腾讯地域也没有成分源,
         成分区会明说「无数据源」,不编造。
  分时   腾讯 minute/query?code=pt… 需要腾讯 pt 码,新浪板块名对得上才有,否则提示。
  日K    腾讯 newfqkline(newfqkline 才有历史,fqkline 对 pt 码只回一根)。
  个股   东财 slist spt=3,一次请求拿全该股所属板块(BK 码 + 主力净额 + 净占比)。

所有对外函数返回 plain dict/list,界面层只负责展示。
"""
from __future__ import annotations

import json
import threading
import time

from ..config import APP_DIR
from ..utils import normalize_code, safe_float, secid
from . import em, http

KINDS = ("hy", "gn", "dy")
KIND_LABEL = {"hy": "行业", "gn": "概念", "dy": "地域"}

_SINA_BK = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_bkzj_bk")
_SINA_NODE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "Market_Center.getHQNodeData")
_TX_RANK = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
_TX_MIN = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
_TX_KLINE = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_EM_SLIST = em.PUSH2 + "/api/qt/slist/get"

_SINA_HDR = {"Referer": "https://finance.sina.com.cn/"}
_TX_HDR = {"Referer": "https://stockapp.finance.qq.com/"}

_TTL_BOARD = 20.0
_TTL_CONS = 300.0         # 成分股名单变化很慢,5 分钟内不用重拉(概念板块最多 4 页)
_TTL_OF = 90.0            # 个股「所属板块」跟着资金流走,90 秒
_TTL_MIN = 15.0
_TTL_K = 300.0
_TTL_MAP = 600.0          # 新浪板块名 → 腾讯 pt 码 的映射表,建一次用 10 分钟
_TTL_FAIL = 6.0           # 取空时短缓存,别让界面每轮都去打一遍

_lock = threading.RLock()
_cache: dict[str, tuple[float, object]] = {}
_build_locks: dict[str, threading.Lock] = {}

# 地域名(腾讯 dy 板块名就是省/市名,东财 slist 给的是「贵州板块」)
_REGION_HINT = {
    "北京", "上海", "天津", "重庆", "广东", "江苏", "浙江", "山东", "河南", "河北",
    "湖南", "湖北", "四川", "福建", "安徽", "江西", "山西", "陕西", "云南", "贵州",
    "广西", "海南", "辽宁", "吉林", "黑龙江", "内蒙古", "宁夏", "甘肃", "青海",
    "新疆", "西藏", "深圳", "广州", "杭州", "南京", "武汉", "成都", "西安", "苏州",
}
_STYLE_HINT = ("重仓", "融资融券", "深股通", "沪股通", "MSCI", "富时", "标普", "证金",
               "汇金", "社保", "QFII", "预盈", "预增", "预减", "破净", "低价", "高价",
               "次新", "送转", "举牌", "增持", "回购", "激励", "员工持股", "定增",
               "创投", "参股", "摘帽", "ST", "转债", "AH股", "B股", "GDR",
               "成长", "价值", "市盈率", "市净率", "热股", "多板", "振幅", "龙头",
               "权重", "活跃", "涨停", "跌停", "百元股", "大盘", "中盘", "小盘",
               "风格", "指数", "股", "增仓", "减仓", "普尔", "道琼斯")


def _memo(key: str, ttl: float, fn, force: bool = False):
    """带 TTL 的缓存;取空(异常/无数据)只缓存 6 秒,避免把接口卡死。

    同一个 key 用一把锁串起来:界面里有好几条线程会同时要同一份数据
    (点板块时「分时」和「成分股」都要那张板块名→腾讯码的映射表),
    不锁就会并发重复构建 —— 实测 805 个概念被拉了两遍,12 次请求。
    """
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < hit[1]:
            return hit[2]
        flock = _build_locks.setdefault(key, threading.Lock())
    with flock:
        with _lock:                      # 抢到锁再看一眼,别人可能刚建好
            hit = _cache.get(key)
            if hit and not force and time.monotonic() - hit[0] < hit[1]:
                return hit[2]
        val = fn()
        ttl_eff = ttl if val else _TTL_FAIL
        with _lock:
            _cache[key] = (time.monotonic(), ttl_eff, val)
        return val


def _ratio(main, inflow, outflow) -> float | None:
    """主力净占比 = 净额 / (流入 + 流出) × 100。"""
    if main is None:
        return None
    total = (inflow or 0.0) + (outflow or 0.0)
    if total <= 0:
        return None
    return main / total * 100.0


# ---------------------------------------------------------------- 榜单

def _boards_sina(kind: str) -> list[dict]:
    fenlei = {"hy": 0, "gn": 1}.get(kind)
    if fenlei is None:
        return []
    txt = http.get_text(_SINA_BK, params={"page": 1, "num": 400, "sort": "netamount",
                                          "asc": 0, "fenlei": fenlei},
                        headers=_SINA_HDR, retries=1)
    rows = http.parse_jsonp(txt)
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        node = str(r.get("category") or "")
        name = str(r.get("name") or "").strip()
        if not name and not node:
            continue
        chg = safe_float(r.get("avg_changeratio"))
        main = safe_float(r.get("netamount"))
        inflow = safe_float(r.get("inamount"))
        outflow = safe_float(r.get("outamount"))
        turnover = safe_float(r.get("turnover"))          # 亿元
        ts_chg = safe_float(r.get("ts_changeratio"))
        out.append({
            "key": f"sina:{node or name}",
            "kind": kind,
            "name": name or node,
            "node": node,
            "tx_code": "",
            "bk": "",
            "change_pct": chg * 100 if chg is not None else None,
            "main": main,
            "main_in": inflow,
            "main_out": outflow,
            "main_ratio": _ratio(main, inflow, outflow),
            "amount": turnover * 1e8 if turnover is not None else None,
            "up": None,
            "down": None,
            "lead_name": str(r.get("ts_name") or ""),
            "lead_pct": ts_chg * 100 if ts_chg is not None else None,
            "source": "sina",
        })
    return out


def _tx_page(kind: str, offset: int, count: int = 200) -> tuple[list[dict], int]:
    js = http.get_json(_TX_RANK, params={"board_type": kind, "sort_type": "price",
                                         "direct": "down", "offset": offset,
                                         "count": count},
                       headers=_TX_HDR, retries=1) or {}
    data = js.get("data") or {}
    return (data.get("rank_list") or []), int(data.get("total") or 0)


def _row_tx(r: dict, kind: str) -> dict | None:
    code = str(r.get("code") or "")
    name = str(r.get("name") or "").strip()
    if not name:
        return None

    def wan(key):                    # 腾讯资金字段单位是万元
        v = safe_float(r.get(key))
        return v * 1e4 if v is not None else None

    up = down = None
    zgb = str(r.get("zgb") or "")
    if "/" in zgb:
        a, b = zgb.split("/", 1)
        up, down = safe_float(a), safe_float(b)
    lzg = r.get("lzg") or {}
    main = wan("zljlr")
    inflow = wan("zllr")
    outflow = wan("zllc")
    return {
        "key": f"tx:{code or name}",
        "kind": kind,
        "name": name,
        "node": "",
        "tx_code": code,
        "bk": "",
        "change_pct": safe_float(r.get("zdf")),
        "main": main,
        "main_in": inflow,
        "main_out": outflow,
        "main_ratio": _ratio(main, inflow, outflow),
        "amount": wan("turnover"),
        "up": int(up) if up is not None else None,
        "down": int(down) if down is not None else None,
        "lead_name": str(lzg.get("name") or ""),
        "lead_pct": safe_float(lzg.get("zdf")),
        "main_5d": wan("zljlr_d5"),
        "main_20d": wan("zljlr_d20"),
        "source": "tx",
    }


def _boards_tx(kind: str) -> list[dict]:
    """腾讯板块榜。count 上限 200,概念 800+ 个必须分页。"""
    out: list[dict] = []
    offset = 0
    for _ in range(12):              # 防御性上限:概念最多 5 页
        rows, total = _tx_page(kind, offset)
        if not rows:
            break
        out += [x for x in (_row_tx(r, kind) for r in rows) if x]
        offset += len(rows)
        if offset >= total:
            break
    return out


def _tx_name_map() -> dict[str, str]:
    """新浪板块名 → 腾讯 pt 码。行业 + 概念一起建,模糊匹配兜底。

    两家分类口径不同(新浪行业 48 个 / 腾讯 31 个,概念 181 vs 805),
    只求「能对上的尽量对上」,对不上的板块分时会给明确提示。
    映射表落盘:建一次要 6 个请求(概念 805 个要翻 5 页),板块名几个月才变一次,
    没必要每次启动都重拉。
    """
    def build():
        mapping: dict[str, str] = {}
        for kind in ("hy", "gn"):
            for row in _boards_tx(kind):
                mapping.setdefault(row["name"], row["tx_code"])
        _save_map(mapping)
        return mapping

    disk = _load_map()
    if disk:
        return disk
    return _memo("txmap", _TTL_MAP, build) or {}


_MAP_FILE = APP_DIR / "data" / "sector_txmap.json"
_MAP_TTL = 7 * 86400.0     # 7 天


def _load_map() -> dict[str, str]:
    """读落盘的映射表,过期/坏了就当没有。"""
    try:
        if time.time() - _MAP_FILE.stat().st_mtime > _MAP_TTL:
            return {}
        data = json.loads(_MAP_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and len(data) > 100:
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_map(mapping: dict[str, str]) -> None:
    if len(mapping) < 100:
        return
    try:
        _MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MAP_FILE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _match_tx(name: str) -> str:
    """新浪板块名找腾讯 pt 码:精确 → 去后缀 → 包含。"""
    m = _tx_name_map()
    if not m:
        return ""
    n = (name or "").strip()
    if n in m:
        return m[n]
    for suffix in ("概念", "行业", "板块", "指数"):
        if n.endswith(suffix) and n[:-len(suffix)] in m:
            return m[n[:-len(suffix)]]
    for k, v in m.items():
        if len(k) >= 2 and (k in n or n in k):
            return v
    return ""


# 新浪行业(48,旧申万口径)→ 腾讯/申万一级行业(31)。两家分类粒度不同,
# 用别名表把「酿酒行业」这类名字接到申万一级「食品饮料」的板块指数上,
# 分时/日K 因此能画出来,但图上要标明是近似口径,不冒充原板块。
_ALIAS_HY = {
    "酿酒行业": "食品饮料", "食品行业": "食品饮料",
    "生物制药": "医药生物", "医疗器械": "医药生物",
    "家电行业": "家用电器",
    "电器行业": "电力设备", "发电设备": "电力设备",
    "石油行业": "石油石化",
    "电子器件": "电子", "电子信息": "电子",
    "机械行业": "机械设备", "仪器仪表": "机械设备", "纺织机械": "机械设备",
    "其它行业": "综合", "综合行业": "综合",
    "酒店旅游": "社会服务",
    "煤炭行业": "煤炭",
    "农药化肥": "基础化工", "化纤行业": "基础化工", "化工行业": "基础化工",
    "塑料制品": "基础化工",
    "建筑建材": "建筑材料", "玻璃行业": "建筑材料", "陶瓷行业": "建筑材料",
    "水泥行业": "建筑材料",
    "飞机制造": "国防军工", "船舶制造": "国防军工",
    "开发区": "房地产",
    "造纸行业": "轻工制造", "家具行业": "轻工制造", "印刷包装": "轻工制造",
    "公路桥梁": "建筑装饰",
    "物资外贸": "商贸零售", "商业百货": "商贸零售",
    "供水供气": "公用事业", "电力行业": "公用事业",
    "摩托车": "汽车", "汽车制造": "汽车",
    "纺织行业": "纺织服饰", "服装鞋类": "纺织服饰",
    "环保行业": "环保", "钢铁行业": "钢铁",
    "传媒娱乐": "传媒",
}


def _resolve_tx(board: dict) -> dict:
    """板块 → 腾讯 pt 码。approx 非空表示借用的是申万一级的近似板块。"""
    code = str(board.get("tx_code") or "")
    if code:
        return {"code": code, "approx": ""}
    name = str(board.get("name") or "").strip()
    m = _tx_name_map()
    alias = _ALIAS_HY.get(name)
    if alias and alias in (m or {}):
        return {"code": m[alias], "approx": alias}
    return {"code": _match_tx(name), "approx": ""}


def boards(kind: str = "hy", force: bool = False) -> list[dict]:
    """板块榜。hy/gn 走新浪,dy 走腾讯。返回按主力净额降序。

    排序放在缓存里做:20 秒一次的刷新不该每次重排 800 行再深拷一遍。
    """
    kind = kind if kind in KINDS else "hy"

    def load():
        if kind == "dy":
            rows = _boards_tx("dy")
        else:
            rows = _boards_sina(kind) or _boards_tx(kind)
        rows.sort(key=lambda r: (r.get("main") is None, -(r.get("main") or 0.0)))
        return rows

    return list(_memo(f"boards:{kind}", _TTL_BOARD, load, force=force) or ())


def board_by_name(name: str, kind: str | None = None) -> dict | None:
    """按名字找板块。"""
    for k in ((kind,) if kind else KINDS):
        for row in boards(k):
            if row["name"] == name or row["name"] == name.removesuffix("板块"):
                return row
    return None


# ---------------------------------------------------------------- 成分股

def _tx_code_of(board: dict) -> str:
    return str(_resolve_tx(board).get("code") or "")


def constituents(board: dict, force: bool = False, limit: int = 400) -> list[dict]:
    """板块成分股。新浪 node 优先;东财 clist b:BK 恢复后再作兜底。"""
    node = str(board.get("node") or "")
    bk = str(board.get("bk") or "")
    key = f"cons:{node or bk or board.get('name')}"

    def load():
        rows: list[dict] = []
        if node:
            rows = _cons_sina(node, limit)
        if not rows and bk:
            rows = _cons_em(bk, limit)
        return rows

    return _memo(key, _TTL_CONS, load, force=force) or []


def _cons_sina(node: str, limit: int) -> list[dict]:
    """新浪板块成分股。单页硬上限 100 只(传 400 也只给 100),必须翻页。"""
    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while len(out) < limit and page <= 8:
        txt = http.get_text(_SINA_NODE, params={"page": page, "num": 100,
                                               "sort": "changepercent", "asc": 0,
                                               "node": node},
                            headers=_SINA_HDR, retries=1)
        rows = http.parse_jsonp(txt)
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            code = normalize_code(r.get("code") or r.get("symbol") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "name": str(r.get("name") or ""),
                "price": safe_float(r.get("trade")),
                "change_pct": safe_float(r.get("changepercent")),
                "amount": safe_float(r.get("amount")),
                "turnover": safe_float(r.get("turnoverratio")),
                "mktcap": safe_float(r.get("mktcap")),       # 万元
                "source": "sina",
            })
        if len(rows) < 100:
            break
        page += 1
    out.sort(key=lambda x: (x["change_pct"] is None, -(x["change_pct"] or 0.0)))
    return out[:limit]


def _cons_em(bk: str, limit: int) -> list[dict]:
    """东财板块成分(clist 的 fs=b:BKxxxx)。当前被服务端拒绝,恢复后可自动接上。"""
    if http.is_cooling(_EM_SLIST):
        return []
    js = http.get_json(em.PUSH2 + "/api/qt/clist/get", params={
        "pn": 1, "pz": max(1, min(limit, 200)), "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": "f62", "fs": f"b:{bk}",
        "fields": "f12,f14,f2,f3,f6,f8,f62,f184",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }, headers=em.EM_HEADERS, retries=1) or {}
    diff = ((js.get("data") or {}).get("diff")) or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    out = []
    for d in diff:
        code = normalize_code(d.get("f12") or "")
        if not code:
            continue
        out.append({
            "code": code, "name": str(d.get("f14") or ""),
            "price": safe_float(d.get("f2")), "change_pct": safe_float(d.get("f3")),
            "amount": safe_float(d.get("f6")), "turnover": safe_float(d.get("f8")),
            "mktcap": None, "main": safe_float(d.get("f62")),
            "source": "em",
        })
    return out


# ---------------------------------------------------------------- 分时 / 日K

def _parse_tx_minute(code: str, js: dict) -> dict:
    node = (js.get("data") or {}).get(code) or {}
    data = node.get("data") or {}
    raw = data.get("data") or []
    bars: list[dict] = []
    amount = 0.0
    vol_sum = 0.0
    for line in raw:
        parts = str(line).split()
        if len(parts) < 2:
            continue
        hhmm, price = parts[0], safe_float(parts[1])
        vol = safe_float(parts[2]) if len(parts) > 2 else 0.0
        if price is None:
            continue
        vol = vol or 0.0
        amount += price * vol
        vol_sum += vol
        bars.append({"t": f"{data.get('date', '')[:8]} {hhmm[:2]}:{hhmm[2:]}",
                     "price": price,
                     "avg": (amount / vol_sum) if vol_sum > 0 else price})
    pre = None
    qt = node.get("qt") or {}
    line = qt.get(code) or []
    if isinstance(line, list) and len(line) > 4:
        pre = safe_float(line[4])
    return {"bars": bars, "pre_close": pre, "date": data.get("date") or ""}


def minute(board: dict, force: bool = False) -> dict:
    """板块分时。腾讯 pt 码对不上时返回空 bars,界面据此提示。"""
    resolved = _resolve_tx(board)
    code = str(resolved.get("code") or "")
    approx = str(resolved.get("approx") or "")
    if not code:
        return {"bars": [], "pre_close": None, "date": "", "code": "", "approx": ""}

    def load():
        js = http.get_json(_TX_MIN, params={"code": code}, headers=_TX_HDR, retries=1) or {}
        return _parse_tx_minute(code, js)

    data = _memo(f"min:{code}", _TTL_MIN, load, force=force) or {"bars": [], "pre_close": None}
    out = dict(data)
    out["code"] = code
    out["approx"] = approx
    return out


def kline(board: dict, days: int = 90, force: bool = False) -> list[dict]:
    """板块日K(腾讯 newfqkline;fqkline 对 pt 码只回一根,不能用)。"""
    code = _tx_code_of(board)
    if not code:
        return []
    days = max(10, min(int(days), 400))

    def load():
        js = http.get_json(_TX_KLINE, params={
            "param": f"{code},day,,,{days},qfq"}, headers=_TX_HDR, retries=1) or {}
        node = (js.get("data") or {}).get(code) or {}
        rows = node.get("qfqday") or node.get("day") or []
        out: list[dict] = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6:
                continue
            out.append({"date": str(r[0]),
                        "open": safe_float(r[1]), "close": safe_float(r[2]),
                        "high": safe_float(r[3]), "low": safe_float(r[4]),
                        "volume": safe_float(r[5])})
        return out

    return _memo(f"k:{code}:{days}", _TTL_K, load, force=force) or []


# ---------------------------------------------------------------- 个股所属板块

def _industry_names() -> set[str]:
    """新浪行业名集合,只在 f111 没给行业层级时兜底用;取不到就退化成空集。"""
    def build():
        return {str(r.get("name") or "").strip()
                for r in _boards_sina("hy") if r.get("name")}

    return _memo("indnames", _TTL_MAP, build) or set()


def classify(name: str, level: int = 0) -> str:
    """所属板块归类:行业 / 概念 / 地域 / 风格 / 其他。

    东财 slist 的 f111 直接给行业层级:2=一级 4=二级 8=三级,0=非行业。
    有它就按它判行业(准);没有(比如别的数据源)再退回按名字查新浪名单。
    非行业的再按名字分地域 / 风格 / 概念。
    """
    n = (name or "").strip()
    if not n:
        return "其他"
    if level in (2, 4, 8):
        return "行业"
    bare = n.removesuffix("板块")
    if bare in _REGION_HINT or n.endswith("板块"):
        return "地域"
    if any(k in n for k in _STYLE_HINT) or "指数" in n or "风格" in n:
        return "风格"
    if n.endswith(("Ⅰ", "Ⅱ", "Ⅲ", "II", "III")):
        return "行业"          # 只有层级名没给 f111 时,靠罗马数字认行业细分
    if n.endswith("概念"):
        return "概念"
    return "行业" if n in _industry_names() else "概念"


def of_stock(code: str, force: bool = False) -> list[dict]:
    """个股所属板块(东财 slist spt=3):BK 码 + 涨跌幅 + 主力净额 + 净占比 + 家数。"""
    code = normalize_code(code)
    if not code:
        return []

    def load():
        js = http.get_json(_EM_SLIST, params={
            "spt": 3, "secid": secid(code), "pn": 1, "pz": 60, "po": 1, "np": 1,
            "fltt": 2, "invt": 2, "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fields": "f12,f13,f14,f3,f62,f184,f104,f105,f111",
        }, headers=em.EM_HEADERS, retries=1) or {}
        diff = ((js.get("data") or {}).get("diff")) or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        out: list[dict] = []
        for d in diff:
            bk = str(d.get("f12") or "")
            name = str(d.get("f14") or "")
            if not bk.startswith("BK") or not name:
                continue          # 第一行是股票自己,跳过
            level = int(safe_float(d.get("f111")) or 0)
            out.append({
                "bk": bk,
                "name": name,
                "type": classify(name, level),
                "level": level,
                "change_pct": safe_float(d.get("f3")),
                "main": safe_float(d.get("f62")),
                "main_ratio": safe_float(d.get("f184")),
                "up": safe_float(d.get("f104")),
                "down": safe_float(d.get("f105")),
                "source": "em",
            })
        order = {"行业": 0, "概念": 1, "地域": 2, "风格": 3, "其他": 4}
        # 行业内部按层级(一级→三级),其余按主力净额从大到小
        out.sort(key=lambda x: (order.get(x["type"], 9),
                               x["level"] if x["type"] == "行业" else 0,
                               -(x["main"] or 0.0)))
        return out

    return _memo(f"of:{code}", _TTL_OF, load, force=force) or []
