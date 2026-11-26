# -*- coding: utf-8 -*-
"""配置守护改成直接读写 config.json,不依赖 cfg() 单例。

为什么:cfg() 是单例,set+save 后同一进程内 get 仍可能拿到旧值
(2026-11-26 实测 step_config 连跑两次都显示 0.0)。
直接操作文件最可靠。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"

# (配置键, 期望值, 为什么)
ENFORCE: list[tuple[str, object, str]] = [
    ("selflearn.target_hysteresis_pct", 0.15,
     "碎单连买:603389 两小时被买 36 次全亏,差额不到目标 15% 就不补"),
    ("autotrade.adaptive_enabled", True,
     "震荡市仓位保护,关掉等于裸奔"),
    ("autotrade.max_buys_per_code_per_day", 3,
     "单票当日买入上限,总笔数限制拦不住集中"),
    ("risk.max_daily_orders", 30,
     "全局限次,防止一天几十笔碎单"),
]


def _load() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _get(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    data = _load()
    if not data:
        print("config.json 读不到或为空")
        return 1

    bad = []
    for key, want, why in ENFORCE:
        got = _get(data, key)
        if got == want:
            continue
        bad.append((key, got, want, why))

    if not bad:
        print(f"配置守护: {len(ENFORCE)} 项全部正确")
        return 0

    print(f"配置守护: {len(bad)} 项需要修正")
    for key, got, want, why in bad:
        print(f"  {key}: {got!r} → {want!r}")
        print(f"    原因: {why}")

    if args.check:
        return 1

    for key, _got, want, _why in bad:
        _set(data, key, want)
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print("已写入 config.json")

    # 重新读文件验证(不用 cfg() 单例)
    data2 = _load()
    left = [k for k, w, _ in ENFORCE if _get(data2, k) != w]
    print(f"重新读文件验证: {'全部正确' if not left else f'仍有 {left} 不对'}")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
