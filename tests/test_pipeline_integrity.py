# -*- coding: utf-8 -*-
"""管道完整性:用「偷看未来」的因子验证审计流程本身没坏。

30 个真实因子全部 rejected 之后必须验证一件事:是因子不行,还是工具坏了。
做法是喂一个必然有效的因子(未来收益本身),看 audit_scores 能不能给出高 t。

如果这个测试挂了,说明 audit_scores / _fwd_panel 的对齐、方向、top_pct
某一处出了问题 —— 那前面所有结论都要重算。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.engine import factor_analysis as fa
from quantlab.engine import factor_mine as fm


def _panel(n_days: int = 200, n_codes: int = 60, seed: int = 7) -> dict:
    """造一个收盘价宽表:每只票随机游走,彼此独立。"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.02, size=(n_days, n_codes))
    px = 10.0 * np.exp(np.cumsum(rets, axis=0))
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    cols = [f"{600000 + i:06d}" for i in range(n_codes)]
    close = pd.DataFrame(px, index=idx, columns=cols)
    vol = pd.DataFrame(rng.uniform(1e5, 5e5, size=(n_days, n_codes)),
                       index=idx, columns=cols)
    return {"close": close, "open": close, "high": close * 1.01,
            "low": close * 0.99, "volume": vol, "amount": vol * close}


def test_lookahead_factor_gets_high_t():
    """因子 = 未来 5 日收益。审计必须给出高正 t。"""
    panel = _panel()
    frames = fm._frames(panel)
    fwd = fm._fwd_panel(panel, 5)
    rep = fa.audit_scores(frames, fwd, horizon=5)
    raw = rep.get("raw") or {}
    assert raw.get("excess_mean") > 0.05, f"超额太低:{raw.get('excess_mean')}"
    assert (raw.get("excess_t") or 0) > 5, f"t 太低:{raw.get('excess_t')}"


def test_reversed_lookahead_gets_negative_t():
    panel = _panel()
    frames = fm._frames(panel)
    fwd = fm._fwd_panel(panel, 5)
    rep = fa.audit_scores(frames, -fwd, horizon=5)
    raw = rep.get("raw") or {}
    assert (raw.get("excess_t") or 0) < -5, f"反向 t 应该很负:{raw.get('excess_t')}"


def test_random_factor_is_near_zero():
    panel = _panel(seed=11)
    frames = fm._frames(panel)
    close = panel["close"]
    rng = np.random.default_rng(99)
    scores = pd.DataFrame(rng.normal(size=close.shape),
                          index=close.index, columns=close.columns)
    rep = fa.audit_scores(frames, scores, horizon=5)
    raw = rep.get("raw") or {}
    assert abs(raw.get("excess_t") or 0) < 3, f"随机因子 t 不该这么大:{raw.get('excess_t')}"


def test_factor_panel_alignment():
    """因子面板的 index/columns 必须和收盘价一致,否则后面全错位。"""
    panel = _panel(seed=3)
    fac = fm._factor_panel(panel, "roc(close,5)")
    close = panel["close"]
    assert list(fac.index) == list(close.index)
    assert list(fac.columns) == list(close.columns)
