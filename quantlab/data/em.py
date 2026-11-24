# -*- coding: utf-8 -*-
"""东方财富数据源:实时行情、排行榜、资金流、K线、分时、F10、新闻、公告。

字段口径(东财 push2 的 f 编号):
  f2 最新价  f3 涨跌幅%  f4 涨跌额  f5 成交量(手)  f6 成交额(元)  f7 振幅%
  f8 换手率%  f9 市盈率(动)  f10 量比  f12 代码  f13 市场  f14 名称
  f15 最高  f16 最低  f17 今开  f18 昨收  f20 总市值  f21 流通市值  f23 市净率
  f62 主力净流入(元)  f66 超大单净额  f69 超大单净占比%  f72 大单净额  f75 大单净占比%
  f78 中单净额  f81 中单净占比%  f84 小单净额  f87 小单净占比%  f184 主力净占比%
  f115 市盈率(静)  f26 上市日期
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..utils import normalize_code, safe_float, secid
from . import http

PUSH2 = "https://push2.eastmoney.com"
PUSH2HIS = "https://push2his.eastmoney.com"
EM_HEADERS = {"Referer": "https://quote.eastmoney.com/"}

# 全 A 股(沪深主板+创业板+科创板+北交所)
FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

QUOTE_FIELDS = ",".join([
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f12", "f13", "f14",
    "f15", "f16", "f17", "f18", "f20", "f21", "f23", "f26", "f62", "f66", "f69",
    "f72", "f75", "f78", "f81", "f84", "f87", "f115", "f184", "f124",
])


def _row_to_quote(d: dict) -> dict:
    """东财 diff 行 → 规范化行情字典。"""
    return {
        "code": d.get("f12"),
        "name": d.get("f14"),
        "market": d.get("f13"),
        "price": safe_float(d.get("f2")),
        "change_pct": safe_float(d.get("f3")),
        "change": safe_float(d.get("f4")),
        "volume": safe_float(d.get("f5")),          # 手
        "amount": safe_float(d.get("f6")),          # 元
        "amplitude": safe_float(d.get("f7")),
        "turnover": safe_float(d.get("f8")),
        "pe_dynamic": safe_float(d.get("f9")),
        "pe_ttm": safe_float(d.get("f115")),
        "volume_ratio": safe_float(d.get("f10")),
        "high": safe_float(d.get("f15")),
        "low": safe_float(d.get("f16")),
        "open": safe_float(d.get("f17")),
        "pre_close": safe_float(d.get("f18")),
        "market_cap": safe_float(d.get("f20")),
        "float_cap": safe_float(d.get("f21")),
        "pb": safe_float(d.get("f23")),
        "list_date": d.get("f26"),
        "main_inflow": safe_float(d.get("f62")),
        "super_inflow": safe_float(d.get("f66")),
        "super_ratio": safe_float(d.get("f69")),
        "big_inflow": safe_float(d.get("f72")),
        "big_ratio": safe_float(d.get("f75")),
        "mid_inflow": safe_float(d.get("f78")),
        "mid_ratio": safe_float(d.get("f81")),
        "small_inflow": safe_float(d.get("f84")),
        "small_ratio": safe_float(d.get("f87")),
        "main_ratio": safe_float(d.get("f184")),
        "ts": safe_float(d.get("f124")),
        "update_time": time.time(),
    }


# ---------------------------------------------------------------- 实时行情

def quotes(codes: list[str], fallback: bool = True, retries: int = 3,
           timeout: float = 8.0) -> dict[str, dict]:
    """批量快照(一次请求拿全部自选股)。

    fallback=True 时东财不可用自动切新浪;realtime.py 自己管级联,会传 False,
    这样失败能立刻上抛,不再多吃一遍新浪的往返。
    """
    codes = [normalize_code(c) for c in codes if c]
    if not codes:
        return {}
    secids = ",".join(secid(c) for c in codes)
    url = f"{PUSH2}/api/qt/ulist.np/get"
    params = {"secids": secids, "fields": QUOTE_FIELDS, "fltt": 2, "invt": 2, "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
    try:
        js = http.get_json(url, params=params, headers=EM_HEADERS,
                           retries=retries, timeout=timeout)
    except Exception:
        if not fallback:
            raise
        from . import sina
        return sina.quotes(codes)
    data = (js or {}).get("data") or {}
    out = {}
    for row in data.get("diff") or []:
        q = _row_to_quote(row)
        if q["code"]:
            out[q["code"]] = q
    if not out and fallback:
        from . import sina
        return sina.quotes(codes)
    return out


def quote(code: str) -> dict | None:
    return quotes([code]).get(normalize_code(code))


def market_list(sort_field: str = "f3", asc: bool = False, page: int = 1, size: int = 100,
                fs: str = FS_ALL_A, retries: int = 3) -> list[dict]:
    """排行榜。sort_field: f3 涨跌幅 / f6 成交额 / f8 换手率 / f10 量比 / f62 主力净流入 / f20 市值。"""
    url = f"{PUSH2}/api/qt/clist/get"
    params = {
        "pn": page, "pz": size, "po": 0 if asc else 1, "np": 1, "fltt": 2, "invt": 2,
        "fid": sort_field, "fs": fs, "fields": QUOTE_FIELDS,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    js = http.get_json(url, params=params, headers=EM_HEADERS, retries=retries)
    data = (js or {}).get("data") or {}
    return [_row_to_quote(r) for r in (data.get("diff") or [])]


def market_list_all(sort_field: str = "f3", asc: bool = False, limit: int = 500) -> list[dict]:
    """翻页拿全市场(默认按涨跌幅取前 limit 只)。东财不可用时退新浪列表。"""
    out: list[dict] = []
    page = 1
    try:
        while len(out) < limit:
            batch = market_list(sort_field, asc, page=page, size=min(100, limit - len(out)))
            if not batch:
                break
            out.extend(batch)
            page += 1
            if page > 60:
                break
        if out:
            return out[:limit]
    except Exception:
        pass
    # 备用:新浪(没有资金流字段,其余可用)
    from . import sina
    sort = "changepercent" if sort_field == "f3" else "amount"
    page = 1
    while len(out) < limit and page <= 60:
        try:
            batch = sina.market_list(page=page, num=100, sort=sort, asc=1 if asc else 0)
        except Exception:
            break
        if not batch:
            break
        out.extend(batch)
        page += 1
    return out[:limit]


INDEX_CODES = ["000001", "399001", "399006", "000300", "000688", "399905"]


def indexes(retries: int = 3, fallback: bool = True) -> list[dict]:
    """主要指数(东财口径)。realtime.py 会先走腾讯,这里只作为第二级。"""
    codes = INDEX_CODES
    from ..utils import index_secid
    secids = [index_secid(c) for c in codes]
    url = f"{PUSH2}/api/qt/ulist.np/get"
    params = {"secids": ",".join(secids), "fields": "f2,f3,f4,f12,f14,f6,f104,f105,f106",
              "fltt": 2, "invt": 2}
    try:
        js = http.get_json(url, params=params, headers=EM_HEADERS, retries=retries)
    except Exception:
        js = None
    data = (js or {}).get("data") or {}
    out = []
    for r in data.get("diff") or []:
        out.append({"code": r.get("f12"), "name": r.get("f14"),
                    "price": safe_float(r.get("f2")), "change_pct": safe_float(r.get("f3")),
                    "change": safe_float(r.get("f4")), "amount": safe_float(r.get("f6")),
                    "source": "em"})
    if out or not fallback:
        return out
    # 新浪指数代码按 index_symbol 显式映射(之前用个股映射,000001 会变成平安银行)
    from . import sina
    return sina.index_quotes(codes)


# ---------------------------------------------------------------- K 线 / 分时

def kline(code: str, klt: int = 101, count: int = 260, fqt: int = 1,
          end: str = "20500101", is_index: bool = False,
          retries: int = 1, timeout: float = 6.0) -> list[dict]:
    """K 线。klt: 1/5/15/30/60 分钟, 101 日, 102 周, 103 月。fqt: 0 不复权 1 前复权 2 后复权。"""
    from ..utils import index_secid
    url = f"{PUSH2HIS}/api/qt/stock/kline/get"
    params = {
        "secid": index_secid(code) if is_index else secid(code),
        "klt": klt, "fqt": fqt, "lmt": count, "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    js = http.get_json(url, params=params, headers=EM_HEADERS,
                       retries=retries, timeout=timeout)
    data = (js or {}).get("data") or {}
    bars = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 7:
            continue
        bars.append({
            "datetime": p[0],
            "open": safe_float(p[1]), "close": safe_float(p[2]),
            "high": safe_float(p[3]), "low": safe_float(p[4]),
            "volume": safe_float(p[5]), "amount": safe_float(p[6]),
            "amplitude": safe_float(p[7]) if len(p) > 7 else None,
            "change_pct": safe_float(p[8]) if len(p) > 8 else None,
            "change": safe_float(p[9]) if len(p) > 9 else None,
            "turnover": safe_float(p[10]) if len(p) > 10 else None,
        })
    return bars


def trends(code: str, ndays: int = 1) -> dict:
    """分时走势(含均价线)。返回 {pre_close, bars:[{t, price, avg, volume, amount}]}"""
    url = f"{PUSH2}/api/qt/stock/trends2/get"
    params = {
        "secid": secid(code), "ndays": ndays, "iscr": 0,
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }
    try:
        js = http.get_json(url, params=params, headers=EM_HEADERS)
    except Exception:
        js = None
    data = (js or {}).get("data") or {}
    out = []
    for line in data.get("trends") or []:
        p = line.split(",")
        if len(p) < 7:
            continue
        out.append({
            "t": p[0],
            "open": safe_float(p[1]), "price": safe_float(p[2]),
            "high": safe_float(p[3]), "low": safe_float(p[4]),
            "volume": safe_float(p[5]), "amount": safe_float(p[6]),
            "avg": safe_float(p[7]) if len(p) > 7 else None,
        })
    if not out:
        # 备用:腾讯分时(量/额是分钟增量,均价需自己累计)
        from .kline import tx_minute
        alt = tx_minute(code)
        if alt.get("bars"):
            return alt
    return {"pre_close": safe_float(data.get("preClose")),
            "name": data.get("name"), "code": data.get("code"), "bars": out}


def ticks_today(code: str, limit: int = 60) -> list[dict]:
    """当日逐笔成交(东财分笔,量大时截断)。"""
    url = f"{PUSH2}/api/qt/stock/details/get"
    params = {"secid": secid(code), "fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55",
              "pos": -limit, "ut": "fa5fd1943c7b386f172d6893dbfba10b"}
    js = http.get_json(url, params=params, headers=EM_HEADERS)
    data = (js or {}).get("data") or {}
    out = []
    for line in data.get("details") or []:
        p = line.split(",")
        if len(p) < 4:
            continue
        out.append({"time": p[0], "price": safe_float(p[1]), "volume": safe_float(p[2]),
                    "amount": safe_float(p[3]) if len(p) > 3 else None,
                    "side": p[4] if len(p) > 4 else None})
    return out


# ---------------------------------------------------------------- 资金流

def fflow_intraday(code: str) -> tuple[list[dict], str | None]:
    """东财 fflow/kline 低层,返回 (bars, name);冷却或失败返回 ([], None)。

    只负责「东财这一次请求」,兜底顺序由 moneyflow.intraday 统一调度 ——
    以前这里冷却期直接返回空,图表整段空白就是这个原因。
    """
    from . import http as _http
    url = f"{PUSH2}/api/qt/stock/fflow/kline/get"
    if _http.is_cooling(url):
        return [], None
    params = {
        "secid": secid(code), "klt": 1, "lmt": 0,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    try:
        js = http.get_json(url, params=params, headers=EM_HEADERS, retries=1, timeout=5.0)
    except Exception:
        return [], None
    data = (js or {}).get("data") or {}
    bars = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        bars.append({
            "t": p[0],
            "main": safe_float(p[1]), "small": safe_float(p[2]),
            "mid": safe_float(p[3]), "big": safe_float(p[4]), "super": safe_float(p[5]),
        })
    return bars, data.get("name")


def moneyflow_intraday(code: str) -> dict:
    """个股当日主力资金分时。走 moneyflow 层:12 秒缓存 + 东财→同花顺→腾讯。"""
    from . import moneyflow as _mf
    return _mf.intraday(code)


def moneyflow_now(code: str, fast: bool = False) -> dict:
    """当日资金流。走 moneyflow 层:批量 f62 → 同花顺 → 新浪 → 腾讯,带缓存。"""
    from . import moneyflow as _mf
    return _mf.now(code, fast=fast)


def moneyflow_history(code: str, days: int = 60) -> list[dict]:
    """个股历史资金流(日频)。"""
    url = f"{PUSH2HIS}/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": secid(code), "lmt": days, "klt": 101,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    js = http.get_json(url, params=params, headers=EM_HEADERS)
    data = (js or {}).get("data") or {}
    out = []
    for line in data.get("klines") or []:
        p = line.split(",")
        if len(p) < 6:
            continue
        out.append({"date": p[0], "main": safe_float(p[1]), "small": safe_float(p[2]),
                    "mid": safe_float(p[3]), "big": safe_float(p[4]), "super": safe_float(p[5]),
                    "main_ratio": safe_float(p[6]) if len(p) > 6 else None})
    return out


def rank_flow(top: int = 50, asc: bool = False, retries: int = 1) -> list[dict]:
    """东财全市场主力净流入排行低层(clist 一次请求)。失败返回空,不抛。"""
    rows = market_list("f62", asc=asc, size=min(max(int(top), 1), 100), retries=retries)
    for r in rows:
        r["source"] = "em"
    return rows


def moneyflow_rank(top: int = 50, asc: bool = False) -> list[dict]:
    """全市场主力资金榜:东财 → 新浪(超大单口径)。"""
    from . import moneyflow as _mf
    return _mf.rank(top, asc=asc)


def sector_flow(top: int = 30, retries: int = 1) -> list[dict]:
    """行业板块资金流低层。fs=m:90+t:2 = 行业板块;东财独有。"""
    url = f"{PUSH2}/api/qt/clist/get"
    params = {
        "pn": 1, "pz": top, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f62",
        "fs": "m:90+t:2+f:!50", "fields": "f2,f3,f6,f12,f14,f62,f184,f66,f69,f72,f75,f84,f87,f128,f140",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    js = http.get_json(url, params=params, headers=EM_HEADERS, retries=retries)
    data = (js or {}).get("data") or {}
    out = []
    for r in data.get("diff") or []:
        out.append({"code": r.get("f12"), "name": r.get("f14"),
                    "change_pct": safe_float(r.get("f3")), "amount": safe_float(r.get("f6")),
                    "main_inflow": safe_float(r.get("f62")), "main_ratio": safe_float(r.get("f184")),
                    "lead_stock": r.get("f128"), "lead_pct": safe_float(r.get("f140")),
                    "source": "em"})
    return out


def sector_moneyflow(top: int = 30) -> list[dict]:
    """行业板块资金流(带缓存与冷却短路)。"""
    from . import moneyflow as _mf
    return _mf.sectors(top)



# ---------------------------------------------------------------- 公司资料 F10

def company_profile(code: str) -> dict:
    """公司概况 + 主营构成概要。"""
    c = normalize_code(code)
    prefix = "".join(_sec_suffix(c)[:1]) + c
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
    js = http.get_json(url, params={"code": prefix},
                       headers={"Referer": "https://emweb.securities.eastmoney.com/"})
    jb = ((js or {}).get("jbzl") or [{}])[0]
    fxxg = ((js or {}).get("fxxg") or [{}])[0]
    return {
        "code": jb.get("SECURITY_CODE", c),
        "name": jb.get("SECURITY_NAME_ABBR"),
        "full_name": jb.get("ORG_NAME"),
        "en_name": jb.get("ORG_NAME_EN"),
        "former_name": jb.get("FORMERNAME"),
        "market": jb.get("TRADE_MARKET"),
        "industry": jb.get("EM2016"),
        "industry_csrc": jb.get("INDUSTRYCSRC1"),
        "chairman": jb.get("CHAIRMAN"),
        "president": jb.get("PRESIDENT"),
        "secretary": jb.get("SECRETARY"),
        "reg_capital": jb.get("REG_CAPITAL"),
        "employees": jb.get("EMP_NUM"),
        "found_date": jb.get("FOUND_DATE"),
        "list_date": fxxg.get("LISTING_DATE") or jb.get("LISTING_DATE"),
        "issue_price": fxxg.get("ISSUE_PRICE"),
        "website": jb.get("ORG_WEB"),
        "email": jb.get("ORG_EMAIL"),
        "address": jb.get("REG_ADDRESS"),
        "office_address": jb.get("ADDRESS"),
        "business": jb.get("BUSINESS_SCOPE"),
        "intro": jb.get("ORG_PROFILE") or jb.get("BUSINESS_SCOPE"),
    }


def finance_summary(code: str) -> list[dict]:
    """主要财务指标(按报告期倒序,取最近 16 期)。数据来自东财 datacenter F10。"""
    c = normalize_code(code)
    suffix = _sec_suffix(c)[1]
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
        "filter": f'(SECUCODE="{suffix}")',
        "pageNumber": "1", "pageSize": "16",
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "source": "HSF10", "client": "PC",
    }
    try:
        js = http.get_json_curl(url, params=params,
                                headers={"Referer": "https://emweb.securities.eastmoney.com/"})
        rows = ((js or {}).get("result") or {}).get("data") or []
    except Exception:
        rows = []
    out = []
    for r in rows:
        out.append({
            "period": r.get("REPORT_DATE_NAME") or (r.get("REPORT_DATE") or "")[:10],
            "eps": safe_float(r.get("EPSJB")),
            "bps": safe_float(r.get("BPS")),
            "revenue": safe_float(r.get("TOTALOPERATEREVE")),
            "revenue_yoy": safe_float(r.get("TOTALOPERATEREVETZ")),
            "net_profit": safe_float(r.get("PARENTNETPROFIT")),
            "profit_yoy": safe_float(r.get("PARENTNETPROFITTZ")),
            "roe": safe_float(r.get("ROEJQ")),
            "gross_margin": safe_float(r.get("XSMLL")),
            "net_margin": safe_float(r.get("XSJLL")),
            "debt_ratio": safe_float(r.get("ZCFZL")),
            "cash_flow_ps": safe_float(r.get("MGJYXJJE")),
            # 盈利质量 / 偿债 / 规模:公司评估页要用,行情页财务表忽略这些键即可
            "current_ratio": safe_float(r.get("LD")),          # 流动比率
            "quick_ratio": safe_float(r.get("SD")),            # 速动比率
            "cash_ratio": safe_float(r.get("CASH_RATIO")),     # 现金比率
            "ocf_to_profit": safe_float(r.get("NCO_NETPROFIT")),  # 经营现金流/净利
            "ocf_to_revenue": safe_float(r.get("NCO_OP")),     # 经营现金流/营收
            "ocf": safe_float(r.get("NETCASH_OPERATE_PK")),    # 经营现金流净额
            "roic": safe_float(r.get("ROIC")),
            "total_assets": safe_float(r.get("TOTAL_ASSETS_PK")),
            "total_equity": safe_float(r.get("TOTAL_EQUITY_PK")),
            "liability": safe_float(r.get("LIABILITY")),
            "interest_debt_ratio": safe_float(r.get("INTEREST_DEBT_RATIO")),  # 有息负债率
            "rd_expense": safe_float(r.get("RDEXPEND")),       # 研发费用(部分公司缺)
            "total_share": safe_float(r.get("TOTAL_SHARE")),
            "revenue_qoq": safe_float(r.get("YYZSRGDHBZC")),   # 营收环比
            "profit_qoq": safe_float(r.get("NETPROFITRPHBZC")),  # 净利环比
            "gross_margin_change": safe_float(r.get("XSMLL_TB")),  # 毛利率同比变动
        })
    return out


# ---------------------------------------------------------------- 公司分析(主营/股东/估值)

F10_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _sec_suffix(code: str) -> tuple[str, str]:
    """代码 → (SH/SZ/BJ, 600519.SH 这种 SECUCODE 后缀)。北交所别当深市。

    北交所:43/83/87 开头(老三板平移)与 920 开头(新代码);900 是沪 B,仍归 SH。
    """
    c = normalize_code(code)
    if c.startswith(("4", "8", "920")):
        mkt = "BJ"
    elif c.startswith(("6", "9", "5")):
        mkt = "SH"
    else:
        mkt = "SZ"
    return mkt, f"{c}.{mkt}"


def _datacenter(report: str, filt: str, size: int = 20,
                sort: str = "REPORT_DATE") -> list[dict]:
    js = http.get_json(DATACENTER, params={
        "reportName": report, "columns": "ALL", "filter": filt,
        "pageNumber": "1", "pageSize": str(size), "sortColumns": sort,
        "sortTypes": "-1", "source": "HSF10", "client": "PC",
    }, headers=F10_HEADERS, retries=2, timeout=10.0)
    return (((js or {}).get("result") or {}).get("data")) or []


def business_mix(code: str, period: str = "") -> dict:
    """主营构成(按报告期)+ 经营范围/经营评述。

    返回 {"period","periods":[...],"by_period":{期:groups},"groups","scope","review"}。
    `groups` / `period` 是最新一期(兼容旧调用);页面切期次用 `by_period`。
    MAINOP_TYPE: 1 行业、2 产品、3 地区。占比是接口给的小数(0.9996 = 99.96%),
    毛利率只有「按行业」那组常有。
    """
    prefix = "".join(_sec_suffix(code)[:1]) + normalize_code(code)
    js = http.get_json(
        "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax",
        params={"code": prefix}, headers=F10_HEADERS, retries=2, timeout=10.0)
    rows = (js or {}).get("zygcfx") or []
    if not rows:
        return {"period": "", "periods": [], "by_period": {}, "groups": [],
                "scope": "", "review": ""}
    labels = {"1": "行业", "2": "产品", "3": "地区"}

    def _groups(day: str) -> list[dict]:
        out = []
        for t in ("1", "2", "3"):
            items = [r for r in rows
                     if str(r.get("REPORT_DATE") or "")[:10] == day
                     and str(r.get("MAINOP_TYPE")) == t]
            if not items:
                continue
            items.sort(key=lambda r: -(safe_float(r.get("MAIN_BUSINESS_INCOME")) or 0))
            out.append({
                "type": labels.get(t, t),
                "rows": [{
                    "name": r.get("ITEM_NAME") or "",
                    "income": safe_float(r.get("MAIN_BUSINESS_INCOME")),
                    "ratio": safe_float(r.get("MBI_RATIO")),
                    "margin": safe_float(r.get("GROSS_RPOFIT_RATIO")),
                } for r in items[:8]],
            })
        return out

    periods = sorted({str(r.get("REPORT_DATE") or "")[:10] for r in rows},
                     reverse=True)
    by_period = {d: _groups(d) for d in periods[:8]}
    pick = period if period in by_period else periods[0]
    scope = (((js or {}).get("zyfw") or [{}])[0].get("BUSINESS_SCOPE")) or ""
    review = (((js or {}).get("jyps") or [{}])[0].get("BUSINESS_REVIEW")) or ""
    return {"period": pick, "periods": periods[:8], "by_period": by_period,
            "groups": by_period.get(pick) or [],
            "scope": scope, "review": review}


def shareholder_detail(code: str) -> dict:
    """股东结构:十大流通股东、股东户数、控股股东、机构持仓汇总。"""
    prefix = "".join(_sec_suffix(code)[:1]) + normalize_code(code)
    js = http.get_json(
        "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax",
        params={"code": prefix}, headers=F10_HEADERS, retries=2, timeout=10.0)
    js = js or {}
    free = [{
        "name": r.get("HOLDER_NAME") or "",
        "ratio": safe_float(r.get("FREE_HOLDNUM_RATIO")),
        "change": safe_float(r.get("HOLD_NUM_CHANGE")),
        "change_ratio": safe_float(r.get("CHANGE_RATIO")),
        "type": r.get("SHARES_TYPE") or "",
    } for r in (js.get("sdltgd") or [])[:10]]
    counts = [{
        "date": str(r.get("END_DATE") or "")[:10],
        "holders": safe_float(r.get("HOLDER_TOTAL_NUM")),
        "change_pct": safe_float(r.get("TOTAL_NUM_RATIO")),
        "focus": r.get("HOLD_FOCUS") or "",
        "avg_shares": safe_float(r.get("AVG_FREE_SHARES")),
    } for r in (js.get("gdrs") or [])[:6]]
    ctrl = (js.get("sjkzr") or [{}])[0]
    inst = [{
        "date": str(r.get("REPORT_DATE") or "")[:10],
        "org_num": safe_float(r.get("TOTAL_ORG_NUM")),
        "ratio": safe_float(r.get("TOTAL_SHARES_RATIO")),
    } for r in (js.get("jgcc") or [])[:3]]
    unlocks = [{
        "date": str(r.get("LIFT_DATE") or "")[:10],
        "num": safe_float(r.get("LIFT_NUM")),
        "ratio": safe_float(r.get("TOTAL_SHARES_RATIO")),
        "type": r.get("LIFT_TYPE") or "",
    } for r in (js.get("xsjj") or [])[:5]]
    return {
        "free_top10": free,
        "holder_counts": counts,
        "controller": (ctrl.get("HOLDER_NAME") or ""),
        "controller_ratio": safe_float(ctrl.get("HOLD_RATIO")),
        "institutions": inst,
        "unlocks": unlocks,
    }


def valuation_snapshot(code: str) -> dict:
    """估值快照(东财 datacenter 每日估值表,不依赖 push2)。

    返回最新交易日收盘的 PE(TTM)/PE(静)/PB/PS、总市值/流通市值、所属二级行业。
    没有该股记录时返回 {} —— 北交所/新股可能缺。
    """
    suff = _sec_suffix(code)[1]
    rows = _datacenter("RPT_VALUEANALYSIS_DET",
                       f'(SECUCODE="{suff}")', size=1, sort="TRADE_DATE")
    if not rows:
        return {}
    r = rows[0]
    return {
        "date": str(r.get("TRADE_DATE") or "")[:10],
        "board": r.get("BOARD_NAME") or "",
        "price": safe_float(r.get("CLOSE_PRICE")),
        "pct": safe_float(r.get("CHANGE_RATE")),
        "pe_ttm": safe_float(r.get("PE_TTM")),
        "pe_static": safe_float(r.get("PE_LAR")),
        "pb": safe_float(r.get("PB_MRQ")),
        "ps": safe_float(r.get("PS_TTM")),
        "total_cap": safe_float(r.get("TOTAL_MARKET_CAP")),
        "float_cap": safe_float(r.get("NOTLIMITED_MARKETCAP_A")),
        "shares": safe_float(r.get("TOTAL_SHARES")),
    }


def industry_valuation(board: str, day: str, size: int = 200) -> list[dict]:
    """同一二级行业、同一交易日的全部个股估值(同业对比用)。"""
    rows = _datacenter("RPT_VALUEANALYSIS_DET",
                       f'(BOARD_NAME="{board}")(TRADE_DATE=\'{day}\')',
                       size=size, sort="TOTAL_MARKET_CAP")
    return [{
        "code": r.get("SECURITY_CODE") or "",
        "name": r.get("SECURITY_NAME_ABBR") or "",
        "pe_ttm": safe_float(r.get("PE_TTM")),
        "pb": safe_float(r.get("PB_MRQ")),
        "ps": safe_float(r.get("PS_TTM")),
        "total_cap": safe_float(r.get("TOTAL_MARKET_CAP")),
        "pct": safe_float(r.get("CHANGE_RATE")),
    } for r in rows]


def dividend_history(code: str, size: int = 8) -> list[dict]:
    """分红方案(按公告日倒序)。每 10 股派息/送转与进度。"""
    rows = _datacenter("RPT_SHAREBONUS_DET",
                       f'(SECURITY_CODE="{normalize_code(code)}")',
                       size=size, sort="NOTICE_DATE")
    out = []
    for r in rows:
        out.append({
            "notice": str(r.get("NOTICE_DATE") or "")[:10],
            "report": str(r.get("REPORT_DATE") or "")[:10],
            "plan": r.get("IMPL_PLAN_PROFILE") or "",
            "cash_per10": safe_float(r.get("PRETAX_BONUS_RMB")),
            "it_ratio": safe_float(r.get("BONUS_IT_RATIO")),
            "progress": r.get("ASSIGN_PROGRESS") or "",
            "ex_date": str(r.get("EX_DIVIDEND_DATE") or "")[:10],
        })
    return out


# ---------------------------------------------------------------- 新闻 / 公告

def stock_news(code: str, count: int = 20) -> list[dict]:
    """个股相关新闻(东财全文检索)。

    注意:该接口按 TLS 指纹分流 —— python-requests 会拿到「股吧用户搜索」的降级结果,
    curl 才是真新闻。故优先走 curl,失败再退回 requests。
    """
    import json as _json
    payload = {
        "uid": "", "keyword": normalize_code(code),
        "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web",
        "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time",
                                       "pageIndex": 1, "pageSize": count,
                                       "preTag": "<em>", "postTag": "</em>"}},
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {"cb": "cb", "param": _json.dumps(payload, ensure_ascii=False)}
    headers = {"Referer": "https://so.eastmoney.com/"}
    try:
        txt = http.get_text_curl(url, params=params, headers=headers)
    except Exception:
        txt = http.get_text(url, params=params, headers=headers)
    js = http.parse_jsonp(txt) if not txt.strip().startswith("{") else _json.loads(txt)
    items = (((js or {}).get("result") or {}).get("cmsArticleWebOld")) or []
    out = []
    for it in items:
        out.append({
            "title": _strip_tag(it.get("title")),
            "summary": _strip_tag(it.get("content")),
            "source": it.get("mediaName"),
            "time": it.get("date"),
            "url": it.get("url"),
        })
    return out


def market_flash(count: int = 50) -> list[dict]:
    """东财 7x24 快讯。"""
    url = f"https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{count}_1_.html"
    try:
        txt = http.get_text(url, headers={"Referer": "https://kuaixun.eastmoney.com/"})
    except Exception:
        return []
    import json as _json
    body = txt.split("=", 1)[1].strip().rstrip(";") if "=" in txt else txt
    try:
        js = _json.loads(body)
    except Exception:
        return []
    out = []
    for it in js.get("LivesList") or []:
        out.append({
            "title": it.get("title") or it.get("simtitle"),
            "summary": _strip_tag(it.get("digest")),
            "time": it.get("showtime") or it.get("time"),
            "url": it.get("url_w"),
            "source": "东财快讯",
        })
    return out


def announcements(code: str, count: int = 20) -> list[dict]:
    """个股公告。"""
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    params = {"sr": -1, "page_size": count, "page_index": 1, "ann_type": "A",
              "client_source": "web", "stock_list": normalize_code(code), "f_node": 0}
    js = http.get_json(url, params=params, headers={"Referer": "https://data.eastmoney.com/"})
    lst = ((js or {}).get("data") or {}).get("list") or []
    out = []
    for it in lst:
        col = (it.get("columns") or [{}])[0].get("column_name", "")
        out.append({
            "title": it.get("title"),
            "date": it.get("notice_date", "")[:10],
            "type": col,
            "code": (it.get("art_code") or ""),
            "url": f"https://data.eastmoney.com/notices/detail/{normalize_code(code)}/{it.get('art_code')}.html",
        })
    return out


def _strip_tag(s: str | None) -> str:
    if not s:
        return ""
    import re
    return re.sub(r"<[^>]+>", "", s).strip()


# ---------------------------------------------------------------- 涨停 / 炸板 / 跌停池

PUSH2EX = "https://push2ex.eastmoney.com"
_POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"
# kind → (接口, 排序, 需要的字段口径)
POOL_KINDS = {
    "zt": ("getTopicZTPool", "fbt:asc"),     # 涨停池(含连板数 lbc)
    "zb": ("getTopicZBPool", "fbt:asc"),     # 炸板池(盘中触板未封住)
    "dt": ("getTopicDTPool", "fund:asc"),    # 跌停池
    "qs": ("getTopicQSPool", "zdp:desc"),    # 强势股池(未涨停但强势)
}


def _pool_time(v) -> str:
    """东财 92501 → 09:25:01;空值返回空串,别显示 00:00:00。"""
    try:
        s = f"{int(v):06d}"
    except (TypeError, ValueError):
        return ""
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _pool_price(v) -> float | None:
    """池接口价格单位是 0.001 元(27710 → 27.71)。"""
    p = safe_float(v)
    return None if p is None else round(p / 1000.0, 2)


def zt_pool(day: str, kind: str = "zt", page_size: int = 500,
            retries: int = 2) -> dict:
    """某交易日涨停池 / 炸板池 / 跌停池。

    day 必须是 YYYYMMDD,且是交易日 —— 接口不接受空日期,传周末/节假日返回空。
    返回 {"day","kind","total","rows"};rows 已归一化成统一字段:
    code/name/price/pct_chg/amount/float_cap/turnover/sector/boards/
    first_time/last_time/break_count/fund/zt_price/amplitude/days。
    连板数 boards 只有涨停池有,其它池为 None —— 不要拿 1 冒充。
    """
    kind = str(kind or "zt").lower()
    if kind not in POOL_KINDS:
        raise ValueError(f"未知的池类型:{kind}")
    ep, sort = POOL_KINDS[kind]
    url = f"{PUSH2EX}/{ep}"
    rows: list[dict] = []
    total = 0
    page = 0
    while page < 4:
        params = {
            "ut": _POOL_UT, "dpt": "wz.ztzt", "Pageindex": page,
            "pagesize": int(page_size), "sort": sort, "date": str(day),
            "fields": "c,m,n,p,ztp,zdp,amount,ltsz,tshare,hs,lbc,fbt,lbt,fund,"
                      "zbc,zf,days,oc,hybk,zttj",
        }
        try:
            js = http.get_json(url, params=params, headers=EM_HEADERS,
                               retries=retries, timeout=10.0)
        except Exception:
            if rows:
                break
            raise
        data = (js or {}).get("data") or {}
        if not data:
            break
        total = int(safe_float(data.get("tc")) or 0)
        batch = data.get("pool") or []
        rows.extend(_row_to_pool(r, kind) for r in batch)
        if len(rows) >= total or not batch:
            break
        page += 1
    return {"day": str(day), "kind": kind, "total": total or len(rows),
            "rows": rows}


def _row_to_pool(d: dict, kind: str) -> dict:
    """池接口一行 → 统一结构。缺失字段一律 None,不用 0 顶替。"""
    zttj = d.get("zttj") or {}
    boards = safe_float(d.get("lbc")) if kind == "zt" else None
    return {
        "code": normalize_code(d.get("c") or ""),
        "name": d.get("n") or "",
        "price": _pool_price(d.get("p")),
        "pct_chg": safe_float(d.get("zdp")),
        "amount": safe_float(d.get("amount")),
        "float_cap": safe_float(d.get("ltsz")),
        "turnover": safe_float(d.get("hs")),
        "sector": d.get("hybk") or "",
        "boards": int(boards) if boards is not None else None,
        "first_time": _pool_time(d.get("fbt")),
        "last_time": _pool_time(d.get("lbt")),
        "break_count": int(safe_float(d.get("zbc")) or 0),
        "fund": safe_float(d.get("fund")),
        "zt_price": _pool_price(d.get("ztp")),
        "amplitude": safe_float(d.get("zf")),
        "days": int(safe_float(d.get("days") or zttj.get("days")) or 0),
        "kind": kind,
    }


# ---------------------------------------------------------------- 沪深港通 / 其他

def northbound() -> dict:
    """北向资金净流入(当日)。接口不稳定时返回空。"""
    url = "https://push2.eastmoney.com/api/qt/kamt/get"
    params = {"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55,f56", "ut": "b2884a393a59ad64002292a3e90d46a5"}
    try:
        js = http.get_json(url, params=params, headers=EM_HEADERS)
        d = (js or {}).get("data") or {}
        return {"sh_net": safe_float(d.get("hk2sh", {}).get("netBuyAmt")),
                "sz_net": safe_float(d.get("hk2sz", {}).get("netBuyAmt"))}
    except Exception:
        return {"sh_net": None, "sz_net": None}
