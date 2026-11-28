# -*- coding: utf-8 -*-
"""收盘快照回填不该按策略过滤。

背景(2026-11-28):
  close_outcome 只有 144/467(31%)。查下来回填 SQL 带了 strategy=? 条件,
  策略一换(multifactor → wfmulti)旧策略的决策就永远不回填。

  但 close_outcome 的语义是「决策价 vs 当天收盘价」,和历史策略无关 ——
  换个策略不影响昨天那笔决策走得怎么样。所以不该按策略过滤。

  账户仍然要限:不同账户是不同资金,不能混。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "quantlab/engine/selflearn.py").read_text(encoding="utf-8")


def _sql_around(marker: str, span: int = 260) -> str:
    """取 marker 附近的 SQL 文本,并剥掉注释行。

    注释里会提到 strategy=? 这种词,不能算进断言。
    """
    i = SRC.find(marker)
    assert i > 0, f"找不到 {marker}"
    seg = SRC[max(0, i - span):i + 80]
    lines = []
    for ln in seg.split("\n"):
        s = ln.strip()
        if s.startswith("#"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def test_close_outcome_fill_not_filtered_by_strategy() -> None:
    """close_outcome 的回填 SQL 不该出现 strategy=?。"""
    sql = _sql_around("AND close_outcome IS NULL")
    assert "strategy=?" not in sql, (
        "close_outcome 回填还在按 strategy 过滤 —— 策略一换旧决策就永不回填")


def test_close_outcome_still_filtered_by_account() -> None:
    """账户必须限 —— 不同账户是不同资金,不能混。"""
    sql = _sql_around("AND close_outcome IS NULL")
    assert "account=?" in sql, "close_outcome 回填没有按 account 过滤"


def test_outcome_fill_still_filtered_by_strategy() -> None:
    """outcome(20 小时口径)是调参口径,必须按策略分开。"""
    sql = _sql_around("AND outcome IS NULL", span=200)
    assert re.search(r"strategy=\?", sql), (
        "outcome 回填丢了 strategy 过滤 —— 不同策略的调参样本会混在一起")
