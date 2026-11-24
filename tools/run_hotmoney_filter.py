# -*- coding: utf-8 -*-
"""游资负面清单:跑一遍看今天该躲哪些票,并验证「躲开」在样本内有没有用。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.engine import hotmoney as hm  # noqa: E402
from quantlab.engine import hotmoney_filter as hf  # noqa: E402

def main() -> int:
    day = hm.latest_day()
    if not day:
        print("库里还没有龙虎榜数据")
        return 1
    print(f"基准日 {day}(库内最新有席位数据的一天)\n")

    buys = hf.hot_buys(day)
    print(f"近 {hf.LOOKBACK} 日大额净买入(≥{hf.NET_THRESHOLD / 1e8:.1f} 亿)的票:{len(buys)} 只")
    for code, info in list(buys.items())[:8]:
        print(f"  {code} {info['name'][:8]:<9} 净买入 {info['net'] / 1e8:+.2f} 亿 "
              f"({info['from']}~{info['to']}, {info['n']} 次)")

    risky = hf.screen(day)
    print(f"\n判定为「该躲」的:{len(risky)} 只(位置 ≥{hf.POS_THRESHOLD:.0%})")
    for r in risky[:10]:
        print(f"  {r['code']} {r.get('name', '')[:8]:<9} 位置 {r['pos']:.0%}  "
              f"净买入 {r['net'] / 1e8:+.2f} 亿  | {'; '.join(r['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
