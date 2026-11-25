# -*- coding: utf-8 -*-
"""量化表达式引擎 v2(内部代号 QL)。

设计取向,照着三条线定的:

1. 幻方式分层——研究侧只写意图,执行侧整条向量化。表达式编成算子树,
   叶子直接落在 numpy 数组上,不做逐 bar 的 Python 求值;语言层不再借道
   `eval`,安全性靠「没有 eval」而不是「过滤喂给 eval 的字符串」。
2. qlib 式算子代数——算子树带预热长度推断(lookback),横截面算子
   (rank/zscore/demean/winsorize)与纵截面算子(ts_*)分开命名。
3. WorldQuant BRAIN 式 Fast Expression——`:=` 变量复用、`? :` 三元、
   `holding` / `pnl_pct` 这类带状态的仓位算子,让「买什么」和「持有多久、
   亏多少撤」能在同一句里表达;同时保留通达信旧写法(列名简写、AND/OR/NOT、
   `名字:表达式`),导入的老公式不用改。

与 v1(旧 rules.py 内联求值)相比修掉的东西:
    · 除零不再抛 `'__import__'` 这种误导性错误
    · 窗口参数做类型/范围校验(ma(close,0) / ma(20,close) 给可读报错)
    · ref 负偏移明确拒绝,不再静默错位
    · 超长窗口不再静默全 True / 全 False
    · 链式比较 `a > b > c` 语义写死为区间并在文档里说明
    · 预热区自动屏蔽,避免前 20 根拿半条均线发假信号
    · 表达式编译一次、多票复用(不再每票重新分词)

对外只暴露四件事:`validate()` / `compile_expr()` / `evaluate()` / `required_bars()`。
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

try:
    from scipy.signal import lfilter as _lfilter
except Exception:                       # noqa: BLE001 —— 没装 scipy 就退回 pandas
    _lfilter = None

try:
    from numpy.lib.stride_tricks import sliding_window_view
except Exception:                       # noqa: BLE001
    sliding_window_view = None

# ------------------------------------------------------------------ 词法

_OPS2 = ("==", "!=", ">=", "<=", "<>", "&&", "||", "**", ":=")
_OPS1 = set("+-*/%^()<>=,;?:#")
_NAME_RE = re.compile(r"[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*")
_NUM_RE = re.compile(r"\d+\.?\d*(?:[eE][+-]?\d+)?")


class DslError(ValueError):
    """表达式错误。带位置、带原文、带一句能照着改的提示。"""

    def __init__(self, msg: str, expr: str = "", pos: int = -1, hint: str = ""):
        self.raw = msg
        self.expr = expr
        self.pos = pos
        self.hint = hint
        super().__init__(self._render())

    def with_expr(self, expr: str) -> "DslError":
        if not self.expr:
            self.expr = expr
            self.pos = -1
            self.args = (self._render(),)
        return self

    def _render(self) -> str:
        if not self.expr:
            return self.raw + (f"({self.hint})" if self.hint else "")
        body = self.expr.strip()
        text = f"{self.raw}"
        if 0 <= self.pos <= len(body):
            text += f"\n  {body}\n  {' ' * self.pos}^"
        else:
            text += f"\n  {body}"
        if self.hint:
            text += f"\n  提示:{self.hint}"
        return text


class _Tok:
    __slots__ = ("kind", "text", "pos")

    def __init__(self, kind: str, text: str, pos: int):
        self.kind, self.text, self.pos = kind, text, pos

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"{self.kind}:{self.text}"


def _lex(src: str) -> list[_Tok]:
    """分词。括号外的换行等价于 `;`,方便多行写一条公式。"""
    out: list[_Tok] = []
    i, n, depth = 0, len(src), 0
    while i < n:
        ch = src[i]
        if ch in " \t\r":
            i += 1
            continue
        if ch == "\n":
            if depth == 0:
                out.append(_Tok("op", ";", i))
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":        # 行注释
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "{":                                           # 块注释
            j = src.find("}", i)
            i = n if j < 0 else j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUM_RE.match(src, i)
            out.append(_Tok("num", m.group(0), i))
            i = m.end()
            continue
        m = _NAME_RE.match(src, i)
        if m:
            word = m.group(0)
            if word.lower() in ("and", "or", "not"):
                out.append(_Tok("op", word.lower(), i))
            else:
                out.append(_Tok("name", word, i))
            i = m.end()
            continue
        two = src[i:i + 2]
        if two in _OPS2:
            out.append(_Tok("op", "!=" if two == "<>" else two, i))
            i += 2
            continue
        if ch in _OPS1:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            out.append(_Tok("op", ch, i))
            i += 1
            continue
        raise DslError(f"不认识的字符 {ch!r}", src, i,
                       "列名/函数名只能用字母、数字、下划线或汉字")
    out.append(_Tok("eof", "", n))
    return out


# ------------------------------------------------------------------ 语法树

class Node:
    """算子树节点。pos 只用来在报错时指位置,不参与求值。"""

    pos = -1

    def eval(self, ctx) -> np.ndarray:
        raise NotImplementedError

    def children(self) -> tuple:
        return ()

    def names(self, acc: set) -> None:
        for c in self.children():
            c.names(acc)

    def subst(self, env: dict) -> "Node":
        """把 env 里的名字换成对应节点(宏展开用)。默认叶子原样返回。"""
        return self

    def walk(self, fn) -> None:
        fn(self)
        for c in self.children():
            c.walk(fn)

    def lookback(self, prog, seen: frozenset = frozenset()) -> int:
        lb = 0
        for c in self.children():
            lb = max(lb, c.lookback(prog, seen))
        return lb


class Const(Node):
    __slots__ = ("value",)

    def __init__(self, value: float):
        self.value = float(value)

    def eval(self, ctx):
        return np.full(ctx.n, self.value, dtype="float64")


class Param(Node):
    """参数占位:#n。编译时没绑定就报错,绑定了直接变 Const。

    这是给「一套公式跑一片参数」准备的——研究页的批量枚举不用再靠
    str.format 拼字符串(拼错了要到运行期才发现)。
    """

    __slots__ = ("index", "name")

    def __init__(self, index: int, name: str = ""):
        self.index = index
        self.name = name

    def eval(self, ctx):
        raise DslError(f"参数 #{self.index} 没有绑定值", ctx.source, -1,
                       "调用时传 params=[...],或把 #n 换成具体数字")

    def subst(self, env: dict) -> "Node":
        return env.get(("#", self.index), self)

    def lookback(self, prog, seen: frozenset = frozenset()) -> int:
        return 0


class Series(Node):
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def eval(self, ctx):
        return ctx.series(self.name)

    def names(self, acc: set) -> None:
        acc.add(self.name)


class Var(Node):
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def eval(self, ctx):
        return ctx.var(self.name)

    def names(self, acc: set) -> None:
        acc.add(self.name)

    def lookback(self, prog, seen: frozenset = frozenset()) -> int:
        if self.name in seen:
            raise DslError(f"变量 {self.name} 循环引用", prog.source)
        return prog.vars[self.name].lookback(prog, seen | {self.name})

    def subst(self, env: dict) -> "Node":
        """自定义函数体内的形参替换:Var('n') → 调用处的实参节点。"""
        return env.get(self.name, self)


class Unary(Node):
    __slots__ = ("op", "x")

    def __init__(self, op: str, x: Node):
        self.op, self.x = op, x

    def children(self):
        return (self.x,)

    def subst(self, env: dict) -> "Node":
        return Unary(self.op, self.x.subst(env))

    def eval(self, ctx):
        v = ctx.val(self.x)
        if self.op == "-":
            return -v
        if self.op == "+":
            return v
        if self.op == "not":
            return (~_bool(v)).astype("float64")
        raise DslError(f"不支持的运算符 {self.op}", ctx.source, self.pos)


class Bin(Node):
    __slots__ = ("op", "a", "b")

    def __init__(self, op: str, a: Node, b: Node):
        self.op, self.a, self.b = op, a, b

    def children(self):
        return (self.a, self.b)

    def eval(self, ctx):
        a, b = ctx.val(self.a), ctx.val(self.b)
        op = self.op
        if op in ("and", "&&"):
            return (_bool(a) & _bool(b)).astype("float64")
        if op in ("or", "||"):
            return (_bool(a) | _bool(b)).astype("float64")
        if op in _CMP:
            return _cmp(op, a, b).astype("float64")
        return _arith(op, a, b, ctx, self.pos)

    def subst(self, env: dict) -> "Node":
        return Bin(self.op, self.a.subst(env), self.b.subst(env))


class ChainCmp(Node):
    """链式比较 a > b > c,语义是 (a>b) and (b>c),即「区间」。"""

    __slots__ = ("ops", "nodes")

    def __init__(self, ops: list[str], nodes: list[Node]):
        self.ops, self.nodes = ops, nodes

    def children(self):
        return tuple(self.nodes)

    def eval(self, ctx):
        acc = None
        prev = ctx.val(self.nodes[0])
        for op, node in zip(self.ops, self.nodes[1:]):
            cur = ctx.val(node)
            m = _cmp(op, prev, cur)
            acc = m if acc is None else (acc & m)
            prev = cur
        return acc.astype("float64")

    def subst(self, env: dict) -> "Node":
        return ChainCmp(self.ops, [n.subst(env) for n in self.nodes])


class Ternary(Node):
    __slots__ = ("cond", "a", "b")

    def __init__(self, cond: Node, a: Node, b: Node):
        self.cond, self.a, self.b = cond, a, b

    def children(self):
        return (self.cond, self.a, self.b)

    def eval(self, ctx):
        m = _bool(ctx.val(self.cond))
        return np.where(m, ctx.val(self.a), ctx.val(self.b)).astype("float64")

    def subst(self, env: dict) -> "Node":
        return Ternary(self.cond.subst(env), self.a.subst(env), self.b.subst(env))


class Call(Node):
    __slots__ = ("name", "args")

    def __init__(self, name: str, args: list[Node]):
        self.name, self.args = name, args

    def children(self):
        return tuple(self.args)

    def eval(self, ctx):
        return ctx.call(self.name, self.args, self.pos)

    def subst(self, env: dict) -> "Node":
        """自定义函数体展开:把形参替换成实参,再把整个调用换成展开后的节点。

        只有 FUNC_SPECS 里的内建函数才会走到这里(自定义函数在解析阶段就换掉了),
        所以调用点看到的永远是内建函数。
        """
        return Call(self.name, [a.subst(env) for a in self.args])

    def lookback(self, prog, seen: frozenset = frozenset()) -> int:
        base = max((a.lookback(prog, seen) for a in self.args), default=0)
        spec = FUNC_SPECS.get(self.name)
        if spec is None:
            return base
        # 先看窗口参数(window_at 指定的那个位置)
        pos = spec["window_at"]
        win_from_arg = None
        if pos is not None:
            idx = pos if pos >= 0 else len(self.args) + pos
            if 0 <= idx < len(self.args):
                win_from_arg = _static_int(self.args[idx])
        if win_from_arg is not None:
            base = max(base, int(spec["lb_mult"] * max(win_from_arg, 1)))
        # 再看这个函数有没有「按参数推预热」的钩子。
        # kdj/boll/macd 这类窗口参数不在 window_at 位置(或者有多个)的函数
        # 靠它算出真实预热;返回 None 表示算不出来,退回 fixed_lb。
        calc = spec.get("lb_of")
        if calc is not None:
            try:
                got = calc(self.args)
            except Exception:  # noqa: BLE001 —— 推不出来不算错,退回保守值
                got = None
            if got is not None:
                return max(base, int(got))
        if spec["fixed_lb"]:
            base = max(base, spec["fixed_lb"])
        if pos is None:
            return base
        if win_from_arg is None:
            return max(base, 60)     # 窗口不是常数时按 60 根保守估计
        return base


def _static_int(node: Node) -> int | None:
    v = _const_value(node)
    if v is not None and float(v).is_integer():
        return int(v)
    return None


def _const_value(node: Node) -> float | None:
    """只有能静态算出数值的节点才返回数:`-5` 是 Unary(Const) 也要认出来。"""
    if isinstance(node, Const):
        return node.value
    if isinstance(node, Unary) and node.op in ("-", "+"):
        inner = _const_value(node.x)
        if inner is not None:
            return -inner if node.op == "-" else inner
    return None


_CMP = {"==", "!=", ">", ">=", "<", "<="}


def _bool(v) -> np.ndarray:
    a = np.asarray(v)
    if a.dtype == bool:
        return a
    return np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=-1.0) != 0


def _num(v) -> np.ndarray:
    a = np.asarray(v)
    return a.astype("float64") if a.dtype != "float64" else a


def _scalar(v) -> float:
    a = np.asarray(v, dtype="float64").reshape(-1)
    if a.size == 0:
        return float("nan")
    return float(a[0])


def _cmp(op: str, a, b) -> np.ndarray:
    """NaN 参与比较一律 False(和 pandas 对齐,不做三值逻辑)。"""
    a, b = _num(a), _num(b)
    with np.errstate(invalid="ignore"):
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "<":
            return a < b
        return a <= b


def _arith(op: str, a, b, ctx, pos: int = -1) -> np.ndarray:
    a, b = _num(a), _num(b)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return np.divide(a, b)
        if op == "%":
            return np.mod(a, b)
        if op in ("^", "**"):
            return np.power(a, b)
    raise DslError(f"不支持的运算符 {op}", getattr(ctx, "source", ""), pos)


# ------------------------------------------------------------------ 优化
#
# 求值器是「解释」:一个节点一个节点地算。真编译器会先化简、再去重。
# 这里做两件便宜但收益明确的事:
#   1) 常量折叠 —— 2*3、1>0、-(5) 这种,编译期就变成常数
#   2) 公共子式消除(hash-consing) —— 结构完全相同的子树合并成同一个对象,
#      求值器的 id(node) 记忆化就会自然命中,ma(close,20) 写两遍只算一次
#
# 不改语义:折叠只碰「全常数的节点」和 x*1 / x/1 / x+0 / x-0 这几条恒等式
# (x*0 不能折,因为 NaN*0 仍是 NaN)。


def _struct_key(node: Node) -> tuple:
    """结构性哈希。结构相同的两棵子树得到相等的 key。"""
    if isinstance(node, Const):
        return ("c", node.value)
    if isinstance(node, Param):
        return ("p", node.index)
    if isinstance(node, Series):
        return ("s", node.name)
    if isinstance(node, Var):
        return ("v", node.name)
    if isinstance(node, Unary):
        return ("u", node.op, _struct_key(node.x))
    if isinstance(node, Bin):
        return ("b", node.op, _struct_key(node.a), _struct_key(node.b))
    if isinstance(node, ChainCmp):
        return ("cc", tuple(node.ops), tuple(_struct_key(n) for n in node.nodes))
    if isinstance(node, Ternary):
        return ("t", _struct_key(node.cond), _struct_key(node.a), _struct_key(node.b))
    if isinstance(node, Call):
        return ("f", node.name, tuple(_struct_key(a) for a in node.args))
    return ("?", id(node))


def _is_const(node: Node) -> bool:
    return isinstance(node, Const)


def _try_fold(node: Node) -> Node:
    """能算成常数的就折成常数;能化简的恒等式顺手化简。"""
    if isinstance(node, Unary):
        x = node.x
        if _is_const(x):
            if node.op == "-":
                return Const(-x.value)
            if node.op == "+":
                return x
            if node.op == "not":
                return Const(0.0 if x.value else 1.0)
        return node

    if isinstance(node, Bin):
        a, b, op = node.a, node.b, node.op
        if _is_const(a) and _is_const(b):
            try:
                if op in ("and", "&&"):
                    return Const(1.0 if (a.value and b.value) else 0.0)
                if op in ("or", "||"):
                    return Const(1.0 if (a.value or b.value) else 0.0)
                if op in _CMP:
                    return Const(1.0 if _cmp(op, [a.value], [b.value])[0] else 0.0)
                return Const(float(_scalar(_arith(op, [a.value], [b.value], None))))
            except DslError:
                return node
        # 恒等式:x*1 / x/1 / x+0 / x-0 都是 x 本身
        if _is_const(b):
            if op in ("*", "/") and b.value == 1.0:
                return a
            if op in ("+", "-") and b.value == 0.0:
                return a
        if _is_const(a) and op == "+" and a.value == 0.0:
            return b
        if _is_const(a) and op == "*" and a.value == 1.0:
            return b
        return node

    if isinstance(node, ChainCmp):
        if all(_is_const(n) for n in node.nodes):
            prev = node.nodes[0].value
            acc = True
            for op, n in zip(node.ops, node.nodes[1:]):
                acc = acc and bool(_cmp(op, [prev], [n.value])[0])
                prev = n.value
            return Const(1.0 if acc else 0.0)
        return node

    if isinstance(node, Ternary):
        if _is_const(node.cond) and _is_const(node.a) and _is_const(node.b):
            return node.a if node.cond.value else node.b
        return node

    return node


def _simplify(node: Node, tab: dict | None = None) -> Node:
    """一趟:先化简子节点,再折叠,再对折叠后的结构做去重(共享同一对象)。"""
    if tab is None:
        tab = {}
    if isinstance(node, (Const, Param, Series, Var)):
        new = node
    elif isinstance(node, Unary):
        new = Unary(node.op, _simplify(node.x, tab))
    elif isinstance(node, Bin):
        new = Bin(node.op, _simplify(node.a, tab), _simplify(node.b, tab))
    elif isinstance(node, ChainCmp):
        new = ChainCmp(list(node.ops), [_simplify(n, tab) for n in node.nodes])
    elif isinstance(node, Ternary):
        new = Ternary(_simplify(node.cond, tab), _simplify(node.a, tab),
                      _simplify(node.b, tab))
    elif isinstance(node, Call):
        new = Call(node.name, [_simplify(a, tab) for a in node.args])
    else:
        return node

    folded = _try_fold(new)
    key = _struct_key(folded)
    old = tab.get(key)
    if old is not None:
        return old
    tab[key] = folded
    return folded


def _count_nodes(node: Node, seen: set | None = None) -> int:
    """树里的节点数(共享的子树只算一次)。"""
    if seen is None:
        seen = set()
    if id(node) in seen:
        return 0
    seen.add(id(node))
    return 1 + sum(_count_nodes(c, seen) for c in node.children())


# ------------------------------------------------------------------ 解析

class _Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = _lex(src)
        self.i = 0
        self.vars: dict[str, Node] = {}
        self.funcs: dict[str, tuple[list[str], Node, int]] = {}
        self.defining: str | None = None
        self.def_params: list[str] = []
        self._just_defined = False

    def peek(self, k: int = 0) -> _Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def accept(self, *texts: str) -> _Tok | None:
        t = self.peek()
        if t.kind == "op" and t.text in texts:
            return self.next()
        return None

    def expect(self, text: str, why: str = "") -> _Tok:
        t = self.peek()
        if t.kind == "op" and t.text == text:
            return self.next()
        raise DslError(f"这里应该是 {text!r},实际是 {t.text!r}", self.src, t.pos, why)

    def at_end(self) -> bool:
        return self.peek().kind == "eof"

    # -- 语句
    def program(self):
        stmts: list[tuple[str, Node]] = []
        result: Node | None = None
        label = ""
        while not self.at_end():
            if self.accept(";"):
                continue
            t, nxt = self.peek(), self.peek(1)
            if t.kind == "name" and nxt.kind == "op" and nxt.text in (":=", "="):
                self.next(), self.next()
                if self.defining:
                    raise DslError(
                        f"函数 {self.defining} 里不能写赋值语句",
                        self.src, t.pos, "函数体只能是一句直接返回的表达式")
                if t.text in self.vars:
                    raise DslError(f"变量 {t.text} 重复定义", self.src, t.pos)
                node = self.expr()
                self.vars[t.text] = node
                stmts.append((t.text, node))
            elif t.kind == "name" and nxt.kind == "op" and nxt.text == ":":
                self.next(), self.next()
                label = t.text
                result = self.expr()
            else:
                node = self.expr()
                if not self._just_defined:      # fn 定义句不产生输出
                    result = node
                self._just_defined = False
            if not self.at_end() and not (self.peek().kind == "op"
                                          and self.peek().text == ";"):
                bad = self.peek()
                raise DslError(f"多余的内容 {bad.text!r}", self.src, bad.pos,
                               "表达式之间用 ; 或换行分隔")
            self.accept(";")
        if result is None:
            raise DslError("没有输出表达式", self.src, 0,
                           "最后一行写要判断的条件,例如:close > ma(20)")
        return stmts, result, label

    # -- 自定义函数  fn 名字(a, b) := 表达式   (体在一条语句里,不跨行)
    def func_def(self, name: str, pos: int) -> None:
        if name.lower() in FUNC_SPECS:
            raise DslError(f"{name} 是内置函数,不能重定义", self.src, pos)
        if name in self.funcs:
            raise DslError(f"函数 {name} 重复定义", self.src, pos)
        params: list[str] = []
        if not self.accept(")"):
            while True:
                pt = self.next()
                if pt.kind != "name":
                    raise DslError(f"参数名必须是标识符,实际是 {pt.text!r}",
                                   self.src, pt.pos)
                if pt.text in params:
                    raise DslError(f"参数 {pt.text} 重复", self.src, pt.pos)
                params.append(pt.text)
                if self.accept(","):
                    continue
                self.expect(")", f"fn {name}(...) 的参数表没闭合")
                break
        self.expect("=", "函数定义写成:fn 名字(a,b) = 表达式")
        self.defining, self.def_params = name, list(params)
        try:
            body = self.expr()
        finally:
            self.defining, self.def_params = None, []
        self.funcs[name] = (params, body, pos)

    # -- 表达式层级
    def expr(self) -> Node:
        cond = self.or_expr()
        if self.accept("?"):
            a = self.expr()
            self.expect(":", "三元写法:条件 ? 成立时取值 : 否则取值")
            b = self.expr()
            return Ternary(cond, a, b)
        return cond

    def or_expr(self) -> Node:
        node = self.and_expr()
        while self.peek().kind == "op" and self.peek().text in ("or", "||"):
            self.next()
            node = Bin("or", node, self.and_expr())
        return node

    def and_expr(self) -> Node:
        node = self.not_expr()
        while self.peek().kind == "op" and self.peek().text in ("and", "&&"):
            self.next()
            node = Bin("and", node, self.not_expr())
        return node

    def not_expr(self) -> Node:
        if self.peek().kind == "op" and self.peek().text == "not":
            pos = self.next().pos
            n = Unary("not", self.not_expr())
            n.pos = pos
            return n
        return self.cmp_expr()

    def cmp_expr(self) -> Node:
        left = self.add_expr()
        ops, nodes = [], [left]
        while self.peek().kind == "op" and self.peek().text in _CMP:
            ops.append(self.next().text)
            nodes.append(self.add_expr())
        if not ops:
            return left
        if len(ops) == 1:
            return Bin(ops[0], nodes[0], nodes[1])
        return ChainCmp(ops, nodes)

    def add_expr(self) -> Node:
        node = self.mul_expr()
        while self.peek().kind == "op" and self.peek().text in ("+", "-"):
            node = Bin(self.next().text, node, self.mul_expr())
        return node

    def mul_expr(self) -> Node:
        node = self.unary()
        while self.peek().kind == "op" and self.peek().text in ("*", "/", "%"):
            node = Bin(self.next().text, node, self.unary())
        return node

    def unary(self) -> Node:
        t = self.peek()
        if t.kind == "op" and t.text in ("-", "+"):
            self.next()
            n = Unary(t.text, self.unary())
            n.pos = t.pos
            return n
        return self.power()

    def power(self) -> Node:
        base = self.primary()
        while self.peek().kind == "op" and self.peek().text in ("^", "**"):
            self.next()
            base = Bin("^", base, self.unary())
        return base

    def primary(self) -> Node:
        t = self.peek()
        if t.kind == "num":
            self.next()
            return Const(float(t.text))
        if t.kind == "op" and t.text == "#":
            # 参数占位:#1 #2 ... 或 #(表达式) 形式的序号
            self.next()
            nt = self.next()
            if nt.kind != "num":
                raise DslError(f"# 后面要跟参数序号,实际是 {nt.text!r}",
                               self.src, nt.pos, "写法:#1 / #2 …")
            idx = int(float(nt.text))
            if idx < 1:
                raise DslError("参数序号从 #1 开始", self.src, t.pos)
            return Param(idx)
        if t.kind == "name":
            self.next()
            # fn 名字(a,b) = ...  只有真正跟着 ( 和 = 才算定义
            if t.text.lower() == "fn" and self._looks_like_fn():
                return self._parse_fn()
            if self.accept("("):
                args: list[Node] = []
                if not self.accept(")"):
                    while True:
                        args.append(self.expr())
                        if self.accept(","):
                            continue
                        self.expect(")", f"{t.text}(...) 的括号没闭合")
                        break
                name = t.text.lower()
                if name in FUNC_SPECS:
                    return Call(name, args)
                if t.text in self.funcs or name in self.funcs:
                    return self._expand_user_func(t.text, args, t.pos)
                raise DslError(f"没有这个函数:{t.text}", self.src, t.pos,
                               _suggest_func(t.text))
            low = t.text.lower()
            if self.defining and t.text in self.def_params:
                return Var(t.text)          # 函数体里的形参,留给 subst 替换
            if low in ALIASES:
                return Series(ALIASES[low])
            if low in COLUMNS:
                return Series(low)
            if t.text in self.vars:
                return Var(t.text)
            if low in ("true", "false"):
                return Const(1.0 if low == "true" else 0.0)
            spec = FUNC_SPECS.get(low)
            if spec is not None:
                if spec["arity"][0] == 0:
                    return Call(low, [])    # 无参函数可以不写括号:macd_dif > 0
                raise DslError(f"{t.text} 是函数,后面要跟括号和参数",
                               self.src, t.pos, spec["doc"])
            if t.text in self.funcs and not self.funcs[t.text][0]:
                return self._expand_user_func(t.text, [], t.pos)
            if self.defining and self.peek().kind == "op" and \
                    self.peek().text in (":=", "="):
                raise DslError(
                    f"函数 {self.defining} 里不能写赋值语句",
                    self.src, t.pos, "函数体只能是一句直接返回的表达式")
            raise DslError(f"不认识的列名或变量:{t.text}", self.src, t.pos,
                           _suggest_name(t.text, list(COLUMNS) + list(self.vars)))
        if t.kind == "op" and t.text == "(":
            self.next()
            node = self.expr()
            self.expect(")", "括号没闭合")
            return node
        raise DslError(f"这里缺少表达式,实际是 {t.text!r}", self.src, t.pos,
                       "检查是否有连续运算符或空括号")

    def _looks_like_fn(self) -> bool:
        """当前在 `fn` 后面,往后扫一层括号,紧跟着 = 就是函数定义。"""
        depth = 0
        for k in range(1, 200):
            tk = self.peek(k)
            if tk.kind == "eof":
                return False
            if tk.kind == "op" and tk.text == "(":
                depth += 1
            elif tk.kind == "op" and tk.text == ")":
                depth -= 1
                if depth == 0:
                    nk = self.peek(k + 1)
                    return nk.kind == "op" and nk.text in ("=", ":=")
        return False

    def _parse_fn(self) -> Node:
        t = self.peek()
        if t.kind != "name":
            raise DslError("fn 后面要跟函数名", self.src, t.pos)
        self.next()
        self.expect("(", "函数定义写成:fn 名字(a, b) = 表达式")
        self.func_def(t.text, t.pos)
        self._just_defined = True
        return Const(0.0)       # 定义本身没有输出,占个位

    def _expand_user_func(self, name: str, args: list[Node], pos: int) -> Node:
        key = name if name in self.funcs else name.lower()
        params, body, def_pos = self.funcs[key]
        if len(args) != len(params):
            raise DslError(f"自定义函数 {key} 需要 {len(params)} 个参数,"
                           f"你给了 {len(args)} 个", self.src, pos)
        env = dict(zip(params, args))
        # 函数体里如果还引用了别的形参名以外的变量,按调用点的定义展开
        out = body.subst(env)
        return out.subst({k: v for k, v in self.vars.items() if k not in env})


# ------------------------------------------------------------------ 函数库

COLUMNS = ("close", "open", "high", "low", "volume", "amount",
           "turnover", "float_cap", "market_cap")

ALIASES = {"c": "close", "o": "open", "h": "high", "l": "low",
           "v": "volume", "vol": "volume", "amo": "amount"}

FUNC_SPECS: dict[str, dict] = {}
_XS_FUNCS: set[str] = set()


def _register(name: str, arity: tuple[int, int | None], impl, doc: str, *,
              window_at: int | None = None, lb_mult: float = 1.0,
              fixed_lb: int = 0, xs: bool = False, group: str = "其他",
              raw_args: bool = False, lb_of=None,
              cols: tuple[str, ...] = ()) -> None:
    """注册一个内建函数。

    raw_args=True 表示 impl 自己负责对实参求值(拿到的是语法树节点),
    用于「同一个位置可能是窗口也可能是序列」这类要按节点类型分派的函数。

    lb_of 是一个 `(args) -> int | None` 的回调,给「窗口不在 window_at 位置」
    或者「预热由好几个参数共同决定」的函数用来算真实预热长度。

    cols 是这个函数**隐式**读取的行情列。像 ma(20)、macd_hist、rsi(14)
    这类只给窗口不给序列的写法,语法树上根本没有 Series 节点,
    names() 收集不到列,界面上就会显示「用到列 (无)」——加了这个字段才能正确上报。
    """
    FUNC_SPECS[name] = {"arity": arity, "impl": impl, "doc": doc,
                        "window_at": window_at, "lb_mult": lb_mult,
                        "fixed_lb": fixed_lb, "xs": xs, "group": group,
                        "raw_args": raw_args, "lb_of": lb_of,
                        "cols": tuple(cols)}
    if xs:
        _XS_FUNCS.add(name)


def _series(x) -> pd.Series:
    return pd.Series(np.asarray(x, dtype="float64"))


# ---- 数值内核:pandas 的 rolling/ewm 每调一次都要新建 Series 走一遍
# 分派逻辑,500 根上要 30~45µs。下面这几个用 numpy/scipy 直接算,
# 数值和 pandas 一致(实测最大差 1e-13 量级),快 4~5 倍。
# 这不是「换成 C++」,是让热点落在 numpy/scipy 的 C 循环里——
# 幻方把瓶颈下沉到 C++/CUDA,单机 Python 项目能下沉的那一层就是这里。


def _roll_mean(x, n: int) -> np.ndarray:
    """min_periods=1 的滑动均值(前缀和,O(n))。"""
    v = _num(x)
    m = len(v)
    if m == 0:
        return v
    cs = np.concatenate(([0.0], np.cumsum(v)))
    idx = np.arange(1, m + 1)
    lo = np.maximum(idx - n, 0)
    return (cs[idx] - cs[lo]) / (idx - lo)


def _roll_sum(x, n: int) -> np.ndarray:
    v = _num(x)
    m = len(v)
    if m == 0:
        return v
    cs = np.concatenate(([0.0], np.cumsum(v)))
    idx = np.arange(1, m + 1)
    lo = np.maximum(idx - n, 0)
    return cs[idx] - cs[lo]


def _roll_ext(x, n: int, how: str) -> np.ndarray:
    """滑动极值:用 numpy 滑动窗口视图,失败时退回 pandas。"""
    v = _num(x)
    m = len(v)
    if m == 0:
        return v
    if sliding_window_view is None or n > 512:   # 窗口太大时窗口视图吃内存
        return np.asarray(getattr(_series(v).rolling(n, min_periods=1), how)(),
                          dtype="float64")
    nn = min(n, m)
    fill = -np.inf if how == "max" else np.inf
    ext = np.concatenate((np.full(nn - 1, fill), v))
    win = sliding_window_view(ext, nn)
    out = win.max(axis=1) if how == "max" else win.min(axis=1)
    # 前 nn-1 根只用真实数据,重算一遍
    for i in range(nn - 1):
        out[i] = v[:i + 1].max() if how == "max" else v[:i + 1].min()
    return out


def _ewm(x, alpha: float) -> np.ndarray:
    """pandas ewm(alpha, adjust=False).mean() 的等价实现。

    递推 y[t] = (1-a)·y[t-1] + a·v[t] 是一阶 IIR,
    用 scipy.signal.lfilter 一个 C 循环算完。
    初始状态按 pandas 的规矩:**第一个有效观测值直接当 y 的初值**;
    NaN 处输出 NaN 且不推进状态。
    """
    v = _num(x)
    if v.size == 0 or not np.isfinite(alpha) or alpha <= 0:
        return v
    if _lfilter is None:               # 没 scipy 就用 pandas
        return np.asarray(_series(v).ewm(alpha=alpha, adjust=False).mean(),
                          dtype="float64")
    ok = ~np.isnan(v)
    if not ok.any():
        return np.full(v.size, np.nan)
    k = int(np.flatnonzero(ok)[0])      # 第一个有效位置
    out = np.full(v.size, np.nan)
    seg = np.nan_to_num(v[k:], nan=0.0)
    zi = [(1.0 - alpha) * v[k]]         # 初值 = 第一个有效观测
    res, _ = _lfilter([alpha], [1.0, -(1.0 - alpha)], seg, zi=zi)
    out[k:] = res
    tail = np.isnan(v[k:])              # 中间再有 NaN 的,还回 NaN
    if tail.any():
        out[k:] = np.where(tail, np.nan, out[k:])
    return out


def _win(v, fname: str, ctx=None, pos: int = -1) -> int:
    """窗口参数:必须是常数整数、≥1、≤5000。错了一个字说清楚。"""
    a = np.asarray(v, dtype="float64")
    if a.size > 1 and not np.allclose(a, a.reshape(-1)[0], equal_nan=True):
        raise DslError(f"{fname} 的窗口参数必须是常数,你传了一条会变的序列",
                       getattr(ctx, "source", ""), pos,
                       f"窗口写成数字,例如 {fname}(close, 20)")
    val = _scalar(a) if a.size else float("nan")
    if not math.isfinite(val) or not float(val).is_integer():
        raise DslError(f"{fname} 的窗口必须是整数,收到 {val:g}",
                       getattr(ctx, "source", ""), pos)
    n = int(val)
    if n < 1:
        raise DslError(f"{fname} 的窗口必须 ≥1,收到 {n}",
                       getattr(ctx, "source", ""), pos,
                       "1 根 K 线没有统计意义,一般至少 3")
    if n > 5000:
        raise DslError(f"{fname} 的窗口 {n} 太大,超过 5000 根就失去意义",
                       getattr(ctx, "source", ""), pos)
    return n


def _lag(v, fname: str, ctx=None) -> int:
    a = np.asarray(v, dtype="float64")
    val = _scalar(a) if a.size else 0.0
    if not math.isfinite(val) or not float(val).is_integer():
        raise DslError(f"{fname} 的位移必须是整数,收到 {val:g}",
                       getattr(ctx, "source", ""))
    n = int(val)
    if n < 0:
        raise DslError(f"{fname} 不能往前看:位移必须 ≥0,收到 {n}",
                       getattr(ctx, "source", ""), -1,
                       "所有函数只看历史;想引用未来是没有语义的")
    if n > 5000:
        raise DslError(f"{fname} 的位移 {n} 太大(>5000)", getattr(ctx, "source", ""))
    return n


def _roll(x, n: int, how: str) -> np.ndarray:
    """min_periods=1 的滑动统计。mean/sum 用前缀和,std/median 等退回 pandas。"""
    if how == "mean":
        return _roll_mean(x, n)
    if how == "sum":
        return _roll_sum(x, n)
    if how in ("max", "min"):
        return _roll_ext(x, n, how)
    r = _series(x).rolling(n, min_periods=1)
    return np.asarray(getattr(r, how)(), dtype="float64")


def _shift(x, n: int) -> np.ndarray:
    """向后位移 n 根(前 n 根为 NaN)。numpy 里就是切片。"""
    v = _num(x)
    if n <= 0:
        return v.copy()
    if n >= len(v):
        return np.full(len(v), np.nan)
    out = np.full(len(v), np.nan)
    out[n:] = v[:-n]
    return out


def _cross_up(a, b) -> np.ndarray:
    x, y = _num(a), _num(b)
    x, y = np.broadcast_arrays(x, y)
    prev = np.r_[False, (x[:-1] <= y[:-1])] if len(x) else np.zeros(0, bool)
    return (x > y) & prev


def _cross_dn(a, b) -> np.ndarray:
    x, y = _num(a), _num(b)
    x, y = np.broadcast_arrays(x, y)
    prev = np.r_[False, (x[:-1] >= y[:-1])] if len(x) else np.zeros(0, bool)
    return (x < y) & prev


def _filter(cond, n: int) -> np.ndarray:
    """条件成立后 n 根内不再重复发信号(通达信 FILTER)。"""
    a = _bool(cond)
    out = np.zeros(len(a), bool)
    last = -10 ** 9
    for i, flag in enumerate(a):
        if flag and i - last >= n:
            out[i] = True
            last = i
    return out


def _bars_since(cond) -> np.ndarray:
    a = _bool(cond)
    out = np.full(len(a), np.nan)
    last = -1
    for i, flag in enumerate(a):
        if flag:
            last = i
        if last >= 0:
            out[i] = i - last
    return out


def _state(entry, exit_, close, mode: str) -> np.ndarray:
    """仓位状态机。同根既开又平 → 以平为准(保守)。

    mode: hold 持仓标记 / days 持仓根数 / price 建仓价 / pnl 浮动盈亏%
    """
    e, x = _bool(entry), _bool(exit_)
    n = max(len(e), len(x))
    e, x = np.resize(e, n), np.resize(x, n)
    c = np.asarray(close, dtype="float64")
    if len(c) < n:
        c = np.resize(c, n)
    out = np.zeros(n)
    held, entry_i = False, -1
    for i in range(n):
        if held and x[i]:
            held, entry_i = False, -1
        if not held and e[i] and not x[i]:
            held, entry_i = True, i
        if not held:
            continue
        if mode == "hold":
            out[i] = 1.0
        elif mode == "days":
            out[i] = float(i - entry_i + 1)
        elif mode == "price":
            out[i] = c[entry_i]
        else:
            base = c[entry_i]
            out[i] = (c[i] / base - 1.0) * 100.0 if base else np.nan
    return out


def _trade_when(trigger, value, exit_) -> np.ndarray:
    """WorldQuant 的 trade_when:触发时改仓位值,出场条件出现归零。"""
    t, x, v = _bool(trigger), _bool(exit_), _num(value)
    n = max(len(t), len(x), len(v))
    t, x, v = np.resize(t, n), np.resize(x, n), np.resize(v, n)
    out = np.zeros(n)
    cur = 0.0
    for i in range(n):
        if x[i]:
            cur = 0.0
        elif t[i]:
            cur = v[i]
        out[i] = cur
    return out


# ---- 横截面 -------------------------------------------------------

_PANEL_CACHE: dict[tuple, pd.DataFrame] = {}
_PANEL_MAX = 24


def _xs_apply(ctx, node: Node, how: str, p: float | None = None):
    if ctx.xs is None:
        raise DslError("横截面函数需要多只股票一起算,单票试跑用不了",
                       ctx.source, -1,
                       "回测、选股、自动交易里能跑;试跑请先换掉 rank/zscore")
    if ctx.code is None:
        raise DslError("横截面函数需要知道当前是哪只票", ctx.source)
    key = (ctx.source, id(node), id(ctx.xs))
    panel = _PANEL_CACHE.get(key)
    if panel is None:
        cols = {}
        for code, df in ctx.xs.items():
            try:
                sub = _Ctx(df, code, ctx.source, _ProgStub(ctx.vars))
                arr = sub.val(node)
            except DslError:
                continue
            idx = (pd.DatetimeIndex(df["datetime"]) if "datetime" in df.columns
                   else pd.RangeIndex(len(df)))
            cols[code] = pd.Series(np.asarray(arr, dtype="float64"), index=idx)
        if not cols:
            raise DslError("横截面函数没有可用的股票数据", ctx.source)
        mine = ctx.df
        idx = (pd.DatetimeIndex(mine["datetime"]) if "datetime" in mine.columns
               else pd.RangeIndex(len(mine)))
        panel = pd.DataFrame(cols).reindex(idx)
        if len(_PANEL_CACHE) > _PANEL_MAX:
            _PANEL_CACHE.clear()
        _PANEL_CACHE[key] = panel
    if how == "rank":
        out = panel.rank(axis=1, pct=True)
    elif how == "zscore":
        sd = panel.std(axis=1, ddof=0).replace(0, np.nan)
        out = panel.sub(panel.mean(axis=1), axis=0).div(sd, axis=0)
    elif how == "demean":
        out = panel.sub(panel.mean(axis=1), axis=0)
    elif how == "winsorize":
        q = float(np.clip(0.01 if p is None else p, 0.001, 0.5))
        out = panel.clip(lower=panel.quantile(q, axis=1),
                         upper=panel.quantile(1 - q, axis=1), axis=0)
    else:
        raise DslError(f"未知的横截面算子 {how}", ctx.source)
    col = out[ctx.code] if ctx.code in out.columns else out.iloc[:, -1]
    return np.asarray(col.fillna(np.nan), dtype="float64")


class _ProgStub:
    """横截面子求值用的最小 prog:只带变量表。"""

    __slots__ = ("vars", "source")

    def __init__(self, variables: dict):
        self.vars = variables
        self.source = ""


# ---- 序列 / 统计 ---------------------------------------------------

_register("ma", (1, 2), lambda ctx, a: np.asarray(
    _series(ctx.close if len(a) == 1 else a[0]).rolling(
        _win(a[-1], "ma", ctx), min_periods=1).mean(), dtype="float64"),
    "ma(n) 收盘 n 日均线;ma(x,n) 任意序列 n 日均线", window_at=-1, group="均线",
    cols=("close",))
_register("ema", (1, 2), lambda ctx, a: np.asarray(
    _series(ctx.close if len(a) == 1 else a[0]).ewm(
        span=_win(a[-1], "ema", ctx), adjust=False).mean(), dtype="float64"),
    "ema(n) 指数均线(span=n);ema(x,n) 任意序列", window_at=-1,
    lb_mult=3, group="均线", cols=("close",))
_register("wma", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "wma", ctx), min_periods=1).apply(
        lambda w: float(np.dot(w, np.arange(1, len(w) + 1))
                        / np.arange(1, len(w) + 1).sum()), raw=True),
    dtype="float64"), "wma(x,n) 线性加权均线(越近权重越大)", window_at=-1, group="均线")
_register("sma", (2, 3), lambda ctx, a: np.asarray(
    _series(a[0]).ewm(alpha=_scalar(a[2]) / max(_win(a[1], "sma", ctx), 1),
                      adjust=False).mean(), dtype="float64")
    if len(a) > 2 else np.asarray(
        _series(a[0]).rolling(_win(a[1], "sma", ctx), min_periods=1).mean(),
        dtype="float64"),
    "sma(x,n[,m]) 通达信加权均线 (m*x+(n-m)*昨)/n;省略 m 即普通均线",
    window_at=1, lb_mult=3, group="均线")
_register("vol_ma", (1, 1), lambda ctx, a: np.asarray(
    _series(ctx.volume).rolling(_win(a[0], "vol_ma", ctx), min_periods=1).mean(),
    dtype="float64"), "vol_ma(n) 成交量 n 日均线", window_at=0, group="均线",
    cols=("volume",))
_register("sum", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "sum", ctx), min_periods=1).sum(),
    dtype="float64"), "sum(x,n) n 根求和", window_at=-1, group="统计")
_register("count", (2, 2), lambda ctx, a: np.asarray(
    _series(_bool(a[0]).astype("float64")).rolling(
        _win(a[1], "count", ctx), min_periods=1).sum(), dtype="float64"),
    "count(条件,n) n 根内成立了几次", window_at=-1, group="统计")
_register("hhv", (2, 2), lambda ctx, a: _roll(a[0], _win(a[1], "hhv", ctx), "max"),
    "hhv(x,n) n 根内最高", window_at=-1, group="极值")
_register("llv", (2, 2), lambda ctx, a: _roll(a[0], _win(a[1], "llv", ctx), "min"),
    "llv(x,n) n 根内最低", window_at=-1, group="极值")
_register("ref", (2, 2), lambda ctx, a: _shift(a[0], _lag(a[1], "ref", ctx)),
    "ref(x,n) n 根之前的 x(n≥0)", window_at=-1, group="引用")
_register("delta", (2, 2), lambda ctx, a: _num(a[0]) - _shift(
    a[0], _lag(a[1], "delta", ctx)), "delta(x,n) = x − ref(x,n)",
    window_at=-1, group="动量")
_register("pct", (1, 1), lambda ctx, a: (
    np.asarray(ctx.close, dtype="float64") / _shift(ctx.close, _win(a[0], "pct", ctx))
    - 1.0) * 100.0, "pct(n) 收盘 n 日涨幅(%)", window_at=0, group="动量",
    cols=("close",))
_register("roc", (2, 2), lambda ctx, a: (
    _num(a[0]) / _shift(a[0], _win(a[1], "roc", ctx)) - 1.0) * 100.0,
    "roc(x,n) 序列 n 根变化率(%)", window_at=-1, group="动量")
_register("std", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "std", ctx), min_periods=2).std(ddof=0),
    dtype="float64"), "std(x,n) n 根标准差", window_at=-1, group="统计")
_register("rank", (1, 1), lambda ctx, a: _xs_apply(ctx, a[0], "rank"),
    "rank(x) 横截面百分位排名 0~1", xs=True, group="横截面")
_register("zscore", (1, 1), lambda ctx, a: _xs_apply(ctx, a[0], "zscore"),
    "zscore(x) 横截面标准化(同一时刻跨票)", xs=True, group="横截面")
_register("demean", (1, 1), lambda ctx, a: _xs_apply(ctx, a[0], "demean"),
    "demean(x) 横截面去均值", xs=True, group="横截面")
_register("winsorize", (1, 2), lambda ctx, a: _xs_apply(
    ctx, a[0], "winsorize",
    _scalar(_const_arg(ctx, a[1], "winsorize")) if len(a) > 1 else None),
    "winsorize(x[,p]) 横截面缩尾,默认去掉上下 1%", xs=True, group="横截面")
_register("ts_rank", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "ts_rank", ctx), min_periods=2).apply(
        lambda w: float(pd.Series(w).rank(pct=True).iloc[-1]), raw=True),
    dtype="float64"), "ts_rank(x,n) 当前值在最近 n 根里的分位 0~1",
    window_at=-1, group="时序")
_register("ts_argmax", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "ts_argmax", ctx), min_periods=1).apply(
        lambda w: float(len(w) - 1 - int(np.argmax(w))), raw=True),
    dtype="float64"), "ts_argmax(x,n) 最近 n 根最高点距今几根", window_at=-1, group="时序")
_register("ts_argmin", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "ts_argmin", ctx), min_periods=1).apply(
        lambda w: float(len(w) - 1 - int(np.argmin(w))), raw=True),
    dtype="float64"), "ts_argmin(x,n) 最近 n 根最低点距今几根", window_at=-1, group="时序")
_register("ts_corr", (3, 3), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[2], "ts_corr", ctx), min_periods=3).corr(
        _series(a[1])), dtype="float64"),
    "ts_corr(x,y,n) n 根滚动相关系数", window_at=-1, group="时序")
_register("decay_linear", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "decay_linear", ctx), min_periods=1).apply(
        lambda w: float(np.dot(w, np.arange(1, len(w) + 1))
                        / np.arange(1, len(w) + 1).sum()), raw=True),
    dtype="float64"), "decay_linear(x,n) 线性衰减加权平均", window_at=-1, group="时序")
_register("ts_std", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "ts_std", ctx), min_periods=2).std(ddof=0),
    dtype="float64"), "ts_std(x,n) 时序标准差", window_at=-1, group="时序")
_register("ts_mean", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "ts_mean", ctx), min_periods=1).mean(),
    dtype="float64"), "ts_mean(x,n) 时序均值(= ma(x,n))", window_at=-1, group="时序")

# ---- 统计矩与回归(补 Alpha158 用到、这里缺的算子) ------------------
# qlib 的 Alpha158 里 Slope/Rsquare/Resi/skew/kurt/quantile 都是主力,
# 缺了就只能算均值方差那几样,因子空间窄一大截。


def _linfit(w) -> tuple[float, float, float]:
    """对一段窗口做 y ~ a + b*x 的最小二乘,返回 (斜率, R², 末点残差)。"""
    y = np.asarray(w, dtype="float64")
    n = len(y)
    if n < 2 or not np.all(np.isfinite(y)):
        return (np.nan, np.nan, np.nan)
    x = np.arange(n, dtype="float64")
    xm, ym = x.mean(), y.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx <= 0:
        return (0.0, 0.0, 0.0)
    b = float(((x - xm) * (y - ym)).sum() / sxx)
    a = ym - b * xm
    yhat = a + b * x
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - ym) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return (b, r2, float(y[-1] - yhat[-1]))


def _roll_linfit(s: pd.Series, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """滚动线性回归的向量化版:一次算出 (斜率, R², 末点残差)。

    原来 slope/rsquare/resi 各自 rolling(n).apply(_linfit),等于在 Python 里
    逐窗口循环 —— 300 只 × 250 天要 4.7 秒。这里用 sliding_window_view
    一次算完三样,同一份中间量复用,快一个数量级。
    """
    y = s.to_numpy(dtype="float64")
    m = len(y)
    nan_out = np.full(m, np.nan)
    if n < 2 or m < n:
        return nan_out, nan_out.copy(), nan_out.copy()

    win = np.lib.stride_tricks.sliding_window_view(y, n)   # (m-n+1, n)
    ok = np.isfinite(win).all(axis=1)                       # 窗口内不能有 NaN
    x = np.arange(n, dtype="float64")
    xm = x.mean()
    xc = x - xm
    sxx = float((xc ** 2).sum())

    slope = np.full(win.shape[0], np.nan)
    r2 = np.full(win.shape[0], np.nan)
    resi = np.full(win.shape[0], np.nan)
    if ok.any():
        w = win[ok]
        ym = w.mean(axis=1, keepdims=True)
        yc = w - ym
        b = (yc * xc).sum(axis=1) / sxx
        yhat_c = b[:, None] * xc              # 相对均值的拟合值
        ss_res = ((yc - yhat_c) ** 2).sum(axis=1)
        ss_tot = (yc ** 2).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(ss_tot > 0, 1.0 - ss_res / ss_tot, 0.0)
        slope[ok] = b
        r2[ok] = r
        resi[ok] = yc[:, -1] - yhat_c[:, -1]

    def pad(arr):
        out = np.full(m, np.nan)
        out[n - 1:] = arr
        return out

    return pad(slope), pad(r2), pad(resi)


_register("skew", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "skew", ctx), min_periods=3).skew(),
    dtype="float64"), "skew(x,n) 时序偏度", window_at=-1, group="时序")
_register("kurt", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "kurt", ctx), min_periods=4).kurt(),
    dtype="float64"), "kurt(x,n) 时序峰度", window_at=-1, group="时序")
_register("var", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "var", ctx), min_periods=2).var(ddof=0),
    dtype="float64"), "var(x,n) 时序方差", window_at=-1, group="时序")
_register("med", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "med", ctx), min_periods=1).median(),
    dtype="float64"), "med(x,n) 时序中位数", window_at=-1, group="时序")
_register("mad", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "mad", ctx), min_periods=2).apply(
        lambda w: float(np.abs(w - np.mean(w)).mean()), raw=True),
    dtype="float64"), "mad(x,n) 平均绝对偏差", window_at=-1, group="时序")
_register("quantile", (2, 3), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "quantile", ctx), min_periods=2).quantile(
        float(_scalar(a[2])) if len(a) > 2 else 0.5),
    dtype="float64"), "quantile(x,n,q) 滚动分位数,q 默认 0.5", window_at=1, group="时序")
_register("prod", (2, 2), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[1], "prod", ctx), min_periods=1).apply(
        lambda w: float(np.prod(w)), raw=True),
    dtype="float64"), "prod(x,n) 滚动乘积", window_at=-1, group="时序")
_register("slope", (2, 2), lambda ctx, a: _roll_linfit(
    _series(a[0]), _win(a[1], "slope", ctx))[0],
    "slope(x,n) n 根线性回归斜率", window_at=-1, group="时序")
_register("rsquare", (2, 2), lambda ctx, a: _roll_linfit(
    _series(a[0]), _win(a[1], "rsquare", ctx))[1],
    "rsquare(x,n) 线性回归 R²(趋势的干净程度)", window_at=-1, group="时序")
_register("resi", (2, 2), lambda ctx, a: _roll_linfit(
    _series(a[0]), _win(a[1], "resi", ctx))[2],
    "resi(x,n) 当前值相对回归线的残差", window_at=-1, group="时序")
_register("ts_cov", (3, 3), lambda ctx, a: np.asarray(
    _series(a[0]).rolling(_win(a[2], "ts_cov", ctx), min_periods=3).cov(
        _series(a[1])), dtype="float64"),
    "ts_cov(x,y,n) n 根滚动协方差", window_at=-1, group="时序")

# ---- 指标 ----------------------------------------------------------

def _looks_window(node: Node) -> bool:
    """这个参数看起来像窗口(常数)还是像序列(列/变量/指标调用)。"""
    if isinstance(node, Const):
        return True
    if isinstance(node, Param):
        return True
    if isinstance(node, Unary) and node.op in ("-", "+"):
        return _looks_window(node.x)
    if isinstance(node, Series):
        return False
    if isinstance(node, Var):
        return False
    return False


def _rsi_arg(ctx, args: list[Node]):
    """rsi 有一个或两个参数,但一个参数时有两种写法,得看实参像什么。

    rsi(6)        → 窗口 6,算收盘价的 RSI(最常用,通达信/看盘软件都这么写)
    rsi(close, 6) → 同上
    rsi(close)    → 算收盘价,窗口用默认 14
    rsi(ma(5), 6) → 对任意序列算窗口 6 的 RSI

    注意 args 是**语法树节点**,不是算好的数组 —— 单参数时要先看节点长什么样,
    才能决定这一个是窗口还是序列。
    """
    if not args:
        return ctx.close, 14
    if len(args) >= 2:
        return ctx.val(args[0]), _win(ctx.val(args[1]), "rsi", ctx)
    # 只有一个参数:常数(或被 -/+/参数占位包住的常数)当窗口,其余当序列
    w = _const_value(args[0])
    if w is not None and _looks_window(args[0]):
        return ctx.close, _win(np.asarray([w]), "rsi", ctx)
    return ctx.val(args[0]), 14


def _impl_rsi(ctx, args: list[Node]) -> np.ndarray:
    """rsi 的实参不预先求值(走节点通道),这样单参数时才能按节点类型分派。"""
    seq, n = _rsi_arg(ctx, args)
    return _rsi(seq, n)


_register("rsi", (0, 2), _impl_rsi,
          "rsi(n) 收盘 RSI;rsi(x,n) 任意序列", window_at=-1, lb_mult=3,
          group="强弱", raw_args=True, cols=("close",))
_register("atr", (0, 1), lambda ctx, a: _atr(
    ctx.high, ctx.low, ctx.close,
    _win(a[0], "atr", ctx) if a else 14),
    "atr(n) n 日真实波幅(元)", window_at=0, lb_mult=3, group="波动",
    cols=("high", "low", "close"))
def _lb_macd(args) -> int | None:
    """MACD 的预热由慢线决定:快慢线各要 3×周期才稳,再加 DEA 的 9 周期。"""
    slow = 26
    if len(args) >= 2:
        v = _static_int(args[1])
        if v is None:
            return None
        slow = v
    elif len(args) == 1:
        return None                      # 只给快线,慢线仍是默认,按默认算
    return int(slow * 1.5) + 9


def _lb_kdj(args) -> int | None:
    """KDJ:n 根求 RSV,再走两级 alpha=1/3 的平滑(约 3 倍周期)。"""
    n = 9
    if args:
        v = _static_int(args[0])
        if v is None:
            return None
        n = v
    return int(n - 1 + 3 * 3)


def _lb_boll(args) -> int | None:
    n = 20
    if args:
        v = _static_int(args[0])
        if v is None:
            return None
        n = v
    return n


_register("macd_dif", (0, 2), lambda ctx, a: _macd(ctx, a)[0],
    "macd_dif([fast,slow]) DIF 线,默认 12/26", fixed_lb=35, group="趋势",
    lb_of=_lb_macd, cols=("close",))
_register("macd_dea", (0, 2), lambda ctx, a: _macd(ctx, a)[1],
    "macd_dea([fast,slow]) DEA 线,默认 12/26", fixed_lb=35, group="趋势",
    lb_of=_lb_macd, cols=("close",))
_register("macd_hist", (0, 2), lambda ctx, a: _macd(ctx, a)[2],
    "macd_hist([fast,slow]) 柱 = (DIF−DEA)×2", fixed_lb=35, group="趋势",
    lb_of=_lb_macd, cols=("close",))
_register("kdj_k", (0, 1), lambda ctx, a: _kdj(ctx, a)[0],
    "kdj_k([n]) K 线,默认 9", fixed_lb=9, group="随机", lb_of=_lb_kdj,
    cols=("high", "low", "close"))
_register("kdj_d", (0, 1), lambda ctx, a: _kdj(ctx, a)[1],
    "kdj_d([n]) D 线,默认 9", fixed_lb=9, group="随机", lb_of=_lb_kdj,
    cols=("high", "low", "close"))
_register("kdj_j", (0, 1), lambda ctx, a: _kdj(ctx, a)[2],
    "kdj_j([n]) J 线,默认 9", fixed_lb=9, group="随机", lb_of=_lb_kdj,
    cols=("high", "low", "close"))
_register("boll_up", (0, 2), lambda ctx, a: _boll(ctx, a)[0],
    "boll_up([n,k]) 布林上轨,默认 20/2", fixed_lb=20, group="布林",
    lb_of=_lb_boll, cols=("close",))
_register("boll_mid", (0, 2), lambda ctx, a: _boll(ctx, a)[1],
    "boll_mid([n,k]) 布林中轨", fixed_lb=20, group="布林", lb_of=_lb_boll,
    cols=("close",))
_register("boll_low", (0, 2), lambda ctx, a: _boll(ctx, a)[2],
    "boll_low([n,k]) 布林下轨", fixed_lb=20, group="布林", lb_of=_lb_boll,
    cols=("close",))

# ---- 逻辑 / 穿越 / 状态 --------------------------------------------

_register("cross", (2, 2), lambda ctx, a: _cross_up(a[0], a[1]),
    "cross(a,b) a 上穿 b", group="逻辑")
_register("crossunder", (2, 2), lambda ctx, a: _cross_dn(a[0], a[1]),
    "crossunder(a,b) a 下穿 b", group="逻辑")
_register("filter", (2, 2), lambda ctx, a: _filter(a[0], _win(a[1], "filter", ctx)),
    "filter(条件,n) 触发后 n 根内不再重复", window_at=-1, group="逻辑")
_register("bars_since", (1, 1), lambda ctx, a: _bars_since(a[0]),
    "bars_since(条件) 距上次成立几根,没成立过为 NaN", group="逻辑")
_register("holding", (2, 2), lambda ctx, a: _state(a[0], a[1], ctx.close, "hold"),
    "holding(开仓,平仓) 该根是否持仓 0/1", group="状态")
_register("hold_bars", (2, 2), lambda ctx, a: _state(a[0], a[1], ctx.close, "days"),
    "hold_bars(开仓,平仓) 已持有几根(开仓当天记 1)", group="状态")
_register("entry_price", (2, 2), lambda ctx, a: _state(a[0], a[1], ctx.close, "price"),
    "entry_price(开仓,平仓) 建仓时的收盘价", group="状态")
_register("pnl_pct", (2, 2), lambda ctx, a: _state(a[0], a[1], ctx.close, "pnl"),
    "pnl_pct(开仓,平仓) 相对建仓价的浮动盈亏(%)", group="状态")
_register("trade_when", (3, 3), lambda ctx, a: _trade_when(a[0], a[1], a[2]),
    "trade_when(触发,仓位值,平仓) 带状态的仓位序列", group="状态")
_register("iff", (3, 3), lambda ctx, a: np.where(
    _bool(a[0]), _num(a[1]), _num(a[2])).astype("float64"),
    "iff(条件,a,b) 条件成立取 a,否则取 b", group="逻辑")
_register("if", (3, 3), lambda ctx, a: np.where(
    _bool(a[0]), _num(a[1]), _num(a[2])).astype("float64"),
    "if(条件,a,b) 同 iff,也支持 条件 ? a : b", group="逻辑")

# ---- 个股信息(通达信兼容) -----------------------------------------

_register("capital", (0, 0), lambda ctx, a: _capital(ctx, ctx.code),
    "capital() 流通股本(手),优先用 float_cap 反推", group="个股信息")
_register("dynainfo", (1, 1), lambda ctx, a: _dynainfo(ctx, a[0]),
    "dynainfo(k) 3/10 昨收 4 今开 5 最高 6 最低 7 现价 8 成交量", group="个股信息")

# ---- 数学 ----------------------------------------------------------

_register("abs", (1, 1), lambda ctx, a: np.abs(_num(a[0])), "abs(x) 绝对值", group="数学")
_register("log", (1, 1), lambda ctx, a: np.log(np.maximum(_num(a[0]), 1e-12)),
    "log(x) 自然对数(≤0 按 1e-12)", group="数学")
_register("sqrt", (1, 1), lambda ctx, a: np.sqrt(np.maximum(_num(a[0]), 0.0)),
    "sqrt(x) 平方根(负数按 0)", group="数学")
_register("exp", (1, 1), lambda ctx, a: np.exp(np.clip(_num(a[0]), -50, 50)),
    "exp(x) 指数", group="数学")
_register("sign", (1, 1), lambda ctx, a: np.sign(np.nan_to_num(_num(a[0]))),
    "sign(x) 符号 -1/0/1", group="数学")
_register("atan", (1, 1), lambda ctx, a: np.arctan(_num(a[0])),
    "atan(x) 反正切(算角度斜率常用)", group="数学")
_register("power", (2, 2), lambda ctx, a: np.power(_num(a[0]), _num(a[1])),
    "power(x,y) 幂", group="数学")
_register("max", (2, 9), lambda ctx, a: _reduce(a, np.maximum),
    "max(a,b,...) 逐元素取大", group="数学")
_register("min", (2, 9), lambda ctx, a: _reduce(a, np.minimum),
    "min(a,b,...) 逐元素取小", group="数学")
_register("clip", (3, 3), lambda ctx, a: np.clip(_num(a[0]), _num(a[1]), _num(a[2])),
    "clip(x,下,上) 截断", group="数学")
_register("round", (1, 1), lambda ctx, a: np.round(_num(a[0])),
    "round(x) 四舍五入", group="数学")


def _reduce(args, op):
    acc = _num(args[0])
    for x in args[1:]:
        acc = op(acc, _num(x))
    return acc


def _const_arg(ctx, node: Node, fname: str) -> np.ndarray:
    if isinstance(node, Const):
        return np.asarray([node.value])
    try:
        v = ctx.val(node)
    except Exception:  # noqa: BLE001
        raise DslError(f"{fname} 的参数必须是常数", ctx.source) from None
    if np.asarray(v).ndim and np.asarray(v).size > 1 and not np.allclose(
            v, np.asarray(v).reshape(-1)[0], equal_nan=True):
        raise DslError(f"{fname} 的参数必须是常数", ctx.source)
    return np.asarray(v)


def _rsi(s, n: int) -> np.ndarray:
    """RSI:涨跌幅的 Wilder 平滑比。全程 numpy,不建 Series。"""
    x = _num(s)
    prev = _shift(x, 1)                    # _shift 给的是「上一根」
    d = x - prev
    up = np.where(np.isnan(d), np.nan, np.maximum(d, 0.0))
    dn = np.where(np.isnan(d), np.nan, np.maximum(-d, 0.0))
    a = 1.0 / n
    # 直接把带 NaN 的序列交给 _ewm:pandas 的 ewm 遇 NaN 跳过、
    # 用第一个有效观测做初值。先 nan_to_num 会把初值变成 0,RSI 就全平了。
    au = _ewm(up, a)
    ad = _ewm(dn, a)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(ad == 0, np.nan, au / ad)
    out = 100 - 100 / (1 + rs)
    return np.where(np.isnan(out), 50.0, out)


def _atr(h, l, c, n: int) -> np.ndarray:
    h, l, c = _num(h), _num(l), _num(c)
    prev = _shift(c, 1)
    hl = h - l
    hc = np.abs(h - prev)
    lc = np.abs(l - prev)
    tr = np.fmax(np.fmax(hl, hc), lc)     # fmax 忽略 NaN,和 pandas max(axis=1) 一致
    return _ewm(tr, 1.0 / n)


def _macd(ctx, a):
    fast = _win(a[0], "macd", ctx) if len(a) > 0 else 12
    slow = _win(a[1], "macd", ctx) if len(a) > 1 else 26
    c = _num(ctx.close)
    dif = _ewm(c, 2.0 / (fast + 1)) - _ewm(c, 2.0 / (slow + 1))
    dea = _ewm(dif, 2.0 / 10)              # span=9 → alpha = 2/(9+1)
    return dif, dea, (dif - dea) * 2


def _kdj(ctx, a):
    n = _win(a[0], "kdj", ctx) if a else 9
    h, l, c = _num(ctx.high), _num(ctx.low), _num(ctx.close)
    ll = _roll_ext(l, n, "min")
    hh = _roll_ext(h, n, "max")
    with np.errstate(divide="ignore", invalid="ignore"):
        rng = hh - ll
        rsv = np.where(rng == 0, np.nan, (c - ll) / rng * 100)
    rsv = np.where(np.isnan(rsv), 50.0, rsv)
    k = _ewm(rsv, 1.0 / 3)
    d = _ewm(k, 1.0 / 3)
    return k, d, 3 * k - 2 * d


def _boll(ctx, a):
    n = _win(a[0], "boll", ctx) if a else 20
    k = _scalar(a[1]) if len(a) > 1 else 2.0
    c = _num(ctx.close)
    mid = _roll_mean(c, n)
    sd = np.asarray(_series(c).rolling(n, min_periods=1).std(ddof=0),
                    dtype="float64")
    return mid + k * sd, mid, mid - k * sd


_SHARES: dict[str, tuple[float, float]] = {}
_SHARES_TTL = 6 * 3600


def _shares_live(code: str) -> float | None:
    """流通股本(手):流通市值 ÷ 现价 ÷ 100。拿不到返回 None。"""
    import time as _t
    hit = _SHARES.get(code)
    if hit and _t.time() - hit[1] < _SHARES_TTL:
        return hit[0]
    cap = price = None
    try:
        from ..data import realtime
        q = (realtime.quotes([code])[0] or {}).get(code) or {}
        cap, price = q.get("float_cap"), q.get("price")
    except Exception:  # noqa: BLE001
        pass
    if not cap or not price:
        try:
            from ..data import em
            q = em.quotes([code], fallback=False, retries=1).get(code) or {}
            cap = cap or q.get("float_cap")
            price = price or q.get("price")
        except Exception:  # noqa: BLE001
            pass
    if not cap or not price:
        return None
    shares = float(cap) / float(price) / 100.0
    _SHARES[code] = (shares, _t.time())
    return shares


def _capital(ctx, code) -> np.ndarray:
    n = ctx.n
    if "float_cap" in ctx.df.columns:
        cap = np.asarray(ctx.df["float_cap"], dtype="float64")
        if not np.all(np.isnan(cap)):
            with np.errstate(divide="ignore", invalid="ignore"):
                return cap / np.asarray(ctx.close, dtype="float64") / 100.0
    if "turnover" in ctx.df.columns:
        turn = np.asarray(ctx.df["turnover"], dtype="float64")
        if not np.all(np.isnan(turn)):
            t = pd.Series(turn).where(pd.Series(turn) > 0).ffill().bfill()
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.asarray(ctx.volume, dtype="float64") / (np.asarray(t) / 100.0)
    if code:
        shares = _shares_live(str(code))
        if shares:
            return np.full(n, float(shares))
    return np.full(n, np.nan)


def _dynainfo(ctx, k) -> np.ndarray:
    k = int(_scalar(k))
    c = np.asarray(ctx.close, dtype="float64")
    prev = np.r_[np.nan, c[:-1]] if len(c) else c
    table = {3: prev, 4: np.asarray(ctx.open, dtype="float64"),
             5: np.asarray(ctx.high, dtype="float64"),
             6: np.asarray(ctx.low, dtype="float64"),
             7: c, 8: np.asarray(ctx.volume, dtype="float64"), 10: prev}
    return np.asarray(table.get(k, c), dtype="float64")


def _suggest_func(name: str) -> str:
    import difflib
    near = difflib.get_close_matches(name.lower(), list(FUNC_SPECS), n=3, cutoff=0.5)
    if near:
        return "你是不是想写:" + " / ".join(near)
    return "按 F1 打开帮助里的「策略函数库」查可用函数"


def _suggest_name(name: str, pool) -> str:
    import difflib
    near = difflib.get_close_matches(name.lower(), [p.lower() for p in pool],
                                     n=3, cutoff=0.5)
    if near:
        return "你是不是想写:" + " / ".join(near)
    return "先赋值再用,例如 x := ma(20)"


# ------------------------------------------------------------------ 求值上下文

_CORE_COLS = ("close", "high", "low", "open", "volume", "amount")


class _Ctx:
    """一次求值的上下文:一份 K 线 + 变量 + 可选横截面面板。"""

    __slots__ = ("df", "code", "n", "source", "vars", "memo", "xs",
                 "_xs_cache", "_cols", "_var_stack")

    def __init__(self, df: pd.DataFrame, code, source: str, prog, xs=None):
        self.df, self.code, self.n, self.source = df, code, len(df), source
        self.vars = prog.vars
        self.memo, self._xs_cache, self._cols = {}, {}, {}
        self._var_stack: set[str] = set()
        self.xs = xs
        # 列按需加载:原来这里无条件把 6 个核心列全转成 float64,
        # 500 根要 206µs,而 close > 0 只需要 1 列。表达式用不到的不碰。

    def __getattr__(self, name: str) -> np.ndarray:
        """close/high/low/open/volume/amount 按需取列。

        只在属性确实没被缓存时才走到这里(__slots__ 挡住了普通 setattr),
        所以热路径上命中 _cols 就不进这个函数。
        """
        if name in _CORE_COLS:
            return self.series(name)
        raise AttributeError(name)

    def _load(self, name: str) -> np.ndarray:
        if name not in self.df.columns:
            if name == "amount":
                return np.full(self.n, np.nan)
            raise DslError(f"数据里没有 {name} 这一列", self.source, -1,
                           "可用列:" + " ".join(
                               c for c in COLUMNS if c in self.df.columns))
        arr = np.asarray(self.df[name], dtype="float64")
        self._cols[name] = arr
        return arr

    def series(self, name: str) -> np.ndarray:
        hit = self._cols.get(name)
        return hit if hit is not None else self._load(name)

    def var(self, name: str) -> np.ndarray:
        node = self.vars[name]
        key = id(node)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        if name in self._var_stack:
            raise DslError(f"变量 {name} 循环引用", self.source)
        self._var_stack.add(name)
        try:
            val = self.val(node)
        finally:
            self._var_stack.discard(name)
        return val

    def val(self, node: Node) -> np.ndarray:
        key = id(node)
        hit = self.memo.get(key)
        if hit is not None:
            return hit
        out = np.asarray(node.eval(self), dtype="float64")
        if out.ndim == 0:
            out = np.full(self.n, float(out))
        elif len(out) != self.n:
            raise DslError(f"长度不一致:{len(out)} vs {self.n}", self.source)
        self.memo[key] = out
        return out

    def call(self, name: str, args: list[Node], pos: int = -1) -> np.ndarray:
        spec = FUNC_SPECS[name]
        lo, hi = spec["arity"]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = str(lo) if hi == lo else f"{lo}~{hi}"
            raise DslError(f"{name} 需要 {want} 个参数,你给了 {len(args)} 个",
                           self.source, pos, spec["doc"])
        if spec["xs"] or spec.get("raw_args"):
            return np.asarray(spec["impl"](self, args), dtype="float64")
        vals = [self.val(a) for a in args]
        return np.asarray(spec["impl"](self, vals), dtype="float64")


# ------------------------------------------------------------------ 程序

class Program:
    """编译后的表达式。同一个字符串只解析一次,多票复用。"""

    __slots__ = ("source", "stmts", "vars", "result", "label", "funcs_def",
                 "raw", "_lb", "_needs_xs", "_funcs", "_names", "_nparams")

    def __init__(self, source: str, stmts, result: Node, label: str,
                 funcs_def: dict | None = None, raw=None):
        self.source = source
        self.stmts = stmts
        self.vars = dict(stmts)
        self.result = result
        self.label = label
        self.funcs_def = dict(funcs_def or {})
        # 优化前的原始树。lint 要检查「你写的东西」,不是折叠后的样子:
        # 1>2>3 折成常数之后就看不出它本来是链式比较了。
        self.raw = raw if raw is not None else (stmts, result)
        self._lb = self._needs_xs = self._funcs = self._names = self._nparams = None

    # -- 优化:常量折叠 + 公共子式消除 ---------------------------
    def optimize(self) -> "Program":
        """返回化简后的新 Program(不改自己,缓存里的那份保持原样)。

        求值是解释式的,同一条子式写两遍就会被算两遍——除了求值器里
        id(node) 的记忆化能救回来的那部分。这里在编译期把结构相同的子树
        合并成同一个对象,记忆化自然全中;顺带把常数部分算掉。
        """
        tab: dict = {}
        vars_ = [(name, _simplify(node, tab)) for name, node in self.stmts]
        result = _simplify(self.result, tab)
        return Program(self.source, vars_, result, self.label, self.funcs_def,
                       raw=self.raw)

    def plan(self) -> str:
        """把优化后的求值顺序摊开:谁先算、算几次、有多少节点被省掉。"""
        stmts0, result0 = self.raw
        before = _count_nodes(result0) + sum(_count_nodes(n) for _, n in stmts0)
        after = _count_nodes(self.result) + sum(
            _count_nodes(n) for _, n in self.stmts)

        lines = [f"原始 {before} 个节点 → 优化后 {after} 个"
                 f"(省 {before - after})"]
        order: list[str] = []
        seen: set = set()

        def walk(node: Node, depth: int) -> None:
            if id(node) in seen:
                order.append(f"  {depth * 2 * ' '}(复用 {_render_node(node)[:40]})")
                return
            seen.add(id(node))
            for c in node.children():
                walk(c, depth + 1)
            order.append(f"  {depth * 2 * ' '}{_render_node(node)[:64]}")

        walk(self.result, 0)
        if order:
            lines.append("求值顺序(后序):")
            lines.extend(order[:30])
            if len(order) > 30:
                lines.append(f"  …还有 {len(order) - 30} 步")
        return "\n".join(lines)

    def saved_nodes(self) -> int:
        """编译期省掉多少节点(原始节点数 − 优化后节点数)。"""
        stmts0, result0 = self.raw
        before = _count_nodes(result0) + sum(_count_nodes(n) for _, n in stmts0)
        after = _count_nodes(self.result) + sum(
            _count_nodes(n) for _, n in self.stmts)
        return max(0, before - after)

    def n_params(self) -> int:
        """表达式里用到几个参数占位(#n 的最大序号)。"""
        if self._nparams is None:
            acc = 0

            def walk(node):
                nonlocal acc
                if isinstance(node, Param):
                    acc = max(acc, node.index)
                for c in node.children():
                    walk(c)
            walk(self.result)
            for _, node in self.vars.items():
                walk(node)
            self._nparams = acc
        return self._nparams

    def bind(self, *params: float) -> "Program":
        """把 #1 #2 … 绑成常数,返回新 Program。

        参数占位是给参数网格用的:一条公式跑一片参数,不用 str.format 拼字符串。
        """
        need = self.n_params()
        if need and len(params) < need:
            raise DslError(f"这条表达式有 {need} 个参数,你只给了 {len(params)} 个",
                           self.source, -1, f"例如 evaluate(expr, df, params={list(range(1, need + 1))})")
        if not need:
            return self
        env = {("#", i + 1): Const(v) for i, v in enumerate(params)}
        return Program(self.source, [
            (k, v.subst(env)) for k, v in self.stmts
        ], self.result.subst(env), self.label, self.funcs_def)

    def lookback(self) -> int:
        if self._lb is None:
            self._lb = self.result.lookback(self)
        return self._lb

    def funcs(self) -> set[str]:
        if self._funcs is None:
            acc: set[str] = set()

            def walk(node):
                if isinstance(node, Call):
                    acc.add(node.name)
                for c in node.children():
                    walk(c)
            walk(self.result)
            for _, node in self.vars.items():
                walk(node)
            self._funcs = acc
        return self._funcs

    def needs_xs(self) -> bool:
        if self._needs_xs is None:
            self._needs_xs = bool(self.funcs() & _XS_FUNCS)
        return self._needs_xs

    def names(self) -> set[str]:
        if self._names is None:
            acc: set[str] = set()
            self.result.names(acc)
            for _, node in self.vars.items():
                node.names(acc)
            # 再补上函数隐式读的列:ma(20)、macd_hist 这类只给窗口不给序列,
            # 语法树上没有 Series 节点,只靠上面的收集会漏掉 close。
            # 判据是「实参里到底有没有序列」,不能只看参数个数:
            #   ma(20)        没序列 -> 补 close
            #   ma(volume,20) 有序列 -> 不补
            #   rsi(14)       没序列 -> 补 close
            #   rsi(close,14) 有序列 -> 不补
            implicit: set[str] = set()

            def has_seq(nodes) -> bool:
                for a in nodes:
                    if isinstance(a, (Series, Var)):
                        return True
                    if isinstance(a, (Call, Unary, Bin, Ternary)) and \
                            has_seq(a.children()):
                        return True
                return False

            def walk(node):
                if isinstance(node, Call):
                    spec = FUNC_SPECS.get(node.name)
                    if spec and spec.get("cols") and not has_seq(node.args):
                        implicit.update(spec["cols"])
                for c in node.children():
                    walk(c)
            walk(self.result)
            for _, node in self.vars.items():
                walk(node)
            acc |= implicit
            self._names = acc - set(self.vars)
        return self._names

    def run(self, df: pd.DataFrame, code=None, xs=None, warmup: bool = True):
        if df is None or len(df) == 0:
            return np.zeros(0)
        ctx = _Ctx(df, code, self.source, self, xs=xs)
        out = ctx.val(self.result)
        if warmup:
            lb = self.lookback()
            if 0 < lb < len(out):
                out = out.copy()
                out[:lb] = np.nan
        return out

    def explain(self) -> str:
        return (f"预热 {self.lookback()} 根 / 函数 "
                f"{' '.join(sorted(self.funcs())) or '(无)'} / 列 "
                f"{' '.join(sorted(self.names())) or '(无)'}")

    # -- 调试:把每个子式的值摊开 --------------------------------
    def trace(self, df: pd.DataFrame, code=None, at: int = -1, limit: int = 40):
        """逐个子式求值,返回 [(路径, 表达式, 该根的值)]。

        比 print 强的地方:它走的是同一条求值路径,看到的就是真实值,
        而且不用改表达式就能定位「到底哪一段判错了」。
        """
        rows: list[tuple[str, str, float]] = []
        if df is None or len(df) == 0:
            return rows
        i = at if at >= 0 else len(df) - 1
        ctx = _Ctx(df, code, self.source, self)

        def walk(node: Node, path: str, depth: int) -> None:
            if depth > 12 or len(rows) >= limit:
                return
            children = node.children()
            if children:
                for k, c in enumerate(children):
                    walk(c, f"{path}.{k}" if path else str(k), depth + 1)
            try:
                v = ctx.val(node)
            except DslError:
                return
            if len(rows) < limit:
                rows.append((path or "root", _render_node(node)[:70],
                             float(v[i]) if i < len(v) else float("nan")))

        walk(self.result, "", 0)
        return rows

    # -- 静态体检:能在不跑数据的情况下说出来的问题 ---------------
    def lint(self) -> list[str]:
        """静态检查,返回给人看的提醒(不是错误,是「这里可能写错了」)。

        检查的是**优化前**那棵树:1 > 2 > 3 折叠成常数之后,就看不出
        它本来是个链式比较了。
        """
        out: list[str] = []
        used_vars = set()
        stmts, result = self.raw

        def walk(node: Node, depth: int) -> None:
            if isinstance(node, Var):
                used_vars.add(node.name)
            if isinstance(node, Call):
                spec = FUNC_SPECS.get(node.name)
                if spec and spec["window_at"] is not None:
                    idx = spec["window_at"]
                    idx = idx if idx >= 0 else len(node.args) + idx
                    if 0 <= idx < len(node.args):
                        w = _const_value(node.args[idx])
                        if w is not None and node.name not in ("ref", "delta") and w == 1:
                            out.append(f"{node.name}(...,1) 等于原序列,这个窗口没有意义")
                if node.name in _XS_FUNCS and depth == 0:
                    out.append(f"{node.name} 是横截面算子,必须多票一起算(回测/选股/自动交易)")
            if isinstance(node, ChainCmp):
                consts = [_const_value(n) for n in node.nodes]
                if all(c is not None for c in consts):
                    out.append(f"「{_render_node(node)}」全是常数,结果恒定,不是条件")
                elif sum(c is not None for c in consts) >= 2:
                    out.append(f"「{_render_node(node)}」是链式比较(区间),"
                               f"确认你要的是 {node.ops[0]} 且 {node.ops[1]} 同时成立")
            if isinstance(node, Bin) and node.op in _CMP:
                a, b = _const_value(node.a), _const_value(node.b)
                if a is not None and b is not None:
                    out.append(f"「{_render_node(node)}」两边都是常数,"
                               f"结果恒为 {_cmp(node.op, [a], [b])[0]},不是条件")
            if isinstance(node, Call) and node.name == "ref":
                w = _const_value(node.args[1]) if len(node.args) > 1 else None
                if w == 0:
                    out.append("ref(x,0) 就是 x 本身,可以直接写 x")
            for c in node.children():
                walk(c, depth + 1)

        walk(result, 0)
        for name, node in stmts:
            walk(node, 1)
        if stmts:
            unused = [k for k, _ in stmts if k not in used_vars]
            if unused:
                out.append("这些变量定义了但没用到:" + " ".join(unused))
        need = self.lookback()
        if need > 250:
            out.append(f"预热要 {need} 根,日线要拉一年以上才稳,确认数据够长")
        return out


_CACHE: dict[str, Program | DslError] = {}
_CACHE_MAX = 256


def compile_expr(expr: str) -> Program:
    """解析成可执行程序。解析失败抛 DslError,结果按字符串缓存。"""
    key = (expr or "").strip()
    if not key:
        raise DslError("条件为空", "", -1, "至少写一个条件,例如 close > ma(20)")
    hit = _CACHE.get(key)
    if isinstance(hit, Program):
        return hit
    if isinstance(hit, DslError):
        raise hit
    try:
        p = _Parser(key)
        stmts, result, label = p.program()
        prog = Program(key, stmts, result, label, getattr(p, "funcs", None))
        _check_static(prog)
        prog = prog.optimize()      # 常量折叠 + 公共子式消除
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = prog
        return prog
    except DslError as e:
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = e.with_expr(key)
        raise _CACHE[key]
    except RecursionError:
        raise DslError("表达式嵌套太深", key, -1, "拆成几个变量,用 := 分段") from None


def _check_static(prog: Program) -> None:
    """常数参数的静态校验,让 ma(close,0) / ref(close,-1) 在保存前就报错。"""

    def walk(node: Node):
        if isinstance(node, Call):
            spec = FUNC_SPECS.get(node.name)
            if spec and spec["window_at"] is not None:
                idx = spec["window_at"]
                idx = idx if idx >= 0 else len(node.args) + idx
                # rsi 单参数时既可以写 rsi(6)(窗口)也可以写 rsi(close)(序列),
                # 语义在运行期按实参类型分派,编译期不做「窗口必须是常数」的检查。
                loose = node.name == "rsi" and len(node.args) == 1
                if 0 <= idx < len(node.args) and not loose:
                    arg = node.args[idx]
                    const = _const_value(arg)
                    if const is not None:
                        if node.name in ("ref", "delta"):
                            _lag(np.asarray([const]), node.name)
                        else:
                            _win(np.asarray([const]), node.name)
                    elif isinstance(arg, Series):
                        raise DslError(
                            f"{node.name} 的窗口参数给成了序列 {arg.name}",
                            prog.source, -1,
                            f"正确写法:{node.name}(序列, 窗口),例如 "
                            f"{node.name}(close, 20)")
            for c in node.args:
                walk(c)
            return
        for c in node.children():
            walk(c)

    walk(prog.result)
    for _, node in prog.vars.items():
        walk(node)


# ------------------------------------------------------------------ 公共 API

def validate(expr: str) -> tuple[bool, str]:
    """校验表达式。返回 (是否通过, 错误说明)。不执行 K 线计算。"""
    if not expr or not str(expr).strip():
        return False, "条件为空"
    try:
        compile_expr(str(expr))
        return True, ""
    except DslError as e:
        return False, str(e)
    except RecursionError:
        return False, "表达式嵌套太深,拆成几个变量吧"


def required_bars(expr: str) -> int:
    """这条表达式至少要多少根 K 线才算稳(预热长度)。"""
    try:
        return compile_expr(expr).lookback()
    except DslError:
        return 0


def evaluate(expr: str, df: pd.DataFrame, code: str | None = None, *,
             xs: dict | None = None, warmup: bool = True,
             as_bool: bool = True, params: list | tuple | None = None) -> np.ndarray:
    """对一只票求值。默认返回 bool 数组(非 0 为真)。

    warmup=True 时预热区置 False,避免半条均线发假信号。
    params 用来绑表达式里的 #1 #2 …(参数网格用)。
    """
    prog = compile_expr(expr)
    if params:
        prog = prog.bind(*params)
    out = prog.run(df, code, xs=xs, warmup=warmup)
    if not as_bool:
        return np.asarray(out, dtype="float64")
    if out.dtype == bool:
        return np.nan_to_num(out.astype("float64")) != 0
    return np.nan_to_num(np.asarray(out, dtype="float64"), nan=0.0) != 0


def evaluate_value(expr: str, df: pd.DataFrame, code: str | None = None, *,
                   xs: dict | None = None, warmup: bool = False,
                   params: list | tuple | None = None) -> np.ndarray:
    """求值并保留原始数值(不转 bool),给画线/打分用。"""
    prog = compile_expr(expr)
    if params:
        prog = prog.bind(*params)
    return np.asarray(prog.run(df, code, xs=xs, warmup=warmup), dtype="float64")


def param_count(expr: str) -> int:
    """这条表达式用了几个参数占位(#n 的最大序号)。"""
    try:
        return compile_expr(expr).n_params()
    except DslError:
        return 0


def substitute(expr: str, mapping: dict) -> str:
    """把 #n 换成具体数字,产出可以直接存库的表达式。

    参数网格跑完想把某一组参数落成规则时用,比 str.format 安全
    (表达式本身有语法错会直接报出来,而不是拼出一句坏代码)。
    ``mapping`` 的键是参数序号:`{1: 20, 2: 5}`。
    """
    prog = compile_expr(expr)
    if not prog.n_params():
        return prog.source
    env = {}
    for k, v in mapping.items():
        idx = int(k[1:]) if isinstance(k, str) else int(k)
        env[("#", idx)] = Const(float(v))
    missing = [i for i in range(1, prog.n_params() + 1) if ("#", i) not in env]
    if missing:
        raise DslError(f"参数 {missing} 没有给值", prog.source, -1,
                       f"这条表达式有 {prog.n_params()} 个参数")
    return _render_node(prog.result.subst(env))


def render(expr: str, params: list | tuple) -> str:
    """按位置给参数赋值并渲染成文本:render("ma(close,#1)", [20]) → ma(close, 20)。"""
    prog = compile_expr(expr)
    need = prog.n_params()
    if need and len(params) < need:
        raise DslError(f"这条表达式有 {need} 个参数,你给了 {len(params)} 个",
                       prog.source, -1, "按 #1 #2 … 的顺序给")
    subs = {("#", i + 1): Const(float(v)) for i, v in enumerate(params)}
    return _render_node(prog.result.subst(subs)) if need else prog.source


_PREC = {"?:": 1, "or": 2, "||": 2, "and": 3, "&&": 3, "not": 4,
         "==": 5, "!=": 5, ">": 5, ">=": 5, "<": 5, "<=": 5,
         "+": 6, "-": 6, "*": 7, "/": 7, "%": 7, "^": 8, "**": 8}


def _render_node(node: Node, parent: int = 0) -> str:
    """把算子树打印回 DSL 源码。按优先级决定要不要加括号,不是一律加。"""
    text = _render_raw(node)
    if _prec_of(node) < parent:
        return f"({text})"
    return text


def _prec_of(node: Node) -> int:
    if isinstance(node, (Bin,)):
        return _PREC.get(node.op, 9)
    if isinstance(node, Ternary):
        return 1
    if isinstance(node, Unary):
        return 4 if node.op == "not" else 8
    return 9


def _render_raw(node: Node) -> str:
    if isinstance(node, Const):
        v = node.value
        return str(int(v)) if float(v).is_integer() else f"{v:g}"
    if isinstance(node, Param):
        return f"#{node.index}"
    if isinstance(node, (Series, Var)):
        return node.name
    if isinstance(node, Unary):
        inner = _render_node(node.x, 9)
        return f"-{inner}" if node.op == "-" else \
            (f"+{inner}" if node.op == "+" else f"not {inner}")
    if isinstance(node, Bin):
        p = _PREC.get(node.op, 9)
        # 右结合的两个运算符要特殊处理:减法右边的加减、除法右边的乘除
        left = _render_node(node.a, p)
        right = _render_node(node.b, p + 1 if node.op in ("-", "/", "%") else p)
        return f"{left} {node.op} {right}"
    if isinstance(node, ChainCmp):
        parts = [_render_node(n, 6) for n in node.nodes]
        out = parts[0]
        for op, p in zip(node.ops, parts[1:]):
            out += f" {op} {p}"
        return out
    if isinstance(node, Ternary):
        return (f"{_render_node(node.cond, 2)} ? {_render_node(node.a, 1)}"
                f" : {_render_node(node.b, 1)}")
    if isinstance(node, Call):
        return f"{node.name}({', '.join(_render_node(a, 1) for a in node.args)})"
    return "?"


def clear_caches() -> None:
    """清掉表达式编译缓存与横截面面板缓存(批量任务收尾时调用)。"""
    _CACHE.clear()
    _PANEL_CACHE.clear()


def docs() -> list[tuple[str, str, str]]:
    """(分组, 名字, 说明),给帮助窗口和 README 生成函数表。"""
    return [(spec["group"], name, spec["doc"]) for name, spec in FUNC_SPECS.items()]


#: 全部函数名,给帮助窗口做覆盖自检
FUNCS = tuple(sorted(FUNC_SPECS))

#: 全部列名(含通达信简写)
COLUMNS_ALL = tuple(list(COLUMNS) + list(ALIASES))
