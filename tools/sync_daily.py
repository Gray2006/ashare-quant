# -*- coding: utf-8 -*-
"""补日线:最近几个交易日的全市场覆盖缺口。

背景(2026-11-25 自检发现):
  2026-11-18 及之前每天 5226 只、amount 4473 只;
  2026-11-19 之后只剩 778 只、amount 51 只。
  —— 最近 4 个交易日的日线基本没更新,选股/回测实际只在 778 只里挑。

做法:逐只调 get_kline(它拉完会自动落库),只补「最新一根比全市场最新日期旧」的票。
腾讯源最快最稳,东财常被限流,所以默认 source="tx"。
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


def latest_by_code() -> tuple[str, dict[str, str]]:
    """全市场最新交易日 + 每只票的最新日期。

    「最新交易日」用 _last_trade_day() 而不是 max(bar_time):
    盘中只有十几只票落库,那一行就是今天,拿它当基准会把其余五千多只
    全判成「落后」,白跑一遍。
    """
    from quantlab.data.kline import _last_trade_day
    from quantlab.data.store import store
    c = store().conn
    day = _last_trade_day()
    rows = c.execute(
        "select code, max(bar_time) d from kline_cache where klt=101 and fqt=1 "
        "group by code").fetchall()
    return str(day or ""), {r["code"]: str(r["d"]) for r in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10, help="每只拉多少根")
    ap.add_argument("--limit", type=int, default=0, help="最多补多少只,0=不限")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--source", default="tx", help="tx / em / sina / auto")
    args = ap.parse_args()

    from quantlab.data.kline import get_kline

    day, last = latest_by_code()
    if not day:
        log("库里没有日线")
        return 1
    stale = [c for c, d in last.items() if d < day]
    log(f"全市场最新 {day};有 {len(stale)}/{len(last)} 只停在更早的日期")
    if not stale:
        log("没有缺口")
        return 0
    if args.limit:
        stale = stale[:args.limit]

    ok, fail = 0, 0
    for i, code in enumerate(stale, 1):
        try:
            bars, src = get_kline(code, 101, count=args.count, source=args.source)
            if bars:
                ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 3:
                log(f"  {code} 失败 {str(e)[:60]}")
        if i % 50 == 0:
            log(f"  {i}/{len(stale)} 成功 {ok} 失败 {fail}")
        time.sleep(args.sleep)

    day2, last2 = latest_by_code()
    still = sum(1 for c, d in last2.items() if d < day2)
    log(f"完成:成功 {ok} / 失败 {fail};仍有 {still} 只落后于 {day2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
