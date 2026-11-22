# -*- coding: utf-8 -*-
"""实时选股:全市场快照 → DataFrame → 条件表达式筛选 / 预设策略。

表达式只允许:列名、数字、算术与比较运算符、and/or/not/in、括号。
不支持函数调用与属性访问(防注入、也防手滑写坏)。
"""
from __future__ import annotations

import re
import time

import numpy as np
import pandas as pd

from ..config import cfg
from ..data import em
from ..utils import normalize_code, safe_float

SNAPSHOT_TTL = 45.0
_snap_cache: dict = {"ts": 0.0, "df": None}

# 表达式里可用的列
COLUMNS = [
    "code", "name", "price", "pct_chg", "change", "volume", "amount", "amplitude",
    "turnover", "volume_ratio", "pe_ttm", "pb", "market_cap", "float_cap",
    "main_inflow", "main_ratio", "super_inflow", "big_inflow", "mid_inflow", "small_inflow",
    "high", "low", "open", "pre_close", "board",
]

_KEYWORDS = {"and", "or", "not", "in", "True", "False", "None"}
_ALLOWED_OPS = {"==", "!=", ">=", "<=", ">", "<", "=", "-", "+", "*", "/", "%", "(", ")", ","}


def snapshot(force: bool = False) -> pd.DataFrame:
    """全 A 股实时快照(缓存 45 秒)。"""
    now = time.time()
    df = _snap_cache.get("df")
    if df is not None and not force and now - _snap_cache.get("ts", 0) < SNAPSHOT_TTL:
        return df

    rows = em.market_list_all("f3", asc=False, limit=6000)
    if not rows:
        raise RuntimeError("无法获取全市场行情(数据源限流或断网),稍后重试")
    out = []
    for r in rows:
        out.append({
            "code": r["code"], "name": r["name"], "price": r["price"],
            "pct_chg": r["change_pct"], "change": r["change"],
            "volume": r["volume"], "amount": r["amount"], "amplitude": r["amplitude"],
            "turnover": r["turnover"], "volume_ratio": r["volume_ratio"],
            "pe_ttm": r["pe_ttm"], "pb": r["pb"],
            "market_cap": r["market_cap"], "float_cap": r["float_cap"],
            "main_inflow": r["main_inflow"], "main_ratio": r["main_ratio"],
            "super_inflow": r["super_inflow"], "big_inflow": r["big_inflow"],
            "mid_inflow": r["mid_inflow"], "small_inflow": r["small_inflow"],
            "high": r["high"], "low": r["low"], "open": r["open"], "pre_close": r["pre_close"],
        })
    df = pd.DataFrame(out)
    from ..utils import board_of
    df["board"] = df["code"].map(board_of)
    df = df.dropna(subset=["price"])
    _snap_cache["df"] = df
    _snap_cache["ts"] = now
    return df


def validate_expr(expr: str, columns: list[str] | None = None) -> tuple[bool, str]:
    """语法白名单校验,返回 (ok, 错误信息)。"""
    cols = set(columns or COLUMNS)
    if not expr or not expr.strip():
        return False, "条件为空"
    # 先把比较/算术识别一遍:用简单分词
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+\.?\d*(?:[eE][+-]?\d+)?|"
                        r'==|!=|>=|<=|>|<|=|[-+*/%()]|,', expr)
    for t in tokens:
        if t in _KEYWORDS or t in cols:
            continue
        if re.fullmatch(r"\d+\.?\d*(?:[eE][+-]?\d+)?", t):
            continue
        if t in {"==", "!=", ">=", "<=", ">", "<", "=", "-", "+", "*", "/", "%", "(", ")", ","}:
            continue
        return False, f"不支持的写法:{t}(只允许列名、数字、运算符、and/or/not/in)"
    if "__" in expr or "import" in expr or "lambda" in expr:
        return False, "表达式含禁止内容"
    # 以运算符/连接词结尾 = 表达式不完整("pct_chg >" 这种能过白名单但算不出来)
    tail = expr.rstrip()
    if re.search(r"(==|!=|>=|<=|>|<|=|\+|\-|\*|/|%|\band|\bor|\bnot|\bin)$", tail):
        return False, "表达式不完整(结尾是运算符)"
    if tail.count("(") != tail.count(")"):
        return False, "括号不成对"
    return True, ""


def run(expr: str, df: pd.DataFrame | None = None, limit: int | None = 200,
        sort_by: str | None = None, ascending: bool = False) -> pd.DataFrame:
    """按表达式筛选。返回命中的行。"""
    ok, err = validate_expr(expr)
    if not ok:
        raise ValueError(err)
    data = snapshot() if df is None else df
    try:
        mask = data.eval(expr, engine="python")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"条件执行失败:{e}") from e
    hit = data[mask.fillna(False)] if hasattr(mask, "fillna") else data[mask]
    if sort_by and sort_by in hit.columns:
        hit = hit.sort_values(sort_by, ascending=ascending)
    if limit:
        hit = hit.head(limit)
    return hit.reset_index(drop=True)


def preset_list() -> list[dict]:
    from ..config import cfg
    return cfg().get("screener.presets", []) or []


def save_preset(name: str, expr: str, desc: str = "") -> dict:
    """把一条选股表达式存进库,以后下拉直接选。"""
    name = (name or "").strip()
    expr = (expr or "").strip()
    if not name or not expr:
        raise ValueError("名称和表达式都要填")
    ok, why = validate_expr(expr)
    if not ok:
        raise ValueError(f"表达式有误:{why}")
    c = cfg()
    presets = list(c.get("screener.presets", []) or [])
    presets = [x for x in presets if x.get("name") != name]
    presets.append({"name": name, "expr": expr, "desc": desc.strip()})
    c.set("screener.presets", presets, save=True)
    return presets[-1]


def delete_preset(name: str) -> bool:
    c = cfg()
    presets = list(c.get("screener.presets", []) or [])
    left = [x for x in presets if x.get("name") != name]
    if len(left) == len(presets):
        return False
    c.set("screener.presets", left, save=True)
    return True


def run_preset(name: str, limit: int = 200) -> pd.DataFrame:
    for p in preset_list():
        if p.get("name") == name:
            return run(p["expr"], limit=limit, sort_by="main_inflow")
    raise KeyError(f"没有预设 {name}")


def explain(df: pd.DataFrame) -> str:
    """给筛选结果一句话摘要。"""
    if df is None or df.empty:
        return "无命中"
    up = (df["pct_chg"] > 0).sum()
    inflow = df["main_inflow"].fillna(0).sum()
    return (f"命中 {len(df)} 只,上涨 {up} 只,主力合计净流入 {inflow / 1e8:.2f} 亿,"
            f"涨幅中位数 {df['pct_chg'].median():.2f}%")


def to_rows(df: pd.DataFrame) -> list[dict]:
    """DataFrame → 界面行(带展示字段)。"""
    if df is None or df.empty:
        return []
    rows = []
    for r in df.to_dict("records"):
        rows.append({
            "code": r.get("code"), "name": r.get("name"),
            "price": r.get("price"), "pct_chg": r.get("pct_chg"),
            "turnover": r.get("turnover"), "volume_ratio": r.get("volume_ratio"),
            "amount": r.get("amount"), "main_inflow": r.get("main_inflow"),
            "main_ratio": r.get("main_ratio"), "pe_ttm": r.get("pe_ttm"),
            "market_cap": r.get("market_cap"), "board": r.get("board"),
        })
    return rows


# ==================================================================== 按资金量筛选

DEFAULT_SCREEN_WEIGHTS = {"flow": 0.30, "theme": 0.30, "news": 0.30, "ext": 0.20}


def _z(s: pd.Series) -> pd.Series:
    """单列稳健标准化:截尾到 ±3。缺失保持 NaN,不填 0。"""
    v = pd.to_numeric(s, errors="coerce").astype("float64")
    v = v.replace([float("inf"), float("-inf")], float("nan"))
    if v.notna().sum() < 2:
        return pd.Series(float("nan"), index=s.index)
    mu, sd = float(v.mean()), float(v.std())
    if not sd or sd != sd:
        return pd.Series(0.0, index=s.index).where(v.notna())
    return ((v - mu) / sd).clip(-3, 3)


def screen(alpha_row: pd.Series, info: pd.DataFrame, capital: float, n_hold: int = 30,
           overlays: dict | None = None, weights: dict | None = None,
           part: float = 0.05, min_cap: float = 5e9, min_list_days: int = 250,
           lot: int = 100, keep_alpha_nan: bool = False) -> tuple[pd.DataFrame, pd.Series]:
    """先按资金量硬过滤,再叠加信号打分,输出可解释的候选表。

    alpha_row: Series(代码 → 模型 alpha),允许 NaN(没算出来的因子不参与)
    info:      DataFrame(index=代码),列:
               price 现价 / adv 日均成交额 / mktcap 市值 / name / list_days 上市天数
               / blocked(可选,涨停停牌 True) / float_cap(可选)
    overlays:  {"flow","theme","news","ext": Series, "veto": 布尔 Series}
               这四项**没有可回测的历史**,权重刻意保守(见 DEFAULT_SCREEN_WEIGHTS)
    part:      单票预算占日均成交额的上限比例 —— 0.05 = 不超过当日成交额的 5%
    keep_alpha_nan: True 时 alpha 缺失也能入选(只按叠加分排序),默认不选。

    返回 (候选表, 过滤原因计数 Series)。
    候选表列:alpha_z / 各叠加项 z / final / weight / why。
    """
    w = dict(DEFAULT_SCREEN_WEIGHTS)
    w.update(weights or {})
    overlays = overlays or {}
    df = info.reindex(alpha_row.index).copy()
    df["alpha"] = pd.to_numeric(alpha_row, errors="coerce")
    budget = float(capital) / max(int(n_hold), 1)
    why = pd.Series("", index=df.index, dtype="object")

    def flag(mask, text: str) -> None:
        nonlocal why
        m = pd.Series(mask).reindex(df.index).fillna(False).astype(bool)
        why = why.where(~m, why + text + ";")

    if "name" in df:
        flag(df["name"].astype(str).str.contains("ST", na=False), "ST")
    if "list_days" in df:
        flag(pd.to_numeric(df["list_days"], errors="coerce") < min_list_days, "次新")
    if "mktcap" in df:
        flag(pd.to_numeric(df["mktcap"], errors="coerce") < min_cap, "市值过小")
    if "adv" in df:
        adv = pd.to_numeric(df["adv"], errors="coerce")
        # 容量:单票预算不能超过日均成交额的 part
        flag(adv * part < budget, "流动性不足(容量)")
        cap_w = part * adv / max(float(capital), 1.0)
    else:
        cap_w = pd.Series(float("nan"), index=df.index)
    if "price" in df:
        px = pd.to_numeric(df["price"], errors="coerce")
        flag(px * lot > budget, "一手买不起")
    if "blocked" in df:
        flag(df["blocked"].astype(bool), "涨停/停牌")
    if "veto" in overlays:
        veto = overlays["veto"]
        if isinstance(veto, pd.Series):
            flag(veto.reindex(df.index).fillna(False), "负面事件")
    if not keep_alpha_nan:
        flag(df["alpha"].isna(), "无alpha")

    df["alpha_z"] = _z(df["alpha"])
    final = df["alpha_z"].copy()
    for k in ("flow", "theme", "news", "ext"):
        if k not in overlays:
            continue
        s = overlays[k]
        if not isinstance(s, pd.Series):
            s = pd.Series(s)
        df[k] = _z(s.reindex(df.index))
        final = final + float(w.get(k, 0.0)) * df[k]
    # 没有 alpha 的票不给分,免得一个纯叠加信号被当成有效 alpha
    if not keep_alpha_nan:
        final = final.where(df["alpha"].notna())
    df["final"] = final.where(why == "")
    df["why"] = why
    # 单票权重:等权与容量上限取小;容量按当日成交额算,拿不到就不限
    eq = 1.0 / max(int(n_hold), 1)
    df["weight"] = np.minimum(eq, cap_w)

    ok = df.dropna(subset=["final"]).sort_values("final", ascending=False)
    top = ok.head(int(n_hold)).copy()
    cols = ["alpha_z"] + [k for k in ("flow", "theme", "news", "ext") if k in df] + \
        ["final", "weight"]
    reasons = why[why != ""].str.rstrip(";").str.split(";").explode()
    return top[cols], reasons[reasons != ""].value_counts()


def capital_filter(df: pd.DataFrame, capital: float, n_hold: int = 10,
                   part: float = 0.05, min_cap: float = 5e9,
                   min_list_days: int = 250, lot: int = 100) -> pd.Series:
    """只做硬过滤(不排序),返回每只票的过滤原因串(空 = 通过)。

    给选股页/自动交易用:先看「买得起吗」,再看分数。
    """
    budget = float(capital) / max(int(n_hold), 1)
    why = pd.Series("", index=df.index, dtype="object")

    def flag(mask, text: str) -> None:
        nonlocal why
        m = pd.Series(mask).reindex(df.index).fillna(False).astype(bool)
        why = why.where(~m, why + text + ";")

    if "name" in df:
        flag(df["name"].astype(str).str.contains("ST", na=False), "ST")
    if "list_days" in df:
        flag(pd.to_numeric(df["list_days"], errors="coerce") < min_list_days, "次新")
    if "market_cap" in df:
        flag(pd.to_numeric(df["market_cap"], errors="coerce") < min_cap, "市值过小")
    if "amount" in df:
        flag(pd.to_numeric(df["amount"], errors="coerce") * part < budget,
             "流动性不足(容量)")
    if "price" in df:
        flag(pd.to_numeric(df["price"], errors="coerce") * lot > budget, "一手买不起")
    return why.str.rstrip(";")


def candidates(df: pd.DataFrame, top: int = 20, sort_by: str = "main_inflow") -> list[dict]:
    """把筛选结果转成候选清单(供交易面板/回测使用)。"""
    if df is None or df.empty:
        return []
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False)
    out = []
    for r in df.head(top).to_dict("records"):
        out.append({"code": normalize_code(r["code"]), "name": r.get("name"),
                    "price": safe_float(r.get("price")),
                    "score": safe_float(r.get("main_inflow"), 0.0),
                    "reason": f"涨幅{safe_float(r.get('pct_chg'), 0):.2f}% "
                              f"主力{safe_float(r.get('main_inflow'), 0) / 1e4:.0f}万"})
    return out
