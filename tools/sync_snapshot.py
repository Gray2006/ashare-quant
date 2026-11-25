# -*- coding: utf-8 -*-
"""盘后用全市场快照补当日日线。

为什么需要它:日线是按需拉的 —— 用户点开过的票才会更新。
2026-11-25 自检发现 11-19 之后每天只有 778 只有数据(11-18 之前是 5226 只),
选股/回测实际只在 778 只里挑。

逐只补 5000 次要半小时,不现实。新浪的全市场列表一次 100 条、50 页拿全,
盘后跑一次即可。东财 push2 的 clist 常被拒,所以走新浪。

用法(盘后 15:10 之后):
    python tools/sync_snapshot.py
    python tools/sync_snapshot.py --pages 60 --sleep 0.25
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


def snapshot_bars(pages: int = 60, sleep: float = 0.25) -> dict[str, dict]:
    """全市场快照 → {code: 一根日线}。"""
    from quantlab.data import sina
    from quantlab.data.kline import _last_trade_day

    # 盘前/盘中拿到的是上一交易日的收盘;15:10 之后才是当天的。
    # 用今天当日期会把昨天的收盘错标成今天(踩过一次)。
    day = _last_trade_day()
    out: dict[str, dict] = {}
    for page in range(1, pages + 1):
        try:
            rows = sina.market_list(page=page, num=100, sort="amount")
        except Exception as e:  # noqa: BLE001
            log(f"  第 {page} 页失败 {str(e)[:50]}")
            time.sleep(sleep * 2)
            continue
        if not rows:
            break
        for r in rows:
            code = (r.get("code") or "").strip()
            px = r.get("price")
            if not code or not px or float(px) <= 0:
                continue
            o = float(r.get("open") or px)
            h = max(float(r.get("high") or px), float(px))
            lo = float(r.get("low") or px)
            if lo <= 0 or lo > float(px):
                lo = float(px)
            out[code] = {"datetime": day, "open": o, "high": h, "low": lo,
                         "close": float(px),
                         "volume": float(r.get("volume") or 0),
                         "amount": float(r.get("amount") or 0)}
        if page % 10 == 0:
            log(f"  {page} 页,累计 {len(out)} 只")
        time.sleep(sleep)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=60, help="每页 100 条,60 页够全市场")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--force", action="store_true",
                    help="即使当日已够也重拉")
    args = ap.parse_args()

    from quantlab.data.kline import _last_trade_day
    from quantlab.data.store import store

    # 短路:库里最近一个交易日已经有足够多的票,就不用再拉 50 页。
    # 非交易日 / 盘前跑会拿到同一个交易日的快照,重复拉是纯浪费。
    st0 = store()
    day0 = _last_trade_day()
    have = st0.conn.execute(
        "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
        (day0,)).fetchone()[0]
    if have >= 3000:
        log(f"{day0} 已有 {have} 只,跳过(要强制重拉用 --force)")
        if not args.force:
            return 0

    bars = snapshot_bars(args.pages, args.sleep)
    if not bars:
        log("没拿到数据(非交易日?接口变了?)")
        return 1

    day = next(iter(bars.values()))["datetime"]
    st = store()
    before = st.conn.execute(
        "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
        (day,)).fetchone()[0]
    log(f"拉到 {len(bars)} 只的 {day} 快照;库里当日原有 {before} 只")

    ok = 0
    for i, (code, bar) in enumerate(bars.items(), 1):
        try:
            st.save_kline(code, 101, 1, [bar])
            ok += 1
        except Exception:  # noqa: BLE001
            pass
        if i % 1000 == 0:
            log(f"  入库 {i}/{len(bars)}")

    after = st.conn.execute(
        "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
        (day,)).fetchone()[0]
    log(f"完成:写入 {ok} 只;库里当日 {before} → {after} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
