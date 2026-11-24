# -*- coding: utf-8 -*-
"""扫一遍所有席位:谁的买入信号有可靠的反向(规避)价值。

判定口径:按日聚合超额的 t(同一天多只票不重复计数),反向 t ≥ 2 才算数。
不联网(不查行业),只读本地库,快。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.engine import hotmoney as hm  # noqa: E402
from quantlab.engine import seat_profile as sp  # noqa: E402

SKIP = ("机构专用", "深股通专用", "沪股通专用", "专用", "专用交易单元")


def main() -> int:
    stats = hm.seat_stats(min_samples=3, days=60)
    stats = [s for s in stats if not any(k in s["seat"] for k in SKIP)]
    print(f"扫描 {len(stats)} 个席位…", flush=True)
    rows = []
    for i, s in enumerate(stats, 1):
        try:
            p = sp.profile(s["seat"], enrich=False)
        except Exception as e:  # noqa: BLE001
            print(f"  {i}/{len(stats)} 失败 {str(e)[:50]}")
            continue
        if not p.get("ok"):
            continue
        rows.append({
            "seat": s["seat"], "n": p["n"], "days": p["n_days"],
            "ret5": p["ret5"], "bench5": p["bench5"], "excess5": p["excess5"],
            "t": p["excess_t"], "reverse_t": p["reverse_t"],
            "win5": p["win5"], "tags": "/".join(p["tags"]),
        })
        if i % 10 == 0:
            print(f"  {i}/{len(stats)}", flush=True)

    rows.sort(key=lambda r: -r["reverse_t"])
    print("\n反向 t 最高的 15 个(全部样本):")
    print(f"{'反向t':>7} {'超额':>8} {'胜率':>5} {'笔数':>4} {'天数':>4}  席位")
    for r in rows[:15]:
        print(f"{r['reverse_t']:>7.2f} {r['excess5'] * 100:>7.2f}% "
              f"{r['win5'] * 100:>4.0f}% {r['n']:>4d} {r['days']:>4d}  "
              f"{r['seat'][:34]}")

    # 样本太小算出来的 t 是噪声:8 笔 3 天也能出 t=6
    MIN_N, MIN_DAYS = 30, 10
    solid = [r for r in rows if r["n"] >= MIN_N and r["days"] >= MIN_DAYS]
    solid.sort(key=lambda r: -r["reverse_t"])
    print(f"\n样本够的席位(n≥{MIN_N} 且天数≥{MIN_DAYS}):{len(solid)} 个")
    print(f"{'反向t':>7} {'超额':>8} {'胜率':>5} {'笔数':>4} {'天数':>4}  席位")
    for r in solid[:12]:
        print(f"{r['reverse_t']:>7.2f} {r['excess5'] * 100:>7.2f}% "
              f"{r['win5'] * 100:>4.0f}% {r['n']:>4d} {r['days']:>4d}  "
              f"{r['seat'][:34]}")

    strong = [r for r in solid if r["reverse_t"] >= 2.0]
    print(f"\n样本够且反向 t ≥ 2:{len(strong)} / {len(solid)}")
    if strong:
        print("  " + ", ".join(f"{r['seat'][:14]}({r['reverse_t']:.1f})"
                               for r in strong[:10]))
    pos = [r for r in solid if (r["excess5"] or 0) > 0.01]
    neg = [r for r in solid if (r["excess5"] or 0) < 0]
    print(f"样本够且正向超额 > 1%:{len(pos)} / {len(solid)}")

    # 单个席位不显著,但「一致为负」本身就是信息:做二项检验
    if solid:
        from math import comb
        k = len(neg)
        n = len(solid)
        p = sum(comb(n, i) for i in range(k, n + 1)) / (2 ** n) * 2
        print(f"\n样本够的席位里超额为负的:{k}/{n},"
              f"二项检验 p={min(p, 1.0):.4f}"
              + ("(显著)" if p < 0.05 else "(不显著)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
