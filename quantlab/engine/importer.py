# -*- coding: utf-8 -*-
"""外部策略导入拆解:把别人给的买卖策略文本,拆成买点/卖点两条可执行表达式。

支持的输入形态:
  1. 带标签的纯文本    买点:close > ma(20) / 卖点:close < ma(20)
  2. JSON               {"buy": "...", "sell": "..."}
  3. 自然语言常见句式   「5 日均线上穿 20 日均线时买入」
  4. 代码块风格         if close > ma(20): buy

解析完统一输出 {name, buy, sell, notes[], warnings[]},
两条表达式都过 rules.validate(),过不了的明确标出来,不硬凑。
"""
from __future__ import annotations

import json
import re

from . import dsl
from . import rules as rules_mod

# 买卖标记(同一行里出现在条件前面就算方向)
BUY_TAGS = ("买入", "买点", "买进", "开仓", "入场", "进场", "做多", "建仓",
            "buy", "long", "entry", "enter")
SELL_TAGS = ("卖出", "卖点", "卖掉", "平仓", "出场", "离场", "止损", "止盈",
             "做空", "sell", "short", "exit", "stop")

# 自然语言 → DSL 片段。键做小写、去空格处理后匹配
PHRASES: list[tuple[str, str]] = [
    (r"(\d+)\s*日?均线(上穿|金叉|突破)(\d+)\s*日?均线", r"cross(ma({0}), ma({2}))"),
    (r"(\d+)\s*日?均线(下穿|死叉|跌破)(\d+)\s*日?均线", r"cross(ma({2}), ma({0}))"),
    (r"股价?上穿(\d+)\s*日?均线", r"cross(close, ma({0}))"),
    (r"股价?下穿(\d+)\s*日?均线", r"cross(ma({0}), close)"),
    (r"站上(\d+)\s*日?均线", r"close > ma({0})"),
    (r"跌破(\d+)\s*日?均线", r"close < ma({0})"),
    (r"RSI\s*[（(]?(\d+)[)）]?\s*低于\s*(\d+)", r"rsi({0}) < {1}"),
    (r"RSI\s*[（(]?(\d+)[)）]?\s*高于\s*(\d+)", r"rsi({0}) > {1}"),
    (r"RSI\s*(?:低于|小于)\s*(\d+)", r"rsi(6) < {0}"),
    (r"RSI\s*(?:高于|大于)\s*(\d+)", r"rsi(6) > {0}"),
    (r"MACD\s*(金叉|翻红|上穿)", r"macd_dif > macd_dea"),
    (r"MACD\s*(死叉|翻绿|下穿)", r"macd_dif < macd_dea"),
    (r"KDJ\s*(金叉)", r"kdj_k > kdj_d"),
    (r"KDJ\s*(死叉)", r"kdj_k < kdj_d"),
    (r"突破\s*(\d+)\s*日?(?:新高|最高价)", r"close > hhv(close, {0}) * 0.999"),
    (r"跌破\s*(\d+)\s*日?(?:新低|最低价)", r"close < llv(close, {0}) * 1.001"),
    (r"(?:放量|成交量放大)\s*(\d+(?:\.\d+)?)\s*倍", r"volume > vol_ma(20) * {0}"),
    (r"(?:缩量|成交量萎缩)\s*(?:到|至)?\s*(\d+)%", r"volume < vol_ma(20) * {0}00 / 100"),
    (r"布林(?:带)?下轨", r"close < boll_low"),
    (r"布林(?:带)?上轨", r"close > boll_up"),
    (r"布林中轨", r"close < boll_mid"),
    (r"涨幅?超(?:过)?\s*(\d+(?:\.\d+)?)%", r"pct(1) > {0}"),
    (r"跌幅?超(?:过)?\s*(\d+(?:\.\d+)?)%", r"pct(1) < -{0}"),
]

# 语句里出现这些词,就算「不看方向」也能判断
_NEUTRAL_HINT = ("条件", "signal", "if ")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _try_json(text: str) -> dict | None:
    raw = text.strip()
    if not raw.startswith(("{", "[")):
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if isinstance(d, dict):
        buy = d.get("buy") or d.get("买入") or d.get("entry") or d.get("long")
        sell = d.get("sell") or d.get("卖出") or d.get("exit") or d.get("short")
        if buy or sell:
            return {"name": d.get("name") or d.get("名称") or "", "buy": buy or "",
                    "sell": sell or "", "desc": d.get("desc") or d.get("说明") or ""}
    return None


def _tag_direction(line: str) -> str | None:
    """一行里出现在最前的买卖标记决定方向。"""
    low = line.lower()
    pos_b = min([low.find(t) for t in BUY_TAGS if t in low] or [10 ** 9])
    pos_s = min([low.find(t) for t in SELL_TAGS if t in low] or [10 ** 9])
    if pos_b == pos_s == 10 ** 9:
        return None
    return "buy" if pos_b < pos_s else "sell"


def _strip_tag(line: str) -> str:
    """去掉行首的方向标记与冒号。"""
    out = line
    for t in BUY_TAGS + SELL_TAGS:
        out = re.sub(rf"^\s*{re.escape(t)}\s*[:：]?\s*", "", out, flags=re.I)
        out = re.sub(r"^\s*(?:当|如果|若)\s*", "", out)
    return out.strip()


def _extract_expr(line: str) -> str:
    """从一行里抠出可能是条件表达式的部分。"""
    body = _strip_tag(line)
    # 去掉「时买入」「则卖出」这类尾巴
    body = re.sub(r"[，,]?\s*(?:时|就|则)?\s*(?:买入|卖出|买进|平仓|开仓|入场|出场).*$", "", body)
    body = re.sub(r"^\s*(?:if|when)\s+", "", body, flags=re.I)
    body = re.sub(r"[:：]\s*(?:buy|sell|long|short)\s*$", "", body, flags=re.I)
    return body.strip().strip("，,。.;；")


def _nl_to_dsl(text: str) -> str:
    """自然语言 → DSL。逐条短语映射,拼成 and。"""
    out: list[str] = []
    rest = text
    for pattern, tpl in PHRASES:
        for m in re.finditer(pattern, rest, flags=re.I):
            try:
                expr = tpl.format(*[g for g in m.groups() if g is not None])
            except (IndexError, KeyError):
                continue
            if expr not in out:
                out.append(expr)
            rest = rest.replace(m.group(0), " ")
    return " and ".join(out)


def _looks_like_dsl(text: str) -> bool:
    """粗判是不是已经是 DSL(能被 rules 解析)。"""
    ok, _why = rules_mod.validate(text)
    return ok


# ---------------------------------------------------------- 通达信/大智慧公式

# 标识符:英文、数字、下划线、中文(通达信变量名常是中文)
_IDENT = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")

# 通达信函数/简写 → DSL 名。赋值过的变量优先,不会被这里覆盖
_TDX_NAME = {
    "MA": "ma", "EMA": "ema", "SMA": "sma", "REF": "ref", "HHV": "hhv", "LLV": "llv",
    "ATAN": "atan", "FILTER": "filter", "CAPITAL": "capital", "DYNAINFO": "dynainfo",
    "CROSS": "cross", "IF": "iff", "IFF": "iff", "ABS": "abs", "MAX": "max", "MIN": "min",
    "SUM": "sum", "COUNT": "count", "RSI": "rsi", "ATR": "atr", "PCT": "pct",
    "CLOSE": "close", "OPEN": "open", "HIGH": "high", "LOW": "low", "VOL": "volume",
    "C": "close", "O": "open", "H": "high", "L": "low", "V": "volume",
    "AMO": "amo", "AMOUNT": "amount", "VOLUME": "volume",
    "AND": "and", "OR": "or", "NOT": "not",
}

_ASSIGN = re.compile(r"([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s*:=\s*(.+)", re.S)
_OUTPUT = re.compile(r"([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)\s*:\s*(.+)", re.S)


def looks_like_tdx(text: str) -> bool:
    """带 := 赋值、或形如 `名字:表达式;` 的多语句公式,按通达信公式处理。"""
    if ":=" in text:
        return True
    return bool(re.search(r"[;；]", text) and re.search(r"[A-Za-z\u4e00-\u9fff]\s*:\s*\S", text))


def _strip_tdx_comments(text: str) -> str:
    text = re.sub(r"\{[^}]*\}", " ", text)          # 通达信块注释
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _statements(text: str) -> list[str]:
    """按分号拆语句(通达信没有字符串字面量,直接拆即可)。"""
    parts = re.split(r"[;；]", text)
    return [p.strip() for p in parts if p.strip()]


def _expand_vars(expr: str, vars_: dict[str, str], rounds: int = 24) -> str:
    """把变量递归内联展开成一条不含变量的表达式。"""
    for _ in range(rounds):
        hit = False

        def rep(m: re.Match) -> str:
            nonlocal hit
            name = m.group(0)
            if name in vars_:
                hit = True
                return f"({vars_[name]})"
            return name

        new = _IDENT.sub(rep, expr)
        if not hit:
            return new
        expr = new
    raise ValueError("变量展开层数过深(可能有循环定义)")


def _map_names(expr: str, vars_: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """函数名与行情简写换成 DSL 名;返回 (表达式, 不认识的标识符)。

    通达信里 CAPITAL、MACD 这类是「无参量」,直接写名字不带括号,
    翻成 DSL 后补上括号;公式里定义过的变量名原样保留(v2 支持 := 变量)。
    """
    unknown: list[str] = []
    known_vars = set(vars_ or ())
    noarg = {mapped for mapped in _TDX_NAME.values()
             if mapped in dsl.FUNC_SPECS and dsl.FUNC_SPECS[mapped]["arity"][0] == 0}

    def rep(m: re.Match) -> str:
        name = m.group(0)
        mapped = _TDX_NAME.get(name)
        if mapped is None:
            low = name.lower()
            if name in known_vars:
                return name
            if low in _TDX_NAME.values() or low in dsl.COLUMNS or low in dsl.ALIASES:
                return low
            if low in dsl.FUNC_SPECS:
                return low
            if re.fullmatch(r"\d+", name):
                return name
            unknown.append(name)
            return name
        if mapped in noarg:
            rest = expr[m.end():]
            if not rest.lstrip().startswith("("):
                return mapped + "()"
        return mapped

    return _IDENT.sub(rep, expr), unknown


def _tdx_to_expr(expr: str, vars_: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """把一条通达信表达式翻成 DSL 片段(变量名保留,由调用方补 := 定义)。"""
    body, unknown = _map_names(expr.strip(), vars_)
    body = body.replace("<>", "!=")
    # 通达信条件里用单个 = 表示等于;已处理 >= <= != ==,剩下的才是它
    body = re.sub(r"(?<![<>!=])=(?!=)", "==", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body, unknown


def _vars_in(expr: str, vars_: dict[str, str]) -> list[str]:
    """这条表达式用到了哪些变量(按定义顺序返回,含传递依赖)。"""
    used: set[str] = set()

    def walk(body: str) -> None:
        for name in _IDENT.findall(body):
            if name in vars_ and name not in used:
                used.add(name)
                walk(vars_[name])

    walk(expr)
    return [k for k in vars_ if k in used]


def _with_vars(expr: str, vars_: dict[str, str]) -> str:
    """把用到的变量定义挂到表达式前面,一行一个 :=。"""
    if not vars_:
        return expr
    names = _vars_in(expr, vars_)
    if not names:
        return expr
    head = "\n".join(f"{n} := {vars_[n]}" for n in names)
    return f"{head}\n{expr}"


def parse_tdx(text: str) -> dict:
    """解析通达信/大智慧公式:变量赋值 + 最后一行输出当条件。

    返回 {buy, sell, notes, warnings, vars};解析不出条件时 buy 为空。
    """
    notes: list[str] = []
    warnings: list[str] = []
    body = _strip_tdx_comments(text)
    vars_: dict[str, str] = {}
    outputs: list[tuple[str, str]] = []

    for stmt in _statements(body):
        m = _ASSIGN.match(stmt)
        if m:
            vars_[m.group(1)] = m.group(2).strip()
            continue
        m = _OUTPUT.match(stmt)
        if m:
            outputs.append((m.group(1), m.group(2).strip()))
            continue
        # 没有名字的裸表达式也算一个输出(少见)
        outputs.append(("", stmt))

    notes.append(f"按通达信公式解析:变量 {len(vars_)} 个、输出 {len(outputs)} 条")

    if not outputs:
        warnings.append("公式里只有赋值没有输出行(应写成 名字:条件;)")
        return {"buy": "", "sell": "", "notes": notes, "warnings": warnings, "vars": vars_}

    buys: list[str] = []
    sells: list[str] = []
    kept_vars: dict[str, str] = {}
    for label, expr in outputs:
        code, unknown = _tdx_to_expr(expr, vars_)
        if unknown:
            warnings.append(f"「{label or expr[:12]}」里有不认识的写法:"
                            + "、".join(dict.fromkeys(unknown)))
        # 用到的变量定义本身也得能翻成 DSL,才跟着一起存
        ok_vars = True
        for name in _vars_in(code, vars_):
            if name in kept_vars:
                continue
            vcode, vunknown = _tdx_to_expr(vars_[name], vars_)
            cand = dict(kept_vars)
            cand[name] = vcode
            # 变量定义里会引用别的变量,要连着变量表一起校验
            if vunknown or not _looks_like_dsl(_with_vars(vcode, cand)):
                warnings.append(f"变量「{name}」翻不出来,这条改成内联展开")
                try:
                    code = _expand_vars(code, vars_)
                except ValueError as e:
                    warnings.append(f"「{label or expr[:12]}」展开失败:{e}")
                    ok_vars = False
                break
            kept_vars[name] = vcode
        if not ok_vars:
            continue
        ok, why = rules_mod.validate(_with_vars(code, kept_vars))
        if not ok:
            warnings.append(f"「{label or expr[:12]}」翻译后仍不合法:{why}")
            continue
        if label:
            notes.append(f"{label} → {code[:60]}{'…' if len(code) > 60 else ''}")
        target = sells if re.search(r"卖|空|SELL|EXIT", label or "", re.I) else buys
        target.append(code)

    buy_expr = " or ".join(dict.fromkeys(buys))
    sell_expr = " or ".join(dict.fromkeys(sells))
    if kept_vars:
        if buy_expr and sell_expr:
            # 买卖是两条独立表达式,各挂一份变量定义,否则卖点找不到买点里的变量
            buy_expr = _with_vars(buy_expr, kept_vars)
            sell_expr = _with_vars(sell_expr, kept_vars)
            notes.append(f"变量 {len(kept_vars)} 个按 := 保留(买卖各挂一份)")
        else:
            one = _with_vars(buy_expr or sell_expr, kept_vars)
            buy_expr, sell_expr = (one, sell_expr) if buy_expr else (buy_expr, one)
            notes.append(f"变量 {len(kept_vars)} 个按 := 保留(DSL v2 支持)")
    elif vars_ and buy_expr:
        notes.append("变量已全部内联展开,不保留中间变量")
    return {"buy": buy_expr, "sell": sell_expr, "notes": notes,
            "warnings": warnings, "vars": vars_, "dsl_vars": kept_vars}


def parse_strategy(text: str, default_name: str = "导入策略",
                   name: str | None = None) -> dict:
    """把一段外部策略文本拆成买点/卖点。

    返回 {name, desc, buy, sell, notes, warnings, ok}
    """
    raw = str(text or "").strip()
    notes: list[str] = []
    warnings: list[str] = []
    if not raw:
        return {"name": name or default_name, "desc": "", "buy": "", "sell": "",
                "notes": [], "warnings": ["内容是空的"], "ok": False}

    # 1) JSON
    j = _try_json(raw)
    if j:
        buy = str(j.get("buy") or "").strip()
        sell = str(j.get("sell") or "").strip()
        notes.append("按 JSON 结构解析")
        return _finish(name or j.get("name") or default_name, j.get("desc") or "",
                       buy, sell, notes, warnings)

    # 2) 通达信/大智慧公式(: = 变量赋值 + `名字:条件` 输出行)
    if looks_like_tdx(raw):
        t = parse_tdx(raw)
        notes.extend(t["notes"])
        warnings.extend(t["warnings"])
        if t["buy"] or t["sell"]:
            return _finish(name or default_name, "", t["buy"], t["sell"], notes, warnings)
        warnings.append("通达信公式没解析出可用的条件,确认最后一行是不是 `名字:条件;`")

    # 3) 按行拆方向;一行里买卖混写(全靠逗号隔开)时再按逗号细分
    lines = [_norm(x) for x in re.split(r"[\r\n;；]+", raw) if _norm(x)]
    pieces: list[str] = []
    for line in lines:
        if _tag_direction(line) == "buy" and any(t in line.lower() for t in SELL_TAGS):
            pieces.extend(x for x in re.split(r"[，,、]", line) if _norm(x))
        else:
            pieces.append(line)

    buys: list[str] = []
    sells: list[str] = []
    for line in pieces:
        direction = _tag_direction(line)
        if direction is None:
            continue
        expr = _extract_expr(line)
        if not expr:
            continue
        # 3) 已经是 DSL 就直接用,否则尝试自然语言翻译
        expr2 = expr if _looks_like_dsl(expr) else _nl_to_dsl(expr)
        if not expr2:
            warnings.append(f"这句没看懂,已跳过:{line[:40]}")
            continue
        if expr2 != expr:
            notes.append(f"「{expr[:24]}」→ {expr2}")
        (buys if direction == "buy" else sells).append(expr2)

    if buys or sells:
        buy_expr = " and ".join(dict.fromkeys(buys))
        sell_expr = " and ".join(dict.fromkeys(sells))
        notes.insert(0, f"按行解析:买点 {len(buys)} 条、卖点 {len(sells)} 条")
        return _finish(name or default_name, "", buy_expr, sell_expr, notes, warnings)

    # 4) 整段没标签:先试 DSL,再试自然语言,猜方向    if _looks_like_dsl(raw):
        notes.append("整段就是一条表达式,默认当买点")
        return _finish(name or default_name, "", raw, "", notes, warnings)
    dsl = _nl_to_dsl(raw)
    if dsl:
        notes.append("没找到买卖标记,按关键词翻译成条件,默认当买点")
        return _finish(name or default_name, "", dsl, "", notes, warnings)

    warnings.append("没解析出任何条件;可以直接按 DSL 写,如 close > ma(20)")
    return {"name": name or default_name, "desc": "", "buy": "", "sell": "",
            "notes": notes, "warnings": warnings, "ok": False}


def _finish(name: str, desc: str, buy: str, sell: str, notes: list[str],
            warnings: list[str]) -> dict:
    ok_buy, why_buy = (rules_mod.validate(buy) if buy else (True, ""))
    ok_sell, why_sell = (rules_mod.validate(sell) if sell else (True, ""))
    if buy and not ok_buy:
        warnings.append(f"买点语法有问题:{why_buy}")
    if sell and not ok_sell:
        warnings.append(f"卖点语法有问题:{why_sell}")
    if not buy and not sell:
        warnings.append("买点和卖点都空")
    return {"name": name, "desc": desc, "buy": buy, "sell": sell,
            "notes": notes, "warnings": warnings,
            "ok": bool((buy or sell) and ok_buy and ok_sell)}


def test_on(code: str, buy: str, sell: str, count: int = 300) -> dict:
    """拿一只股票试跑解析结果,给命中次数。"""
    from ..data import kline as kl
    from . import indicators as ind

    bars, _src = kl.get_kline(code, 101, count=count)
    if not bars:
        raise RuntimeError(f"{code} 没取到日线")
    df = ind.bars_to_df(bars)
    out = {"bars": len(df), "buy": 0, "sell": 0, "recent_buy": False}
    if buy:
        b = rules_mod.evaluate(buy, df, code)
        out["buy"] = int(b.sum())
        out["recent_buy"] = bool(b[-5:].any())
    if sell:
        s = rules_mod.evaluate(sell, df, code)
        out["sell"] = int(s.sum())
    return out


def examples() -> list[dict]:
    """几种常见外部写法,给用户照着贴。"""
    return [
        {"label": "带标签",
         "text": "买点:close > ma(20) and volume > vol_ma(20) * 1.5\n卖点:close < ma(10)"},
        {"label": "自然语言",
         "text": "5日均线上穿20日均线时买入\n跌破20日均线时卖出"},
        {"label": "JSON",
         "text": '{"name": "双均线", "buy": "cross(ma(5), ma(20))", '
                 '"sell": "cross(ma(20), ma(5))"}'},
        {"label": "英文",
         "text": "entry: close > ma(20)\nexit: close < ma(20) * 0.95"},
        {"label": "通达信公式",
         "text": "GS1:=CAPITAL<8000000 AND DYNAINFO(4)>0;\n"
                 "GS2:=CLOSE<45 AND REF(LOW,1)<MA(CLOSE,5);\n"
                 "GS3:=LOW<MA(CLOSE,5) AND CLOSE>=REF(CLOSE,1)*1.07;\n"
                 "选股:(GS3 AND GS1 AND GS2)*2>0;"},
    ]
