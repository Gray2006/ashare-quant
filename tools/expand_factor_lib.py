# -*- coding: utf-8 -*-
"""扩容因子库:从候选里挑未审过的,审完入库。

和 evolve 的区别:evolve 是「重审库里已有的」,这个是「把新的审进库」。
两者合起来才是完整闭环 —— 挖出来 → 审 → 入库(带判定) → 定期重审。

用法:
    python tools/expand_factor_lib.py --n 10
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="这次审几个")
    ap.add_argument("--max-stocks", type=int, default=300)
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args()

    from quantlab.engine import factor_evolve as fe
    from quantlab.engine import factor_mine as fm

    lib = fm.load_library()
    have = {x.get("expr") for x in lib}
    todo = [c for c in fm.candidates() if c["expr"] not in have][:args.n]
    if not todo:
        log("没有未审的候选了")
        return 0
    log(f"库里 {len(lib)} 条,这次审 {len(todo)} 个新候选")

    panel = fm._panel_unbiased(days=args.days, max_stocks=args.max_stocks, market="a")
    ok = 0
    for i, c in enumerate(todo, 1):
        t = time.time()
        r = fe.audit_and_record(c["expr"], c["label"], panel=panel,
                                horizon=args.horizon)
        if r.get("ok"):
            ok += 1
            log(f"  {i}/{len(todo)} {c['label']:<14} "
                f"原始 t={r['raw_t']} 中性 t={r['neu_t']} → {r['status']} "
                f"({time.time() - t:.0f}s)")
        else:
            log(f"  {i}/{len(todo)} {c['label']:<14} 失败 {r.get('msg')}")

    lib2 = fm.load_library()
    from collections import Counter
    log(f"完成:{ok}/{len(todo)};库 {len(lib)} → {len(lib2)} 条")
    log("状态分布:" + str(dict(Counter(x.get("status") or "未审" for x in lib2))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
