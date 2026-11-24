# -*- coding: utf-8 -*-
"""做T亏损归因:完整轮 vs 尾盘强平,各贡献多少。"""
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.data.store import store  # noqa: E402
from quantlab.engine.t0 import T0Config, T0Engine  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
import run_t0 as R  # noqa: E402


def main() -> None:
    codes = [r[0] for r in store().conn.execute(
        "select distinct code from kline_cache where klt=5 and fqt=1").fetchall()]
    daily = {}
    for code in codes:
        bars = R.daily_bars(code)
        if len(bars) >= 250:
            daily[code] = {b["date"]: b["close"] for b in bars}
    data = {}
    for code in codes:
        if code in daily:
            d = R.min5_days(code, daily[code])
            if d:
                data[code] = d

    for mode in ("reverse", "forward"):
        cfg = T0Config(base_shares=1000, ratio=0.30, mode=mode,
                       min_pct=0.010, take_pct=0.005)
        bucket = defaultdict(lambda: {"n": 0, "net": 0.0, "gross": 0.0, "cost": 0.0,
                                      "worst": 0.0, "best": 0.0})
        for code, days in data.items():
            recs = T0Engine(code, cfg).run(days)
            for r in recs:
                key = "完整一轮" if r.filled else (
                    "尾盘强平" if r.note.startswith("尾盘") else "未触发")
                b = bucket[key]
                b["n"] += 1
                b["net"] += r.net
                b["gross"] += r.gross
                b["cost"] += r.cost
                b["worst"] = min(b["worst"], r.net)
                b["best"] = max(b["best"], r.net)
        print(f"\n=== {mode} 1.0/0.5 ===")
        for k, v in bucket.items():
            avg = v["net"] / v["n"] if v["n"] else 0
            print(f"{k:<6} n={v['n']:<5} 净={v['net']:+10.0f} 均={avg:+8.1f} "
                  f"毛={v['gross']:+9.0f} 成本={v['cost']:8.0f} "
                  f"最差={v['worst']:+8.1f} 最好={v['best']:+8.1f}")

    # 完整轮里,赢了多小、输了多大
    cfg = T0Config(base_shares=1000, ratio=0.30, mode="reverse",
                   min_pct=0.010, take_pct=0.005)
    wins, losses = [], []
    for code, days in data.items():
        for r in T0Engine(code, cfg).run(days):
            if r.filled:
                (wins if r.net > 0 else losses).append(r.net)
    if wins:
        print(f"\n完整轮 赢 {len(wins)} 笔 均值 {sum(wins)/len(wins):+.1f}")
    if losses:
        print(f"完整轮 亏 {len(losses)} 笔 均值 {sum(losses)/len(losses):+.1f}")


if __name__ == "__main__":
    main()
