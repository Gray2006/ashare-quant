# -*- coding: utf-8 -*-
"""数据就绪度:哪些维度够做因子审计了。

背景:2026-11-28 查完发现新维度全都不够(5分钟线每只 22 天、
新闻覆盖率 1-2%),只能等数据积累。

这个工具回答一个问题:**现在能审计什么,还差多少**。
避免每次都要重新查一遍数据规模,也避免在样本不够时硬跑出过拟合结论。

    python tools/data_readiness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 审计门槛(经验值,来自实际踩过的坑)
REQ = {
    "日线": {"min_days": 250, "min_codes": 300,
             "why": "至少一年 + 300 只,否则分年度验证做不了"},
    "60分钟": {"min_days": 250, "min_codes": 300,
               "why": "同上;日内微观因子已证否,再看要有新思路"},
    "5分钟": {"min_codes": 300, "min_days_per_code": 60,
              "why": "每只至少 60 天才够分季度验证;现在 22~25 天"},
    "新闻": {"min_coverage_pct": 20.0,
             "why": "横截面因子至少要覆盖 20% 的票;现在 1~2%"},
}


def _q(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()
    except Exception:  # noqa: BLE001
        return None


def check_kline(conn) -> list[dict]:
    out = []
    for klt, label in ((101, "日线"), (60, "60分钟"), (5, "5分钟")):
        r = _q(conn,
               "select count(distinct code) codes, "
               "count(distinct substr(bar_time,1,10)) days "
               "from kline_cache where klt=?", (klt,))
        if not r:
            continue
        # 每只票的中位天数(比总量更有意义)
        rows = conn.execute(
            "select count(distinct substr(bar_time,1,10)) d from kline_cache "
            "where klt=? group by code", (klt,)).fetchall()
        ds = sorted(int(x["d"]) for x in rows)
        med = ds[len(ds) // 2] if ds else 0
        out.append({"label": label, "codes": int(r["codes"]),
                    "days": int(r["days"]), "median_days": med})
    return out


def check_news(conn) -> dict | None:
    r = _q(conn, "select count(*) n, count(distinct key) codes, "
                 "count(distinct day) days from news_archive")
    if not r or not r["n"]:
        return None
    # 最近 10 天平均每天覆盖多少票
    rows = conn.execute(
        "select day, count(distinct key) c from news_archive "
        "group by day order by day desc limit 10").fetchall()
    per_day = [int(x["c"]) for x in rows] or [0]
    avg_codes = sum(per_day) / len(per_day)
    total = _q(conn, "select count(distinct code) n from kline_cache "
                     "where klt=101")
    universe = int(total["n"] or 0) if total else 0
    cov = (avg_codes / universe * 100) if universe else 0.0
    return {"n": int(r["n"]), "days": int(r["days"]),
            "avg_codes": avg_codes, "universe": universe,
            "coverage_pct": cov}


def main() -> int:
    from quantlab.data.store import store

    conn = store().conn
    print("=" * 66)
    print("数据就绪度 · 哪些维度够做因子审计")
    print("=" * 66)

    klines = {x["label"]: x for x in check_kline(conn)}
    news = check_news(conn)

    print("\n【日线 / 分钟线】")
    for label in ("日线", "60分钟", "5分钟"):
        x = klines.get(label)
        if not x:
            print(f"  {label}: 无数据")
            continue
        req = REQ.get(label) or {}
        need_codes = int(req.get("min_codes") or 0)
        if label == "5分钟":
            need_med = int(req.get("min_days_per_code") or 0)
            ok = (x["codes"] >= need_codes
                  and x["median_days"] >= need_med)
            gap = ""
            if x["codes"] < need_codes:
                gap += f" 差 {need_codes - x['codes']} 只"
            if x["median_days"] < need_med:
                gap += f" 每只差 {need_med - x['median_days']} 天"
        else:
            need_days = int(req.get("min_days") or 0)
            ok = x["codes"] >= need_codes and x["days"] >= need_days
            gap = ""
            if x["codes"] < need_codes:
                gap += f" 差 {need_codes - x['codes']} 只"
            if x["days"] < need_days:
                gap += f" 差 {need_days - x['days']} 天"
        mark = "OK " if ok else "不够"
        print(f"  [{mark}] {label}: {x['codes']} 只 / {x['days']} 天 "
              f"(每只中位 {x['median_days']} 天){gap}")
        if not ok and req.get("why"):
            print(f"         {req['why']}")

    print("\n【新闻】")
    if not news:
        print("  无数据")
    else:
        req = REQ["新闻"]
        need = float(req["min_coverage_pct"])
        ok = news["coverage_pct"] >= need
        print(f"  [{'OK ' if ok else '不够'}] {news['n']} 条 / "
              f"{news['days']} 天,最近每天覆盖 {news['avg_codes']:.0f} 只 "
              f"({news['coverage_pct']:.1f}% / 需要 {need:.0f}%)")
        if not ok:
            print(f"         {req['why']}")

    print("\n" + "=" * 66)
    ready = []
    for label in ("日线", "60分钟"):
        x = klines.get(label)
        if x and x["codes"] >= 300 and x["days"] >= 250:
            ready.append(label)
    if ready:
        print(f"可以审计: {'、'.join(ready)}(但日线维度已挖空)")
    print("5分钟线和新闻还需要时间积累 —— 不用急,数据在自动攒。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
