# -*- coding: utf-8 -*-
"""模拟盘 / 实盘反哺报告。

    python tools/flywheel.py
    python tools/flywheel.py --days 180
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    from quantlab.engine import flywheel

    rep = flywheel.build(days=args.days)
    print(flywheel.render(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
