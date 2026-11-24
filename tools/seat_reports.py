# -*- coding: utf-8 -*-
"""批量生成主要游资席位的画像报告。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.engine import hotmoney as hm  # noqa: E402
from quantlab.engine import seat_profile as sp  # noqa: E402

SKIP = ("机构专用", "深股通专用", "沪股通专用", "专用")


def main() -> int:
    stats = hm.seat_stats(min_samples=3, days=60)
    picks = [s for s in stats if not any(k in s["seat"] for k in SKIP)]
    picks.sort(key=lambda s: -s["n"])
    top = picks[:5]
    print(f"挑 {len(top)} 个席位:")
    for s in top:
        print(f"  {s['n']:3d} 次  {s['seat'][:36]}")
    for i, s in enumerate(top, 1):
        md, js = sp.save_report(s["seat"], ROOT / "reports",
                                stem=f"seat-{i}-{s['seat'][:8]}")
        print(f"  写入 {Path(md).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
