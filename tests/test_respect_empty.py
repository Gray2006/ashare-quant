# -*- coding: utf-8 -*-
"""策略 select 返回空时,实盘必须空仓,不能 fallback 硬买。

背景(2026-11-27 发现):
  2026-11-16 起 wfmulti 的 select() 连续 9 个交易日返回 0 只 ——
  策略在说「现在没有可靠信号」。
  回测尊重它,空仓躲过跌幅;实盘 autotrader 却落到 fallback
  按因子分数硬买 20 只,那几天买的全是这个路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantlab.engine.autotrader import AutoTrader  # noqa: E402


def test_respect_empty_method_exists() -> None:
    assert hasattr(AutoTrader, "_respect_empty")


def test_respect_empty_defaults_true() -> None:
    """默认必须尊重空仓 —— 和回测口径一致。"""
    class _Dummy:
        _respect_empty = AutoTrader._respect_empty

    assert _Dummy()._respect_empty() is True


def test_empty_select_does_not_fall_through() -> None:
    """select 正常返回全 0 时,不能落到「分数回退」分支。"""
    src = (ROOT / "quantlab/engine/autotrader.py").read_text(encoding="utf-8")
    i_select = src.find("sel = st.select(scores)")
    i_respect = src.find("if self._respect_empty():")
    i_fallback = src.find("# 回退:直接取分数最高的")
    assert i_select > 0, "找不到 select 调用"
    assert i_respect > i_select, "尊重空仓的分支必须在 select 之后"
    assert i_fallback > i_respect, (
        "分数回退必须排在「尊重空仓」之后 —— 否则空仓时还是会硬买")


def test_fallback_only_on_exception_or_optout() -> None:
    """fallback 只该在策略报错、或显式关掉 respect_empty 时走到。"""
    src = (ROOT / "quantlab/engine/autotrader.py").read_text(encoding="utf-8")
    i_exc = src.find("sel = None          # 策略报错")
    assert i_exc > 0, "策略报错时置 sel=None 的逻辑不见了"
    i_respect = src.find("if self._respect_empty():")
    assert i_respect > i_exc, "报错处理应在尊重空仓之前"


def test_fallback_marks_itself() -> None:
    """真走到 fallback 时,note 里要写明「分数回退」,别让人误以为是策略选的。"""
    src = (ROOT / "quantlab/engine/autotrader.py").read_text(encoding="utf-8")
    assert "分数回退" in src
    assert "select 空仓" in src
