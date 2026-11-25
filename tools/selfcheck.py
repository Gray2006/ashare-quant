# -*- coding: utf-8 -*-
"""静态自检:数据完整性、缓存状态、性能热点、磁盘占用。

不联网,不写数据,只读。跑完给一份体检报告。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OK, WARN, BAD = "OK", "WARN", "BAD"
rows: list[tuple[str, str, str]] = []


def mark(level: str, item: str, detail: str) -> None:
    rows.append((level, item, detail))
    print(f"[{level:>4}] {item:<28} {detail}", flush=True)


def check_db() -> None:
    from quantlab.data.store import store
    db = ROOT / "data" / "quantlab.db"
    if not db.exists():
        mark(BAD, "quantlab.db", "不存在")
        return
    size = db.stat().st_size / 1e9
    mark(OK if size < 8 else WARN, "quantlab.db", f"{size:.2f} GB")

    c = store().conn
    try:
        n, lo, hi = c.execute(
            "select count(*), min(bar_time), max(bar_time) from kline_cache "
            "where klt=101 and fqt=1").fetchone()
        mark(OK, "日线", f"{n:,} 根 {lo}~{hi}")
        codes = c.execute(
            "select count(distinct code) from kline_cache where klt=101 and fqt=1"
        ).fetchone()[0]
        mark(OK if codes > 4000 else WARN, "日线股票数", f"{codes}")
        n5 = c.execute(
            "select count(distinct code) from kline_cache where klt=5 and fqt=1"
        ).fetchone()[0]
        mark(OK if n5 >= 40 else WARN, "5分钟线股票数", f"{n5}(做T/学习要用)")
        # 最近交易日缺口
        last = c.execute(
            "select max(bar_time) from kline_cache where klt=101 and fqt=1"
        ).fetchone()[0]
        today = time.strftime("%Y-%m-%d")
        mark(OK if last and last >= today[:8] else WARN, "日线新鲜度",
             f"最新 {last} / 今天 {today}")
    except Exception as e:  # noqa: BLE001
        mark(BAD, "kline_cache", str(e)[:60])


def check_caches() -> None:
    fin = ROOT / "data" / "finance_pit.json"
    if fin.exists():
        try:
            d = json.loads(fin.read_text(encoding="utf-8"))
            periods = sum(len(v) for v in d.values())
            mark(OK, "财务 PIT 缓存", f"{len(d)} 只 / {periods} 期 "
                                     f"{fin.stat().st_size / 1e6:.1f} MB")
        except Exception as e:  # noqa: BLE001
            mark(BAD, "财务 PIT 缓存", f"读不出:{str(e)[:50]}")
    else:
        mark(WARN, "财务 PIT 缓存", "还没拉")

    hm_db = ROOT / "hotmoney.db"
    if hm_db.exists():
        import sqlite3
        try:
            c = sqlite3.connect(str(hm_db))
            n, lo, hi = c.execute(
                "select count(*), min(day), max(day) from lhb_seats").fetchone()
            seats = c.execute("select count(distinct seat) from lhb_seats").fetchone()[0]
            c.close()
            mark(OK if n > 1000 else WARN, "龙虎榜席位",
                 f"{n} 条 / {seats} 席位 / {lo}~{hi}")
        except Exception as e:  # noqa: BLE001
            mark(BAD, "龙虎榜席位", str(e)[:50])
    else:
        mark(WARN, "龙虎榜席位", "还没采集")


def check_factor_lib() -> None:
    from quantlab.engine import factor_mine as fm
    lib = fm.load_library()
    if not lib:
        mark(WARN, "因子库", "空的")
        return
    with_audit = sum(1 for x in lib if x.get("audit"))
    from collections import Counter
    cnt = Counter(x.get("status") or "未审" for x in lib)
    mark(OK, "因子库", f"{len(lib)} 条,{with_audit} 条有审计记录")
    mark(OK if with_audit else WARN, "因子审计覆盖",
         ", ".join(f"{k}={v}" for k, v in cnt.most_common()))


def check_perf() -> None:
    """几个最常走的路径的耗时。"""
    from quantlab.engine import factor_mine as fm
    t = time.time()
    panel = fm._panel(days=250, max_stocks=300, market="a")
    dt = time.time() - t
    mark(OK if dt < 3 else WARN, "取面板(300只×250天)", f"{dt:.2f}s")

    t = time.time()
    fm._factor_panel(panel, "slope(close,20)")
    dt = time.time() - t
    mark(OK if dt < 1 else WARN, "单因子全池求值", f"{dt:.2f}s")

    t = time.time()
    from quantlab.engine import t0_learn as tl
    tl.learn("600519")
    dt = time.time() - t
    mark(OK if dt < 5 else WARN, "做T参数学习(单票)", f"{dt:.2f}s")


def check_imports() -> None:
    """核心模块能不能 import(删 import 后的快速回归)。"""
    mods = ["quantlab.engine.dsl", "quantlab.engine.factor_mine",
            "quantlab.engine.factor_analysis", "quantlab.engine.factor_evolve",
            "quantlab.engine.seat_profile", "quantlab.engine.hotmoney_filter",
            "quantlab.engine.finance_pit", "quantlab.engine.cv",
            "quantlab.engine.autotrader", "quantlab.engine.t0_learn",
            "quantlab.broker.sim", "quantlab.data.em"]
    bad = []
    for m in mods:
        try:
            __import__(m)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m}:{str(e)[:40]}")
    mark(OK if not bad else BAD, "核心模块 import",
         "全部正常" if not bad else "; ".join(bad[:3]))


def main() -> int:
    print("=" * 72)
    print("Gray 量化工作台 · 静态自检")
    print("=" * 72)
    check_imports()
    check_db()
    check_caches()
    check_factor_lib()
    check_perf()

    print("\n" + "=" * 72)
    bad = sum(1 for r in rows if r[0] == BAD)
    warn = sum(1 for r in rows if r[0] == WARN)
    print(f"结论:{len(rows)} 项,{bad} 个 BAD,{warn} 个 WARN")
    for level, item, detail in rows:
        if level != OK:
            print(f"  {level} {item}: {detail}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
