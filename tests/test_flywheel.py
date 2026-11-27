# -*- coding: utf-8 -*-
"""模拟盘 / 实盘分组的回归测试。

背景:归因原来用 account=None 把所有账户混算,模拟盘的大量试错样本
淹没实盘信号。分组判定的边界必须锁住,别让噪音账户混进实盘。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantlab.engine import flywheel  # noqa: E402


def test_kind_of_unknown_defaults_to_sim() -> None:
    """没配、不在模拟表、没有成交记录 → 按模拟盘处理。

    只有 sl_decisions 没有 orders 的是测试批次(「量化」账户就是这样:
    11-13 一天 134 条 decision,orders 0 条),不能当实盘证据。
    """
    assert flywheel.kind_of("完全不存在的账户名") == "sim"


def test_kind_of_respects_explicit() -> None:
    kinds = {"测试盘": "sim", "真钱": "live"}
    assert flywheel.kind_of("测试盘", kinds) == "sim"
    assert flywheel.kind_of("真钱", kinds) == "live"


def test_kind_of_invalid_value_falls_back_to_sim() -> None:
    """配错值(不是 sim/live)时按模拟盘处理,别当成实盘。"""
    assert flywheel.kind_of("某账户", {"某账户": "production"}) == "sim"


def test_account_kinds_includes_sim_accounts() -> None:
    """sim_accounts 表里的账户必须判成 sim。"""
    kinds = flywheel.account_kinds()
    for name in ("默认账户", "自检账户", "_pytest"):
        if name in kinds:
            assert kinds[name] == "sim", f"{name} 应该是 sim"


def test_noise_threshold_exists() -> None:
    """实盘账户少于 NOISE_MIN_N 条要过滤 —— 联调残留会错位结论。"""
    assert flywheel.NOISE_MIN_N >= 2


def test_render_marks_kind_source() -> None:
    """报告要说明账户类型是怎么来的,别让人以为是人工确认的。"""
    rep = flywheel.build(days=90)
    text = flywheel.render(rep)
    assert "模拟/实盘反哺" in text
    assert "账户类型" in text


def test_summarize_reports_caliber() -> None:
    """summarize 必须报告口径构成 —— 快照和真实平仓混算会失真。"""
    from quantlab.engine import strategy_review as sr

    items = [
        {"close_outcome": 0.01, "ts": 1.0},
        {"outcome": 0.02, "ts": 2.0},
        {"outcome": 0.03, "ts": 3.0},
    ]
    s = sr.summarize(items)
    assert s["n"] == 3
    assert s["n_close"] == 1
    assert s["n_snapshot"] == 2


def test_caliber_thin_when_snapshot_dominates() -> None:
    """快照占多数时,正的净均值不能给「候选」。"""
    src = (ROOT / "quantlab/engine/flywheel.py").read_text(encoding="utf-8")
    assert "caliber_thin" in src
    assert "nc < ns" in src, "缺少「真实平仓少于快照」的判断"


def test_cost_rate_from_config() -> None:
    """归因成本必须从配置算,不能硬编码。

    2026-11-27 发现 COST 硬编码 0.15%,实际配置双边约 0.269%
    (佣金 0.0085% + 过户费 0.001% + 滑点 0.1% 各两次,卖出再加印花税 0.05%)。
    低估成本会让归因偏乐观 —— rule:均线多头 就是被这个从 +0.07% 算成
    看起来「唯一为正」,用真实成本是 -0.05%/笔。
    """
    from quantlab.engine import strategy_review as sr

    rate = sr.cost_rate()
    assert 0.001 < rate < 0.01, f"成本率 {rate} 不合理"
    # 必须比旧的硬编码 0.15% 高 —— 否则说明没读到配置
    assert rate > 0.0015, f"成本率 {rate} 仍低于硬编码兜底,配置没生效"


def test_summarize_uses_cost_rate() -> None:
    """summarize 的 net_mean 必须用 cost_rate(),不是硬编码 COST。"""
    src = (ROOT / "quantlab/engine/strategy_review.py").read_text(
        encoding="utf-8")
    assert "mean - cost_rate()" in src, "net_mean 还在用硬编码 COST"
