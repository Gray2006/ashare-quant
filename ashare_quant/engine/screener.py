"""安全的选股表达式。

不调用 eval / exec，只解析白名单内的 AST 节点；禁止属性访问、下标、
推导式、lambda 与任何导入。适合接收用户输入的筛选条件。

示例：
    close > 0 and between(turnover, 1, 10) and between(pe_ttm, 0, 40)
    rank(momentum_20) > 0.8 and volatility_20 < 0.5
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Mapping

import numpy as np
import pandas as pd


class ScreenError(ValueError):
    pass


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def _rank(series: pd.Series) -> pd.Series:
    return series.rank(pct=True)


def _zscore(series: pd.Series) -> pd.Series:
    std = series.std()
    return (series - series.mean()) / std if std else series * 0.0


def _between(series: pd.Series, low: float, high: float) -> pd.Series:
    return series.between(low, high)


_FUNCS = {
    "abs": np.abs,
    "min": np.minimum,
    "max": np.maximum,
    "round": np.round,
    "log": np.log,
    "sqrt": np.sqrt,
    "sign": np.sign,
    "rank": _rank,
    "zscore": _zscore,
    "clip": lambda s, low, high: s.clip(low, high),
    "between": _between,
    "isna": pd.isna,
    "notna": pd.notna,
}

_MAX_EXPR_LENGTH = 1000


class _Evaluator:
    def __init__(self, names: Mapping[str, Any]) -> None:
        self.names = names

    def eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return self.eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, str, bool)) or node.value is None:
                return node.value
            raise ScreenError("不支持的常量类型")
        if isinstance(node, ast.Name):
            if node.id in ("True", "true"):
                return True
            if node.id in ("False", "false"):
                return False
            if node.id not in self.names:
                raise ScreenError(f"未知字段: {node.id}")
            return self.names[node.id]
        if isinstance(node, ast.UnaryOp):
            value = self.eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.Not):
                return ~value.astype(bool) if isinstance(value, pd.Series) else not value
            raise ScreenError("不支持的一元运算")
        if isinstance(node, ast.BinOp):
            func = _BIN_OPS.get(type(node.op))
            if not func:
                raise ScreenError("不支持的二元运算")
            left, right = self.eval(node.left), self.eval(node.right)
            result = func(left, right)
            if isinstance(result, pd.Series):
                result = result.replace([np.inf, -np.inf], np.nan)
            return result
        if isinstance(node, ast.BoolOp):
            values = [self.eval(value) for value in node.values]
            result = values[0]
            for value in values[1:]:
                if isinstance(node.op, ast.And):
                    result = result & value
                else:
                    result = result | value
            return result
        if isinstance(node, ast.Compare):
            left = self.eval(node.left)
            result = None
            for op, comparator_node in zip(node.ops, node.comparators):
                func = _CMP_OPS.get(type(op))
                if not func:
                    raise ScreenError("不支持的比较运算")
                right = self.eval(comparator_node)
                piece = func(left, right)
                result = piece if result is None else result & piece
                left = right
            return result
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ScreenError("只允许调用内置函数")
            name = node.func.id
            if name not in _FUNCS:
                raise ScreenError(f"不允许的函数: {name}")
            if node.keywords:
                raise ScreenError("函数不支持关键字参数")
            args = [self.eval(arg) for arg in node.args]
            try:
                return _FUNCS[name](*args)
            except TypeError as exc:
                raise ScreenError(f"函数 {name} 参数不匹配") from exc
        raise ScreenError(f"不支持的语法: {type(node).__name__}")


def evaluate(expr: str, frame: pd.DataFrame,
             extra: Mapping[str, Any] | None = None) -> Any:
    if not expr or not expr.strip():
        raise ScreenError("表达式为空")
    if len(expr) > _MAX_EXPR_LENGTH:
        raise ScreenError("表达式过长")
    names: dict[str, Any] = {column: frame[column] for column in frame.columns}
    if extra:
        names.update(extra)
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ScreenError(f"语法错误: {exc.msg}") from exc
    return _Evaluator(names).eval(tree)


def screen(
    frame: pd.DataFrame,
    expr: str,
    limit: int | None = None,
    sort_by: str | None = None,
    ascending: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = evaluate(expr, frame, extra)
    if isinstance(result, pd.Series):
        mask = result.fillna(False).astype(bool)
    elif isinstance(result, (bool, np.bool_)):
        mask = pd.Series(bool(result), index=frame.index)
    else:
        raise ScreenError("表达式结果不是布尔条件")
    selected = frame[mask].copy()
    if sort_by:
        if sort_by not in selected.columns:
            raise ScreenError(f"排序字段不存在: {sort_by}")
        selected = selected.sort_values(sort_by, ascending=ascending)
    if limit:
        selected = selected.head(int(limit))
    return selected.reset_index(drop=True)


def attach_factors(frame: pd.DataFrame, panel: dict[str, pd.DataFrame],
                   names: list[str] | None = None) -> pd.DataFrame:
    """把最新一日的因子值按代码拼到快照表上，供表达式引用。"""
    from . import factors

    out = frame.copy()
    wanted = names if names is not None else list(factors.FACTORS)
    for name in wanted:
        if name not in factors.FACTORS:
            continue
        try:
            series = factors.latest(name, panel)
        except (KeyError, ValueError):
            continue
        if series.empty:
            continue
        out[name] = out["code"].map(series)
    return out


def factor_snapshot(panel: dict[str, pd.DataFrame],
                    names: list[str] | None = None) -> pd.DataFrame:
    """最新一日的横截面因子表，index=code。"""
    from . import factors

    wanted = names if names is not None else list(factors.FACTORS)
    columns = {}
    for name in wanted:
        if name not in factors.FACTORS:
            continue
        series = factors.latest(name, panel)
        if not series.empty:
            columns[name] = series
    return pd.DataFrame(columns)
