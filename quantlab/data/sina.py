# -*- coding: utf-8 -*-
"""新浪行情:五档盘口 + 批量快照(东财限流时的备用源)。

新浪 hq 字段顺序:
  0 名称 1 今开 2 昨收 3 现价 4 最高 5 最低 6 买一价 7 卖一价 8 成交量(股) 9 成交额(元)
  10 买一量 11 买一价 12 买二量 13 买二价 ... 18 买五量 19 买五价
  20 卖一量 21 卖一价 ... 28 卖五量 29 卖五价  30 日期 31 时间
"""
from __future__ import annotations

from ..utils import index_symbol, normalize_code, safe_float, sina_symbol
from . import http

SINA_HQ = "https://hq.sinajs.cn/list="
SINA_LIST = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData")
MF_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
          "MoneyFlow.ssi_ssfx_flzjtj")
MF_BK_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "MoneyFlow.ssl_bkzj_ssggzj")
MF_HIS_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
              "MoneyFlow.ssl_qsfx_zjlrqs")
HEADERS = {"Referer": "https://finance.sina.com.cn"}


def _parse_line(line: str) -> dict | None:
    if '="' not in line:
        return None
    sym = line.split('var hq_str_', 1)[-1].split('="', 1)[0]
    payload = line.split('="', 1)[1].rsplit('"', 1)[0]
    if not payload:
        return None
    p = payload.split(",")
    if len(p) < 32 or not p[3]:
        return None
    price = safe_float(p[3])
    pre = safe_float(p[2])
    bids, asks = [], []
    for i in range(5):
        bv, bp = safe_float(p[10 + i * 2]), safe_float(p[11 + i * 2])
        av, ap = safe_float(p[20 + i * 2]), safe_float(p[21 + i * 2])
        if bp:
            bids.append((bp, bv))
        if ap:
            asks.append((ap, av))
    return {
        "code": normalize_code(sym),
        "name": p[0],
        "price": price,
        "open": safe_float(p[1]),
        "pre_close": pre,
        "high": safe_float(p[4]),
        "low": safe_float(p[5]),
        "bid1": safe_float(p[6]),
        "ask1": safe_float(p[7]),
        "volume": (safe_float(p[8]) or 0) / 100.0,      # 股 → 手
        "amount": safe_float(p[9]),
        "bids": bids,                                    # [(价, 量(股))]
        "asks": asks,
        "date": p[30],
        "time": p[31],
        "change": (price - pre) if price and pre else None,
        "change_pct": (price / pre - 1) * 100 if price and pre else None,
    }


def orderbook(code: str) -> dict | None:
    """单只股票的五档 + 基本行情。"""
    try:
        txt = http.get_text(SINA_HQ + sina_symbol(code), headers=HEADERS, encoding="gbk")
    except Exception:
        return None
    return _parse_line(txt.strip())


def quotes(codes: list[str]) -> dict[str, dict]:
    """批量快照(每批 60 只,新浪单次上限较宽)。"""
    out: dict[str, dict] = {}
    codes = [normalize_code(c) for c in codes if c]
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = SINA_HQ + ",".join(sina_symbol(c) for c in batch)
        try:
            txt = http.get_text(url, headers=HEADERS, encoding="gbk", retries=2)
        except Exception:
            continue
        for line in txt.split(";"):
            q = _parse_line(line.strip())
            if q:
                out[q["code"]] = q
    return out


def index_quotes(codes: list[str]) -> list[dict]:
    """指数快照(第三级备用)。

    必须用 index_symbol 映射:000001 作为指数是 sh000001,
    用个股映射会拿到 sz000001 = 平安银行(之前的串号 bug 就在这里)。
    """
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return []
    out: list[dict] = []
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        url = SINA_HQ + ",".join(index_symbol(c) for c in batch)
        try:
            txt = http.get_text(url, headers=HEADERS, encoding="gbk", retries=2)
        except Exception:
            continue
        for line in txt.split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            sym = line.split("hq_str_", 1)[-1].split("=", 1)[0]
            payload = line.split('="', 1)[1].rsplit('"', 1)[0]
            p = payload.split(",")
            if len(p) < 10:
                continue
            price, pre = safe_float(p[3]), safe_float(p[2])
            out.append({
                "code": normalize_code(sym),
                "name": p[0],
                "price": price,
                "pre_close": pre,
                "open": safe_float(p[1]),
                "high": safe_float(p[4]),
                "low": safe_float(p[5]),
                "volume": (safe_float(p[8]) or 0) / 100.0,   # 股 → 手
                "amount": safe_float(p[9]),
                "change": (price - pre) if price and pre else None,
                "change_pct": (price / pre - 1) * 100 if price and pre else None,
                "source": "sina",
            })
    return out


def moneyflow(code: str) -> dict:
    """个股当日资金流(东财 push2 不可用时的备用源)。

    r0 超大单 / r1 大单 / r2 中单 / r3 小单,字段为累计流入流出额。
    主力 = 超大单净额 + 大单净额。
    """
    import json

    txt = http.get_text(MF_URL, params={"daima": sina_symbol(code)},
                        headers=HEADERS, encoding="gbk", retries=1)
    try:
        js = json.loads(txt)
    except Exception:
        js = http.parse_jsonp(txt)
    if not isinstance(js, dict):
        return {}
    def net(i: int) -> float | None:
        a, b = safe_float(js.get(f"r{i}_in")), safe_float(js.get(f"r{i}_out"))
        if a is None or b is None:
            return None
        return a - b

    super_net, big_net, mid_net, small_net = net(0), net(1), net(2), net(3)
    main = None
    if super_net is not None and big_net is not None:
        main = super_net + big_net
    # 今日的买卖毛额:主力 = 超大单 + 大单的累计流入/流出
    inflow = outflow = None
    try:
        ins = [safe_float(js.get("r0_in")), safe_float(js.get("r1_in"))]
        outs = [safe_float(js.get("r0_out")), safe_float(js.get("r1_out"))]
        if all(v is not None for v in ins + outs):
            inflow = sum(ins)   # type: ignore[arg-type]
            outflow = sum(outs)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        inflow = outflow = None
    return {"name": js.get("name"), "main": main, "super": super_net,
            "big": big_net, "mid": mid_net, "small": small_net,
            "inflow": inflow, "outflow": outflow}


def market_list(page: int = 1, num: int = 100, sort: str = "changepercent",
                asc: int = 0, node: str = "hs_a") -> list[dict]:
    """全市场列表(页大小 ≤100)。字段:code/name/price/change_pct/volume/amount/..."""
    txt = http.get_text(SINA_LIST, params={"page": page, "num": num, "sort": sort,
                                           "asc": asc, "node": node,
                                           "symbol": "", "_s_r_a": "page"},
                        headers=HEADERS, retries=2)
    rows = http.parse_jsonp(txt)
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        price = safe_float(r.get("trade"))
        out.append({
            "code": normalize_code(r.get("code") or ""),
            "name": r.get("name"),
            "price": price,
            "change": safe_float(r.get("pricechange")),
            "change_pct": safe_float(r.get("changepercent")),
            "open": safe_float(r.get("open")),
            "high": safe_float(r.get("high")),
            "low": safe_float(r.get("low")),
            "pre_close": safe_float(r.get("settlement")),
            "volume": (safe_float(r.get("volume")) or 0) / 100.0,
            "amount": safe_float(r.get("amount")),
            "turnover": safe_float(r.get("turnoverratio")),
            "pe_ttm": safe_float(r.get("per")),
            "pb": safe_float(r.get("pb")),
            "market_cap": (safe_float(r.get("mktcap")) or 0) * 1e4,   # 万元 → 元
            "float_cap": (safe_float(r.get("nmc")) or 0) * 1e4,
            "volume_ratio": None, "amplitude": None,
            "main_inflow": None, "main_ratio": None, "super_inflow": None,
            "big_inflow": None, "mid_inflow": None, "small_inflow": None,
        })
    return out


def moneyflow_history(code: str, num: int = 15) -> list[dict]:
    """个股日频资金流(新浪,逐日净额),东财 daykline 不通时用。

    口径与榜单一致:netamount = 大单以上净额(数据商口径),每行带 source='sina'。
    返回按日期升序:[{date, main, super, big, mid, small, main_ratio, source}]
    """
    code_n = normalize_code(code)
    daima = sina_symbol(code_n)
    txt = http.get_text(MF_HIS_URL, params={"daima": daima, "num": max(3, min(int(num), 60))},
                        headers=HEADERS, encoding="gbk", retries=1)
    rows = http.parse_jsonp(txt)
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        r0 = safe_float(r.get("r0_net"))
        r1 = safe_float(r.get("r1_net"))
        main = safe_float(r.get("netamount"))
        if main is None and (r0 is not None or r1 is not None):
            main = (r0 or 0.0) + (r1 or 0.0)
        if main is None:
            continue
        ratio = safe_float(r.get("ratioamount"))
        out.append({"date": str(r.get("opendate") or ""),
                    "main": main, "super": r0, "big": r1,
                    "mid": safe_float(r.get("r2_net")),
                    "small": safe_float(r.get("r3_net")),
                    "main_ratio": ratio * 100 if ratio is not None else None,
                    "source": "sina"})
    out.sort(key=lambda x: x["date"])
    return out


def moneyflow_rank(top: int = 50, asc: bool = False, sort: str = "netamount") -> list[dict]:
    """全市场资金流向榜(东财 clist 挂掉时的备用源)。

    sort: netamount 资金净流入合计 / r0_net 超大单净额。
    asc=False 取流入前 N,asc=True 取流出前 N。

    口径提醒:netamount 是「大单以上净流入」(数据商口径),和东财 f62 近似但不完全等同,
    所以榜单行都带 source='sina',界面据此标注来源。
    """
    num = max(1, min(int(top), 100))
    txt = http.get_text(MF_BK_URL, params={
        "page": 1, "num": num, "sort": sort, "asc": 1 if asc else 0,
        "bankuai": "", "shichang": "",
    }, headers=HEADERS, encoding="gbk", retries=1)
    rows = http.parse_jsonp(txt)
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # 新浪这两个字段是小数比例(0.19 = 19%),界面按百分数展示,这里统一 ×100
        chg = safe_float(r.get("changeratio"))
        ratio = safe_float(r.get("ratioamount"))
        out.append({
            "code": normalize_code(r.get("symbol") or ""),
            "name": r.get("name"),
            "price": safe_float(r.get("trade")),
            "change_pct": chg * 100 if chg is not None else None,
            "amount": safe_float(r.get("amount")),
            "main_inflow": safe_float(r.get("netamount")),
            "main_ratio": ratio * 100 if ratio is not None else None,
            "super_inflow": safe_float(r.get("r0_net")),
            "big_inflow": safe_float(r.get("r1_net")),
            "source": "sina",
        })
    return out
