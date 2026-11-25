# -*- coding: utf-8 -*-
"""拉全市场财务摘要(PIT 用) —— 并发版。

财务缓存只有 300 只时,选股页的「排除高 ROIC」过滤器大部分票用不上。
串行跑实测每只 4~5 秒(东财财务接口慢),5239 只要 6 小时,改成 8 并发约 40 分钟。

断点续传:每完成 50 只落盘一次,中断再跑会跳过已有的。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_lock = threading.Lock()
_done = 0


def main() -> int:
    from quantlab.data import em
    from quantlab.data.store import store

    path = ROOT / "data" / "finance_pit.json"
    cache: dict = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}

    c = store().conn
    codes = [r[0] for r in c.execute(
        "select distinct code from kline_cache where klt=101 and fqt=1 "
        "and length(code)=6 and substr(code,1,1) in ('0','3','6') "
        "order by code").fetchall()]
    todo = [x for x in codes if not cache.get(x)]
    print(f"全市场 {len(codes)} 只,已有 {len(cache)},待拉 {len(todo)}", flush=True)
    if not todo:
        return 0

    def fetch_one(code: str) -> tuple[str, dict | None]:
        try:
            rows = em.finance_summary(code) or []
        except Exception:  # noqa: BLE001
            return code, None
        if not rows:
            return code, None
        return code, {str(r.get("period")): r for r in rows if r.get("period")}

    t0 = time.time()
    global _done
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_one, code): code for code in todo}
        for fut in as_completed(futs):
            code, data = fut.result()
            if data:
                cache[code] = data
            with _lock:
                _done += 1
                n = _done
            if n % 50 == 0:
                path.write_text(json.dumps(cache, ensure_ascii=False),
                                encoding="utf-8")
                speed = n / max(1e-6, time.time() - t0)
                left = (len(todo) - n) / max(speed, 1e-6) / 60
                print(f"  {n}/{len(todo)} 已入库 {len(cache)} 只 · "
                      f"剩约 {left:.0f} 分钟", flush=True)

    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"完成:{len(cache)} 只,耗时 {(time.time() - t0) / 60:.1f} 分钟", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
