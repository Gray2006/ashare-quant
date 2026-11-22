# -*- coding: utf-8 -*-
"""Pine Script 子集 → 内部策略表达式(DSL)。

**只做翻译,不做完整 Pine 解释器**。这个模块把常见的「指标 + 交叉 + 进出场」
写法翻成 rules.py 能直接跑的条件表达式,翻不了的部分会明确报出来,不会瞎猜。

支持(够覆盖绝大多数量价策略模板):
    //@version=5
    strategy("名字", overlay=true)
    fast = ta.sma(close, 10)
    slow = ta.sma(close, 30)
    longCond = ta.crossover(fast, slow)
    if longCond
        strategy.entry("long", strategy.long)
    if ta.crossunder(fast, slow)
        strategy.close("long")

函数映射:
    ta.sma(x,n)→ma(x,n)      ta.ema(x,n)→ema(x,n)     ta.rsi(x,n)→rsi(n)
    ta.crossover(a,b)→cross(a,b)                        ta.crossunder(a,b)→cross(b,a)
    ta.highest(x,n)→hhv(x,n)  ta.lowest(x,n)→llv(x,n)   ta.atr(n)→atr(n)
    nz(x)→x  na→nan

不支持(会报错并指出第几行):for/while、var/varip 持久化、request.security 多周期、
数组与元组、plot/plotshape/alert 等绘图与告警(直接忽略,不算错)、
strategy.position_size 等账户相关内建变量。
"""
from __future__ import annotations

import re

FUNC_MAP = {
    "ta.sma": "ma",
    "ta.ema": "ema",
    "ta.rsi": "rsi",
    "ta.crossover": "cross",
    "ta.crossunder": "__crossunder__",
    "ta.highest": "hhv",
    "ta.lowest": "llv",
    "ta.atr": "atr",
    "ta.tr": "atr",
    "math.abs": "abs",
}

# 直接忽略的行(绘图/告警/策略声明)
IGNORE_PREFIX = ("plot(", "plotshape(", "plotchar(", "plotcandle(", "bgcolor(",
                 "alert(", "alertcondition(", "strategy(", "indicator(", "hline(",
                 "fill(", "label.", "line.", "//@version")

UNSUPPORTED = {
    "for ": "循环", "while ": "循环", "var ": "var 持久变量", "varip ": "varip 持久变量",
    "request.security": "多周期取数", "array.": "数组", "matrix.": "矩阵",
    "input.": "input 交互参数", "strategy.position_size": "账户仓位内建变量",
    "strategy.equity": "账户权益内建变量", "ta.pivothigh": "枢轴点",
    "ta.pivotlow": "枢轴点", "ta.vwap": "VWAP",
}

SERIES = {"close", "open", "high", "low", "volume", "hl2", "hlc3", "ohlc4"}


def _strip_comment(line: str) -> str:
    out = []
    in_str = False
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if in_str:
            out.append(ch)
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                in_str = False
        else:
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break
            if ch in "\"'":
                in_str = True
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _series_sub(expr: str) -> str:
    """hl2/hlc3/ohlc4 展开成算式。"""
    return (expr.replace("hlc3", "((high+low+close)/3)")
                .replace("hl2", "((high+low)/2)")
                .replace("ohlc4", "((open+high+low+close)/4)"))


def _rewrite_funcs(expr: str) -> str:
    """把 ta.xxx(...) 换成内部函数名。crossunder 单独处理(参数对调)。"""
    # crossunder(a, b) → cross(b, a)
    def _under(m):
        args = _split_args(m.group(1))
        if len(args) != 2:
            raise ValueError("ta.crossunder 需要两个参数")
        return f"cross({args[1].strip()}, {args[0].strip()})"

    expr = re.sub(r"ta\.crossunder\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", _under, expr)

    for pine, ours in FUNC_MAP.items():
        if ours == "__crossunder__":
            continue
        expr = re.sub(re.escape(pine) + r"\s*\(", ours + "(", expr)

    # nz(x) → x(内部没有 NaN 语义,直接取原值)
    expr = re.sub(r"\bnz\s*\(\s*([^,)]+)", r"(\1)", expr)
    expr = expr.replace(" na", " nan").replace("(na", "(nan")

    # rsi(close, 14) → rsi(14):内部 rsi 只吃周期,序列固定是收盘
    expr = re.sub(r"\brsi\(\s*(?:close|c)\s*,\s*([^,()]+?)\s*\)", r"rsi(\1)", expr)
    # atr 同理:Pine 的 ta.atr(n) 已是单参,但有人写 atr(high,low,close,n)
    expr = re.sub(r"\batr\(\s*high\s*,\s*low\s*,\s*close\s*,\s*([^,()]+?)\s*\)",
                  r"atr(\1)", expr)

    # x[n] → ref(x, n):DSL 用 ref 取前值,不支持下标
    expr = re.sub(r"([A-Za-z_][A-Za-z0-9_]*|\([^()]*\))\s*\[\s*(\d+)\s*\]",
                  r"ref(\1, \2)", expr)

    return _series_sub(expr)


def _find_kwarg(call: str, key: str) -> str | None:
    """从 `f(a, when = <表达式>, b=c)` 里抠出 when 的值。

    不能用正则的懒惰匹配 —— 表达式里带函数调用(自己的逗号)会被截断,
    结果翻出一个括号不匹配的残句。这里按括号深度扫到同层的逗号或右括号。
    """
    m = re.search(r"\b" + re.escape(key) + r"\s*=\s*", call)
    if not m:
        return None
    i = m.end()
    depth = 0
    out = []
    in_str, q = False, ""
    while i < len(call):
        ch = call[i]
        if in_str:
            out.append(ch)
            if ch == q:
                in_str = False
        elif ch in "\"'":
            in_str, q = True, ch
            out.append(ch)
        elif ch in "([":
            depth += 1
            out.append(ch)
        elif ch in ")]":
            if depth == 0:
                break          # 到本层右括号,参数结束
            depth -= 1
            out.append(ch)
        elif ch == "," and depth == 0:
            break              # 到同层逗号,参数结束
        else:
            out.append(ch)
        i += 1
    val = "".join(out).strip()
    return val or None


def _split_args(s: str) -> list[str]:
    out, depth, cur, in_str, q = [], 0, [], False, ""
    for ch in s:
        if in_str:
            cur.append(ch)
            if ch == q:
                in_str = False
            continue
        if ch in "\"'":
            in_str, q = True, ch
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return [x for x in out if x]


def _subst(expr: str, vars_: dict, seen: tuple = ()) -> str:
    """把用户变量名替换成它对应的表达式(递归展开)。"""
    def repl(m):
        name = m.group(0)
        if name in vars_ and name not in seen:
            return "(" + _subst(vars_[name], vars_, seen + (name,)) + ")"
        return name

    # 只替换独立标识符,避开函数名与已带括号的部分
    return re.sub(r"[A-Za-z_][A-Za-z0-9_]*", repl, expr)


def translate(src: str) -> dict:
    """把 Pine 源码翻成内部条件表达式。

    返回 {ok, buy, sell, vars, notes, error, lines}
    buy/sell 为 None 表示没找到对应进出场。
    """
    if not src or not src.strip():
        return {"ok": False, "error": "源码是空的", "buy": None, "sell": None,
                "vars": {}, "notes": [], "lines": 0}

    vars_: dict[str, str] = {}
    notes: list[str] = []
    buy_expr = None
    sell_expr = None
    pending_if = None          # 上一个 if 的条件,给下一行 entry 用
    raw_lines = src.splitlines()

    for no, raw in enumerate(raw_lines, 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        low = line.strip().lower()

        if low.startswith(IGNORE_PREFIX):
            continue
        for bad, what in UNSUPPORTED.items():
            if bad in low:
                return {"ok": False, "buy": None, "sell": None, "vars": vars_,
                        "notes": notes, "lines": len(raw_lines),
                        "error": f"第 {no} 行用到了不支持的写法:{what}(`{bad.strip()}`)"}

        s = line.strip()

        # if <cond>  → 记住条件,下一行 entry/close 用它
        m = re.match(r"^if\s+(.+?)\s*$", s, re.I)
        if m:
            try:
                pending_if = _subst(_rewrite_funcs(m.group(1)), vars_)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "buy": None, "sell": None, "vars": vars_,
                        "notes": notes, "lines": len(raw_lines),
                        "error": f"第 {no} 行 if 条件解析失败:{e}"}
            continue

        # strategy.entry(...) / strategy.close(...) / strategy.exit(...)
        if s.lower().startswith(("strategy.entry", "strategy.close", "strategy.exit")):
            cond = pending_if
            m_when = _find_kwarg(s, "when")
            if m_when:
                try:
                    cond = _subst(_rewrite_funcs(m_when), vars_)
                except Exception:  # noqa: BLE001
                    cond = pending_if
            if cond is None:
                notes.append(f"第 {no} 行的进出场没有条件(缺 if / when),已跳过")
                continue
            if s.lower().startswith("strategy.entry") and "strategy.long" in s.lower():
                buy_expr = cond
            elif s.lower().startswith("strategy.entry") and "strategy.short" in s.lower():
                sell_expr = cond
            elif s.lower().startswith(("strategy.close", "strategy.exit")):
                sell_expr = cond
            pending_if = None
            continue

        # 变量赋值 name = expr
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|=)\s*(.+)$", s)
        if m and not s.lower().startswith("plot"):
            name, expr = m.group(1), m.group(2)
            try:
                vars_[name] = _rewrite_funcs(expr)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "buy": None, "sell": None, "vars": vars_,
                        "notes": notes, "lines": len(raw_lines),
                        "error": f"第 {no} 行赋值解析失败:{e}"}
            pending_if = None
            continue

        notes.append(f"第 {no} 行没认出来,已跳过:{s[:40]}")
        pending_if = None

    # 常见省略写法:只写了一个交叉条件,没写 strategy.entry
    if buy_expr is None and not sell_expr and len(vars_) == 1:
        only = next(iter(vars_.values()))
        if "cross(" in only:
            buy_expr = _subst(only, vars_)
            notes.append("没找到 strategy.entry,用唯一的 cross 条件当买点")

    if buy_expr:
        buy_expr = _subst(buy_expr, vars_)
    if sell_expr:
        sell_expr = _subst(sell_expr, vars_)

    from . import rules as _rules
    for tag, expr in (("买点", buy_expr), ("卖点", sell_expr)):
        if not expr:
            continue
        ok, msg = _rules.validate(expr)
        if not ok:
            return {"ok": False, "buy": buy_expr, "sell": sell_expr, "vars": vars_,
                    "notes": notes, "lines": len(raw_lines),
                    "error": f"翻出来的{tag}表达式没通过内部校验:{msg}"}

    if not buy_expr and not sell_expr:
        return {"ok": False, "buy": None, "sell": None, "vars": vars_,
                "notes": notes, "lines": len(raw_lines),
                "error": "没找到可用的进出场条件(需要 strategy.entry / strategy.close)"}

    return {"ok": True, "buy": buy_expr, "sell": sell_expr, "vars": vars_,
            "notes": notes, "lines": len(raw_lines), "error": ""}


def to_rule(name: str, rep: dict, note: str = "") -> dict:
    """翻译结果 → 可以直接存进策略库的规则 dict(字段与 rules.save_rule 对齐)。"""
    if not rep.get("ok"):
        raise ValueError(rep.get("error") or "翻译失败")
    return {"name": name, "buy": rep["buy"] or "0",
            "sell": rep["sell"] or "0",
            "note": note or "从 Pine Script 翻译而来", "source": "pine"}
