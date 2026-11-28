# -*- coding: utf-8 -*-
"""选股次日验证必须接进每日维护。

背景(2026-11-28):
  pick_verify 表一直是 0 条。查下来 verify_next_day 只被
  云端任务(gray-service)、界面按钮、自检脚本调用 ——
  本地每日维护从来不跑它,所以涨幅预选没有验证数据,成绩单永远空的。

  补上之后首次跑:11-24 的 105 只里只有 20 只跑赢成本(15.3%)。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "tools/daily_maintain.py").read_text(encoding="utf-8")


def test_step_exists() -> None:
    assert "def step_pick_verify(" in SRC


def test_step_registered_in_main() -> None:
    """必须真的在 main 里被调,不能只是个函数。"""
    assert 'run("pick_verify", step_pick_verify)' in SRC, (
        "step_pick_verify 没接进 main —— 写了也不会跑")


def test_skip_flag_exists() -> None:
    """要有跳过开关,免得调试时被迫跑全量。"""
    assert "--skip-pick-verify" in SRC
    assert "args.skip_pick_verify" in SRC


def test_uses_configured_account() -> None:
    """账户从配置读,不能写死。"""
    i = SRC.find("def step_pick_verify(")
    seg = SRC[i:i + 900]
    assert "autotrade" in seg and "account" in seg, (
        "没从配置读账户,写死的话换个账户就验证错对象")
