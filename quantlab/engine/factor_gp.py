# -*- coding: utf-8 -*-
"""遗传式因子挖掘(表达式树搜索),参考幻方那类「自动挖因子」的做法。

和模板挖掘的区别:
  * 不是从固定 50 个模板里挑,而是用表达式树做交叉/变异,持续生成新公式;
  * 适应度用**样本外验证**:前 70% 时间训练,后 30% 测试,测试段同向才给分;
  * 复杂度惩罚(树深),覆盖率门槛,相关性去重;
  * 产出仍是 dsl 表达式,可直接复制去选股/回测/自动交易。

规模是家用级的:种群 40、代数 10 左右,不是幻方那种集群;但方法一致。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import dsl
from . import factor_mine as fm

VARS = ("close", "open", "high", "low", "volume", "amount")
WINDOWS = (3, 5, 10, 20, 60)
UNARY = ("abs", "sign", "log", "sqrt", "rank", "zscore")
BINARY = ("+", "-", "*", "/")
WINDOWED = ("ts_mean", "ts_std", "ts_rank", "delta", "ref", "decay_linear",
            "roc")
MAX_DEPTH = 5


# ---------------------------------------------------------------- 表达式树
def _rand_tree(rng: np.random.Generator, depth: int = 0) -> tuple:
    if depth >= MAX_DEPTH:
        return ("v", str(rng.choice(VARS)))
    r = rng.random()
    if r < 0.28 or depth == 0:
        return ("v", str(rng.choice(VARS)))
    if r < 0.36:
        return ("n", float(rng.choice((1.0, 0.5, 2.0))))
    if r < 0.58:
        return ("u", str(rng.choice(UNARY)), _rand_tree(rng, depth + 1))
    if r < 0.76:
        return ("w", str(rng.choice(WINDOWED)), _rand_tree(rng, depth + 1),
                int(rng.choice(WINDOWS)))
    if r < 0.88:
        return ("c", "ts_corr", _rand_tree(rng, depth + 1),
                _rand_tree(rng, depth + 1), int(rng.choice(WINDOWS)))
    return ("b", str(rng.choice(BINARY)), _rand_tree(rng, depth + 1),
            _rand_tree(rng, depth + 1))


def render(t: tuple) -> str:
    kind = t[0]
    if kind == "v":
        return t[1]
    if kind == "n":
        return f"{t[1]:g}"
    if kind == "u":
        return f"{t[1]}({render(t[2])})"
    if kind == "w":
        return f"{t[1]}({render(t[2])},{t[3]})"
    if kind == "c":
        return f"ts_corr({render(t[2])},{render(t[3])},{t[4]})"
    a, b = render(t[2]), render(t[3])
    if t[1] == "/":
        return f"({a})/(({b})+1e-9)"
    return f"({a}){t[1]}({b})"


def depth_of(t: tuple) -> int:
    if t[0] in ("v", "n"):
        return 1
    if t[0] == "u":
        return 1 + depth_of(t[2])
    if t[0] == "w":
        return 1 + depth_of(t[2])
    if t[0] == "c":
        return 1 + max(depth_of(t[2]), depth_of(t[3]))
    return 1 + max(depth_of(t[2]), depth_of(t[3]))


def _nodes(t: tuple) -> list[tuple]:
    out = [t]
    if t[0] == "u":
        out += _nodes(t[2])
    elif t[0] == "w":
        out += _nodes(t[2])
    elif t[0] == "c":
        out += _nodes(t[2]) + _nodes(t[3])
    elif t[0] == "b":
        out += _nodes(t[2]) + _nodes(t[3])
    return out


def mutate(t: tuple, rng: np.random.Generator) -> tuple:
    """随机挑一个子树替换成新随机树。"""
    nodes = _nodes(t)

    def walk(node: tuple, target: tuple) -> tuple:
        if node is target:
            return _rand_tree(rng, depth=max(0, MAX_DEPTH - 2))
        if node[0] == "u":
            return ("u", node[1], walk(node[2], target))
        if node[0] == "w":
            return ("w", node[1], walk(node[2], target), node[3])
        if node[0] == "c":
            return ("c", node[1], walk(node[2], target), walk(node[3], target),
                    node[4])
        if node[0] == "b":
            return ("b", node[1], walk(node[2], target), walk(node[3], target))
        return node

    return walk(t, nodes[int(rng.integers(0, len(nodes)))])


def crossover(a: tuple, b: tuple, rng: np.random.Generator) -> tuple:
    """把 a 的某个子树换成 b 的某个子树。"""
    an = _nodes(a)
    bn = _nodes(b)
    src = bn[int(rng.integers(0, len(bn)))]
    dst = an[int(rng.integers(0, len(an)))]

    def walk(node: tuple) -> tuple:
        if node is dst:
            return src
        if node[0] == "u":
            return ("u", node[1], walk(node[2]))
        if node[0] == "w":
            return ("w", node[1], walk(node[2]), node[3])
        if node[0] == "c":
            return ("c", node[1], walk(node[2]), walk(node[3]), node[4])
        if node[0] == "b":
            return ("b", node[1], walk(node[2]), walk(node[3]))
        return node

    return walk(a)


# ---------------------------------------------------------------- 适应度
def _ic_series(fac: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    fac, fwd = fac.align(fwd, join="inner")
    rows = []
    idx = []
    for d in fac.index:
        a, b = fac.loc[d], fwd.loc[d]
        m = a.notna() & b.notna()
        if int(m.sum()) < 10:
            continue
        rows.append(a[m].rank().corr(b[m].rank()))
        idx.append(d)
    return pd.Series(rows, index=pd.DatetimeIndex(idx)).dropna()


def _stats(ic: pd.Series) -> dict:
    if len(ic) < 10:
        return {}
    mean, std = float(ic.mean()), float(ic.std(ddof=1) or 0.0)
    ir = mean / std if std > 1e-12 else 0.0
    return {"ic": round(mean, 4), "icir": round(ir, 3),
            "t": round(ir * np.sqrt(len(ic)), 2), "n": len(ic),
            "win": round(float((ic > 0).mean()), 3)}


def evaluate(panel: dict, tree: tuple, fwd: pd.DataFrame,
             split: float = 0.7, fac_cache: dict | None = None) -> dict:
    if depth_of(tree) < 2:          # 裸 close/low 不算因子
        return {}
    expr = render(tree)
    fac = (fac_cache or {}).get(expr)
    if fac is None:
        fac = fm._factor_panel(panel, expr)
        if fac_cache is not None and not fac.empty:
            fac_cache[expr] = fac
    if fac.empty:
        return {}
    ic = _ic_series(fac, fwd)
    if len(ic) < 30:
        return {}
    n_tr = max(20, int(len(ic) * split))
    tr, te = ic.iloc[:n_tr], ic.iloc[n_tr:]
    st_tr, st_te = _stats(tr), _stats(te)
    if not st_tr or not st_te:
        return {}
    coverage = float(fac.notna().mean().mean())
    if coverage < 0.6:
        return {}
    same = (st_tr["ic"] > 0) == (st_te["ic"] > 0)
    oos_ok = same and abs(st_te["ic"]) >= 0.01
    depth = depth_of(tree)
    # 适应度:样本外通过的才给高分,再扣复杂度
    fit = (abs(st_tr["icir"]) + abs(st_te["icir"])) if oos_ok else 0.0
    fit -= 0.01 * depth
    return {"expr": expr, "depth": depth, "coverage": round(coverage, 3),
            "train_ic": st_tr["ic"], "train_icir": st_tr["icir"],
            "test_ic": st_te["ic"], "test_icir": st_te["icir"],
            "ic": st_tr["ic"], "icir": st_tr["icir"],
            "win": st_tr["win"], "fitness": round(fit, 4),
            "oos_ok": bool(oos_ok), "label": f"GP{depth}",
            "mutation": True}


def run(days: int = 250, max_stocks: int = 300, horizon: int = 5,
        market: str = "a", generations: int = 10, population: int = 40,
        max_corr: float = 0.7, time_budget: float = 300.0, seed: int = 11,
        progress=None, should_stop=None) -> dict:
    t0 = time.time()
    panel = fm._panel(days, max_stocks, market)
    if not panel:
        return {"ok": False, "error": "本地没有 K 线数据", "rows": []}
    fwd = fm._fwd_panel(panel, horizon)
    rng = np.random.default_rng(int(seed))
    cache: dict[str, dict] = {}
    fac_cache: dict[str, pd.DataFrame] = {}

    def ev(tree: tuple) -> dict:
        expr = render(tree)
        if expr in cache:
            return cache[expr]
        try:
            st = evaluate(panel, tree, fwd, fac_cache=fac_cache)
        except Exception:  # noqa: BLE001
            st = {}
        cache[expr] = st
        return st

    def usable(tree: tuple) -> bool:
        """能算出因子面板就行;打分可以低,但不能进不了种群。"""
        if depth_of(tree) < 2:          # 裸 close/low 这种不算因子
            return False
        expr = render(tree)
        try:
            fac = fac_cache.get(expr)
            if fac is None:
                fac = fm._factor_panel(panel, expr)
                if not fac.empty:
                    fac_cache[expr] = fac
            return not fac.empty
        except Exception:  # noqa: BLE001
            return False

    pop: list[tuple] = []
    tries = 0
    while len(pop) < int(population) and tries < int(population) * 30:
        tries += 1
        t = _rand_tree(rng, 1)
        if usable(t):
            pop.append(t)
    if not pop:
        return {"ok": False, "error": "随机表达式都算不出因子", "rows": []}

    best_rows: list[dict] = []
    history = []
    for gen in range(1, max(1, int(generations)) + 1):
        if should_stop and should_stop():
            break
        if time.time() - t0 > float(time_budget):
            break
        scored = sorted(((ev(t), t) for t in pop),
                        key=lambda x: float((x[0] or {}).get("fitness") or 0),
                        reverse=True)
        best_rows = [s for s, _t in scored if s][:10]
        history.append({"generation": gen, "evaluated": len(cache),
                        "best_fitness": best_rows[0]["fitness"] if best_rows else 0,
                        "best_icir": best_rows[0]["test_icir"] if best_rows else 0})
        if progress:
            try:
                progress({"generation": gen, "total": generations,
                          "evaluated": len(cache),
                          "best": best_rows[:5],
                          "rows": best_rows})
            except Exception:  # noqa: BLE001
                pass

        # 选择:精英 + 锦标赛
        elite = [t for _s, t in scored[:5]]
        nxt = list(elite)
        while len(nxt) < int(population):
            def pick():
                cand = [scored[int(rng.integers(0, len(scored)))] for _ in range(3)]
                return max(cand, key=lambda x: float((x[0] or {}).get("fitness") or 0))[1]

            a, b = pick(), pick()
            child = crossover(a, b, rng) if rng.random() < 0.6 else a
            if rng.random() < 0.5:
                child = mutate(child, rng)
            if depth_of(child) <= MAX_DEPTH and usable(child):
                nxt.append(child)
        pop = nxt[:int(population)]

    rows = [r for r in cache.values() if r]
    rows.sort(key=lambda r: float(r.get("fitness") or 0), reverse=True)
    rows = rows[:60]
    # 相关性去重 + 样本外过滤(面板已缓存,不再重复求值)
    selected: list[dict] = []
    for r in rows:
        if not r.get("oos_ok") or len(selected) >= 30:
            continue
        f = fac_cache.get(r["expr"])
        if f is None or f.empty:
            continue
        f = f.rank(axis=1)
        dup = False
        for s in selected:
            g = fac_cache.get(s["expr"])
            if g is None or g.empty:
                continue
            g = g.rank(axis=1)
            f2, g2 = f.align(g, join="inner")
            try:
                c = f2.corrwith(g2, axis=1).mean()
            except Exception:  # noqa: BLE001
                c = 1.0
            if abs(float(c)) >= max_corr:
                dup = True
                break
        if not dup:
            selected.append(r)
    for r in rows:
        r["selected"] = any(s["expr"] == r["expr"] for s in selected)
    return {"ok": True, "rows": rows, "selected": selected,
            "horizon": horizon, "n_candidates": len(rows),
            "n_codes": len(panel.get("codes") or panel), "days": days,
            "market": market, "rounds": len(history),
            "rounds_history": history,
            "cost_seconds": round(time.time() - t0, 1),
            "mode": "gp", "ts": time.strftime("%H:%M:%S")}
