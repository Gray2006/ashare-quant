# -*- coding: utf-8 -*-
"""扫「配置默认值硬编码」的同类问题。

模式:cfg.get("a.b", 硬编码值) —— 硬编码值和 config.DEFAULTS 不一致时,
DEFAULTS 改了也白改。

用法:python tools/check_config_defaults.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.config import DEFAULTS  # noqa: E402

PAT = re.compile(
    r'cfg\.get\(\s*["\']([\w.]+)["\']\s*,\s*([^)]+?)\s*\)')

# 白名单:这些 key 的「0」有特殊语义,不算不一致
#   max_drawdown_pct 0 = 用 dd_base 兜底(见 autopilot._dd_base)
ALLOW = {
    "autotrade.max_drawdown_pct",
}


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def main() -> int:
    flat = _flatten(DEFAULTS)
    issues = []
    for py in (ROOT / "quantlab").rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in PAT.finditer(text):
            key, raw = m.group(1), m.group(2).strip()
            if key not in flat or key in ALLOW:
                continue
            want = flat[key]
            # 硬编码的默认值
            try:
                got = eval(raw, {"__builtins__": {}}, {})  # noqa: S307
            except Exception:
                continue
            if isinstance(want, (int, float)) and isinstance(got, (int, float)):
                if abs(float(want) - float(got)) > 1e-9:
                    line = text[:m.start()].count("\n") + 1
                    issues.append((str(py.relative_to(ROOT)), line, key,
                                   got, want))
    if not issues:
        print("没有发现默认值不一致")
        return 0
    print(f"发现 {len(issues)} 处硬编码默认值和 DEFAULTS 不一致:")
    for f, ln, k, got, want in issues:
        print(f"  {f}:{ln}  {k}  硬编码={got}  DEFAULTS={want}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
