# -*- coding: utf-8 -*-
"""稳健参数评估:把「样本内冠军」换成「折外稳健分」。

来源与取舍
----------
蒸馏自 shy3130/tick-stock-panel(MIT)的 `backend/app/backtest/robust_optimizer.py`。
原版是耦合了他们回测引擎的一体化服务(约 500 行,含 FastAPI worker、矩阵缓存、
polars 超集视图)。这里只取**统计纪律**那一层,做成与引擎无关的通用件:
调用方给一个 `evaluate(params, window) -> float`,剩下的都归这里管。

为什么值得偷
------------
本项目刚用实测证明:45 因子 + LightGBM 在干净数据上跑不赢等权,而**样本内**看起来
是有 alpha 的。这正是「样本内冠军」这个选参方式的失败模式 —— 从 N 组参数里挑
最好的那组,挑到的往往是噪声的最优实现。原版的核心转变是:

    排名对象从「样本内得分」改成「折外稳健分」

    R(p) = 0.6 × median(折外₁…折外_K) + 0.4 × worst(折外₁…折外_K)

中位数用来抵抗单段行情的运气,最差段用来惩罚「只在某一段好用」。一组参数必须在
**多个互不重叠**的验证段上都表现好,才有资格当冠军。

四个判据(全部零额外回测,只用已算出的得分矩阵)
------------------------------------------------
1. `robust_score`  稳健分组成:median / worst / spread / score
2. `champion_convergence`  各段冠军是否收敛到同一组参数
   unanimous 全部一致 / majority 过半一致 / scattered 散落
3. `neighborhood_plateau`  冠军在网格上是不是高原
   邻居(沿任一参数走一步)得分衰减 ≤ tol 的占比高 → plateau;全退化 → peak(孤峰)
4. 阶段二样本内对照  Top-N 用全窗再跑一次,degradation = IS − 稳健分 = 样本内乐观偏差

四条合起来的读法:
* 稳健分高 + 收敛 unanimous + 高原 plateau → 参数可信
* 稳健分高但 scattered / peak → 分数是运气,别信
* 稳健分低 + 样本内高(degradation 大) → 教科书式过拟合,直接放弃

与原版的差异(有意为之)
--------------------
* 不依赖 pandas / 他们的引擎,只用标准库时也能跑 —— 方便接到任何评估器上
* `evaluate` 允许抛异常,单段异常只让该参数组出局,不中断整个网格
* 保留原版「未达标组 / 错误组透明垫底」的做法,不静默剔除 —— 免得看不出哪里没跑成
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence

# 验证段数:2 段已够算中位/最差,上限 6 防止过度切碎(每段统计噪声放大)。
K_MIN = 2
K_MAX = 6
# 每段最短自然日数,短于此统计意义不足。
MIN_SEGMENT_DAYS = 14
# 稳健分权重:中位数抗单段运气,最差段罚「只在某段好用」。
W_MEDIAN = 0.6
W_WORST = 0.4


# --------------------------------------------------------------------------
# 1. 切窗
# --------------------------------------------------------------------------

def split_windows(start: date, end: date, k: int) -> list[tuple[date, date]]:
    """把 [start, end] 等分成 k 个**互不重叠**的验证段。

    余数摊给前面的段(原版同口径)。段内交易日密度由评估器自己处理,
    这里只管日历切分和合法性校验。
    """
    if not (K_MIN <= k <= K_MAX):
        raise ValueError(f"验证段数必须在 {K_MIN}-{K_MAX} 之间(当前 {k})")
    span = (end - start).days + 1
    if span < k * MIN_SEGMENT_DAYS:
        raise ValueError(
            f"区间太短:{span} 自然日切不出 {k} 个 ≥{MIN_SEGMENT_DAYS} 天的验证段;"
            f"请扩大区间或减少段数")
    out: list[tuple[date, date]] = []
    base, rem = divmod(span, k)
    cursor = start
    for i in range(k):
        length = base + (1 if i < rem else 0)
        seg_end = cursor + timedelta(days=length - 1)
        out.append((cursor, seg_end))
        cursor = seg_end + timedelta(days=1)
    return out


# --------------------------------------------------------------------------
# 2. 稳健分
# --------------------------------------------------------------------------

def _finite(v) -> bool:
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


def robust_score(values: Sequence[float | None]) -> dict:
    """K 段得分 → 稳健分。

    R = 0.6×median + 0.4×worst。
    `spread`(max−min)本身就是过拟合警报:段间离散大 = 不稳定。
    None / NaN / inf 一律按 −inf 计入 worst 与 median(诚实降级,不当成 0 蒙混)。
    """
    finite = [float(v) for v in values if _finite(v)]
    if not finite:
        return {"median": None, "worst": None, "spread": None,
                "score": float("-inf")}
    # worst 要在「含无效值」的全体上取,无效值视为 −inf
    invalid = len(values) - len(finite)
    worst = float("-inf") if invalid else min(finite)
    s = sorted(finite)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {"median": median, "worst": worst, "spread": s[-1] - s[0],
            "score": W_MEDIAN * median + W_WORST * worst}


# --------------------------------------------------------------------------
# 3. 冠军收敛度
# --------------------------------------------------------------------------

def _key(params: dict) -> str:
    return "|".join(f"{k}={params[k]}" for k in sorted(params))


def _norm(v: float, direction: str) -> float:
    """统一成「越大越好」。direction='min' 表示原始目标越小越好(如回撤)。"""
    return -v if direction == "min" else v


def champion_convergence(rows: Iterable[dict], k: int,
                         direction: str = "max") -> dict:
    """各验证段的冠军(param 的 argmax)是否收敛到同一组参数。

    全部段冠军同组 → unanimous,参数可信;
    过半同组 → majority;
    散落 → scattered,说明策略本身参数不稳定,任何「最优」都别信。
    """
    champions: list[dict | None] = [None] * k
    for row in rows:
        if row.get("error") or row.get("disqualified"):
            continue
        for i, v in enumerate(row.get("oos_values") or []):
            if i >= k or not _finite(v):
                continue
            cur = champions[i]
            if cur is None or _norm(v, direction) > _norm(cur["_v"], direction):
                champions[i] = {"params": row.get("params") or {}, "_v": v}
    keys = [_key(c["params"]) for c in champions if c is not None]
    if not keys:
        return {"label": "no_data", "champions": [], "max_count": 0, "k": k}
    max_count = max(keys.count(x) for x in set(keys))
    if max_count == len(keys) == k:
        label = "unanimous"
    elif max_count > k / 2:
        label = "majority"
    else:
        label = "scattered"
    return {
        "label": label,
        "champions": [
            {"segment": i, "params": c["params"], "objective_raw": c["_v"]}
            for i, c in enumerate(champions) if c is not None
        ],
        "max_count": max_count,
        "k": k,
    }


# --------------------------------------------------------------------------
# 4. 高原判据
# --------------------------------------------------------------------------

def neighborhood_plateau(rows: Sequence[dict], best_row: dict | None,
                         tol_pct: float = 0.25,
                         grid: dict[str, Sequence] | None = None) -> dict | None:
    """冠军在参数网格上是否落在「高原」上。

    邻居 = 恰好一个参数沿网格移动一步的组合。得分衰减 ≤ tol 的邻居占比高
    → plateau(参数选择不敏感,可信);全部大幅衰减 → peak(孤峰,过拟合警报)。

    `grid` 必须传**原始参数网格**。早期版本从结果 rows 里反推候选值顺序,但
    rows 到这里已经按得分重排过,反推出来的顺序是 `[50,30,70,10,90]` 这种,
    于是内点 50 被当成边界、邻居只找到一侧 —— 地形判断全错。不能从结果反推输入。

    tol 的尺度取「冠军得分绝对值」与「全体合格分极差」中的较大者,这样对负值
    目标(如回撤、负数收益)同样成立 —— 直接按绝对值取比例会在 0 附近失真。
    """
    if not best_row or best_row.get("error") or best_row.get("disqualified"):
        return None
    best_params = best_row.get("params") or {}
    best_score = best_row.get("score")
    if not best_params or not _finite(best_score) or best_score == float("-inf"):
        return None

    # 候选值顺序:优先用调用方给的原始网格(声明顺序),否则退回首次出现序
    order: dict[str, list] = {}
    if grid:
        for p, vals in grid.items():
            order[p] = list(vals)
    if not order:
        for r in rows:
            for p, v in (r.get("params") or {}).items():
                vals = order.setdefault(p, [])
                if v not in vals:
                    vals.append(v)

    by_key = {_key(r.get("params") or {}): r for r in rows}
    neighbors: list[dict] = []
    # 冠军落在网格边界时(如 hold 取最大值),只有单侧邻居,地形信息天然不足。
    # 这种情况不能断言 peak —— 那是把「没验证过」说成「验证失败」。
    # 注意:`i==0 or i==len-1` 在只有 2 档时两边同时成立,会把所有冠军都算成边界。
    # 所以要求该参数至少有 3 档才判边界,2 档时它就是「全域只有两侧」,不算贴边。
    edge_params: list[str] = []
    for p, vals in order.items():
        if p not in best_params or len(vals) < 2:
            continue
        i = vals.index(best_params[p])
        if len(vals) > 2 and (i == 0 or i == len(vals) - 1):
            edge_params.append(p)
        for j in (i - 1, i + 1):
            if not (0 <= j < len(vals)):
                continue
            cand = dict(best_params)
            cand[p] = vals[j]
            r = by_key.get(_key(cand))
            if r is None:
                continue
            entry = {"params": cand, "score": r.get("score"),
                     "disqualified": bool(r.get("disqualified")),
                     "error": r.get("error")}
            sc = r.get("score")
            if (_finite(sc) and sc != float("-inf")
                    and not entry["disqualified"] and not entry["error"]):
                entry["decay"] = best_score - sc
            neighbors.append(entry)

    valid = [n for n in neighbors if "decay" in n]
    if not valid:
        return {"label": "isolated", "ratio": None, "neighbors": neighbors,
                "tolerance": None, "n_valid": 0, "edge_params": edge_params}

    finite_scores = [r.get("score") for r in rows
                     if _finite(r.get("score")) and r.get("score") != float("-inf")]
    # tol 的尺度:优先用「全体合格分极差」(相对衰减才有意义),极差退化时才用
    # 冠军绝对值兜底。原版直接用 max(|best|, range),在高分目标下(如 10 分制)
    # 会让容差按分值绝对值算、相对过紧 —— 平滑的峰也会被判成孤峰。
    rng = (max(finite_scores) - min(finite_scores)) if finite_scores else 0.0
    scale = rng if rng > 0 else abs(best_score)
    tol = tol_pct * scale
    within = sum(1 for n in valid if n["decay"] <= tol)
    ratio = within / len(valid)
    if ratio >= 0.6:
        label = "plateau"
    elif ratio > 0:
        label = "slope"
    elif len(valid) < 2 or edge_params:
        # 有效邻居少于 2 个、或冠军贴网格边界(只有单侧)—— 这是「没验证过」,
        # 不是「验证失败」,不能当 peak 报出去。
        # 注意门槛是 2 不是 3:两档参数就只可能有 2 个邻居(左右各一),
        # 那已经是完整证据了;按 3 要求会把所有两档网格都判成证据不足。
        label = "insufficient"
    else:
        label = "peak"
    return {"label": label, "ratio": round(ratio, 4), "neighbors": neighbors,
            "tolerance": round(tol, 6), "n_valid": len(valid),
            "edge_params": edge_params}


# --------------------------------------------------------------------------
# 5. 网格展开 + 主驱动
# --------------------------------------------------------------------------

def expand_grid(grid: dict[str, Sequence]) -> list[dict]:
    """笛卡尔积展开参数网格。保持声明顺序,便于复现。"""
    if not grid:
        return [{}]
    keys = list(grid)
    out: list[dict] = []

    def _rec(i: int, cur: dict) -> None:
        if i == len(keys):
            out.append(dict(cur))
            return
        for v in grid[keys[i]]:
            cur[keys[i]] = v
            _rec(i + 1, cur)
        cur.pop(keys[i], None)

    _rec(0, {})
    return out


@dataclass
class RobustResult:
    """一次稳健评估的完整结果。字段名尽量贴近原版,方便对照。"""
    objective: str
    direction: str
    k_segments: int
    windows: list[tuple[str, str]]
    n_combinations: int
    rows: list[dict] = field(default_factory=list)
    best_params: dict | None = None
    best_score: float | None = None
    convergence: dict = field(default_factory=dict)
    plateau: dict | None = None
    stage2: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def verdict(self) -> str:
        """把四个判据统计成一句话结论。"""
        if not self.rows or self.best_params is None:
            return "无可用结果"
        conv = (self.convergence or {}).get("label")
        plat = (self.plateau or {}).get("label")
        deg = self.stage2.get("degradation_max")
        bits: list[str] = []
        if conv == "unanimous" and plat == "plateau":
            bits.append("参数可信(各段冠军一致 + 落在高原)")
        elif conv == "scattered":
            bits.append("参数不可信(各段冠军散落,最优是运气)")
        elif plat == "peak":
            bits.append("参数不可信(孤峰,邻居全塌)")
        elif plat == "insufficient":
            bits.append("参数存疑(冠军在网格边界,邻居太少不足以判定)")
        elif plat == "isolated":
            bits.append("参数不可信(网格太窄,没有邻居可验证)")
        else:
            bits.append(f"参数待观察(收敛 {conv} / 地形 {plat})")
        if deg is not None and deg > 0:
            bits.append(f"样本内比折外高 {deg:.4f},存在乐观偏差")
        return ";".join(bits)

    def to_dict(self) -> dict:
        return {
            "objective": self.objective, "direction": self.direction,
            "k_segments": self.k_segments, "windows": self.windows,
            "n_combinations": self.n_combinations,
            "best_params": self.best_params, "best_score": self.best_score,
            "convergence": self.convergence, "plateau": self.plateau,
            "stage2": self.stage2, "verdict": self.verdict,
            "elapsed_ms": self.elapsed_ms, "rows": self.rows,
        }


def robust_evaluate(
    grid: dict[str, Sequence],
    evaluate: Callable[[dict, tuple[date, date]], float],
    start: date,
    end: date,
    k: int = 4,
    direction: str = "max",
    objective: str = "score",
    min_trades_total: int = 0,
    trades_of: Callable[[dict, tuple[date, date]], int] | None = None,
    stage2_top_n: int = 0,
    evaluate_full: Callable[[dict], float] | None = None,
    progress: Callable[[int, int, float | None], None] | None = None,
) -> RobustResult:
    """在 k 个互不重叠的验证段上评估整个参数网格,按稳健分排名。

    参数
    ----
    evaluate(params, window) -> float
        给定一组参数和一个验证段,返回该段的得分(越大越好;direction='min' 时越小越好)。
    trades_of(params, window) -> int
        可选,返回该段的交易数,用于 `min_trades_total` 门槛(合计不足即除名)。
    evaluate_full(params) -> float
        可选,阶段二用:同一组参数在整个 [start,end] 上跑一次,与稳健分对比得出
        「样本内乐观偏差」。`stage2_top_n` 控制只看前几名。

    单段异常只让该参数组出局(记 error),不中断整个网格 —— 一个参数的边界情况
    不该毁掉整轮搜索。
    """
    windows = split_windows(start, end, k)
    combos = expand_grid(grid)
    t0 = time.perf_counter()
    rows: list[dict] = []
    best_score: float | None = None

    for idx, combo in enumerate(combos):
        oos: list[float | None] = []
        seg_stats: list[dict] = []
        trades_total = 0
        error: str | None = None
        for (ws, we) in windows:
            try:
                v = evaluate(combo, (ws, we))
                if v is not None and not _finite(v):
                    v = None
                n = int(trades_of(combo, (ws, we))) if trades_of else 0
            except Exception as e:  # noqa: BLE001
                error = repr(e)
                break
            oos.append(v)
            trades_total += n
            seg_stats.append({"start": ws.isoformat(), "end": we.isoformat(),
                              "objective_raw": v, "n_trades": n})

        # 方向统一在这里做一次:原始目标可能是「越小越好」(回撤、波动),
        # 稳健分与排名一律在「越大越好」的归一空间里算。
        # 早期版本只在收敛度里转了方向,选冠军时忘了转 —— 冠军会选反。
        oos_norm = [None if v is None else _norm(float(v), direction) for v in oos]

        if error is not None:
            rows.append({"params": combo, "error": error, "oos_values": oos_norm,
                         "segments": seg_stats, "trades_total": trades_total,
                         "disqualified": False})
        else:
            sc = robust_score(oos_norm)
            ok = trades_total >= int(min_trades_total)
            rows.append({
                "params": combo, "oos_values": oos_norm, "segments": seg_stats,
                "trades_total": trades_total, "disqualified": not ok,
                "disqualify_reason": None if ok else (
                    f"K 段合计交易数 {trades_total} < {min_trades_total}"),
                **sc,
            })
            if ok and (best_score is None or sc["score"] > best_score):
                best_score = sc["score"]

        if progress:
            progress(idx + 1, len(combos), best_score)

    # 排序:合格 → 未达标 → 错误,每档内按稳健分降序。
    # 透明垫底,不静默剔除(否则看不出哪里没跑成)。
    # 注意:每档必须真的按分数排 —— 只做拼接会让冠军选错
    # (实测踩过:冠军 −1.18 而旁边就有 −0.16 的组)。
    def _tier(r: dict) -> int:
        return 2 if r.get("error") else (1 if r.get("disqualified") else 0)

    def _score(r: dict) -> float:
        return float(r.get("score") or float("-inf"))

    ordered = (
        sorted([r for r in rows if _tier(r) == 0], key=_score, reverse=True)
        + sorted([r for r in rows if _tier(r) == 1], key=_score, reverse=True)
        + [r for r in rows if _tier(r) == 2]
    )
    for rank, r in enumerate(ordered, start=1):
        r["rank"] = rank

    best_row = next((r for r in ordered
                     if not r.get("error") and not r.get("disqualified")), None)
    # oos_values 此时已是归一空间(越大越好),收敛度不能再转一次方向
    convergence = champion_convergence(ordered, k, direction="max")
    plateau = neighborhood_plateau(ordered, best_row, grid=grid)

    # 阶段二:Top-N 全窗对照,算样本内乐观偏差
    stage2: dict = {"top_n": int(stage2_top_n), "evaluated": 0,
                    "degradation_max": None}
    if stage2_top_n > 0 and evaluate_full is not None:
        degs: list[float] = []
        for row in [r for r in ordered
                    if not r.get("error") and not r.get("disqualified")][:int(stage2_top_n)]:
            try:
                is_score = evaluate_full(row["params"])
            except Exception as e:  # noqa: BLE001
                row["is_full"] = {"error": repr(e)}
                continue
            if not _finite(is_score):
                row["is_full"] = {"error": "全窗得分无效"}
                continue
            # 与 oos_values 一致,进同一个归一空间(越大越好)再比较
            norm = _norm(float(is_score), direction)
            row["is_full"] = {"objective_raw": is_score, "score_norm": norm}
            if _finite(row.get("score")):
                deg = norm - float(row["score"])
                row["degradation"] = deg
                row["degradation_pct"] = (
                    deg / abs(norm) if norm != 0 else None)
                degs.append(deg)
            stage2["evaluated"] += 1
        if degs:
            stage2["degradation_max"] = max(degs)

    return RobustResult(
        objective=objective, direction=direction, k_segments=k,
        windows=[(ws.isoformat(), we.isoformat()) for ws, we in windows],
        n_combinations=len(combos), rows=ordered,
        best_params=(best_row["params"] if best_row else None),
        best_score=(best_row["score"] if best_row else None),
        convergence=convergence, plateau=plateau, stage2=stage2,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )
