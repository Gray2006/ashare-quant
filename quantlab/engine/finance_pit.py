# -*- coding: utf-8 -*-
"""财务数据落地 + PIT(point-in-time)面板。

关键约束:东财给的是**报告期**(2026三季报),不是**公告日**。
拿报告期直接对齐交易日 = 前视偏差 —— 三季报的数据 9 月 30 日就有了,
但 10 月底才公布,9 月就能用等于偷看未来。

这里的处理:按法定披露截止日做**保守 PIT** ——
  一季报 4-30 前、半年报 8-31 前、三季报 10-31 前、年报次年 4-30 前。
用「最晚可能公告日」当可用起点,宁可晚用也不早用。

产出:
  data/finance_pit.json   {code: {period: {...}}}
  tools 内直接算面板用
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 报告期 → 最晚公告日(月, 日, 跨年偏移)
DEADLINE = {
    "一季报": (4, 30, 0),
    "中报": (8, 31, 0),
    "半年报": (8, 31, 0),
    "三季报": (10, 31, 0),
    "年报": (4, 30, 1),
}


def period_to_available(period: str) -> str | None:
    """把 '2026三季报' 转成「数据可用日」'2026-10-31'。"""
    import re
    m = re.match(r"(\d{4})\s*(一季报|中报|半年报|三季报|年报)", period or "")
    if not m:
        return None
    year, kind = int(m.group(1)), m.group(2)
    mon, day, add = DEADLINE[kind]
    return f"{year + add:04d}-{mon:02d}-{day:02d}"


def fetch(codes: list[str], sleep: float = 0.35) -> dict:
    """拉财务摘要,存 {code: {period: row}}。已存在就跳过。"""
    from quantlab.data import em

    path = ROOT / "data" / "finance_pit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}

    got = 0
    for i, code in enumerate(codes, 1):
        if code in cache and cache[code]:
            continue
        try:
            rows = em.finance_summary(code) or []
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            cache[code] = {str(r.get("period")): r for r in rows if r.get("period")}
            got += 1
        if i % 20 == 0:
            print(f"  {i}/{len(codes)} 已拉 {got}", flush=True)
            path.write_text(json.dumps(cache, ensure_ascii=False),
                            encoding="utf-8")
        time.sleep(sleep)

    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    print(f"完成:{len(cache)} 只,文件 {path}")
    return cache


def pit_panel(cache: dict, dates: list[str]) -> dict[str, dict[str, dict]]:
    """{code: {交易日: 当期财务}} —— 每个交易日只取当时已经公告的最新一期。"""
    out: dict[str, dict[str, dict]] = {}
    for code, periods in cache.items():
        seq = []
        for period, row in periods.items():
            avail = period_to_available(period)
            if avail:
                seq.append((avail, row))
        seq.sort(key=lambda kv: kv[0])
        if not seq:
            continue
        per_day: dict[str, dict] = {}
        j = -1
        for d in dates:
            while j + 1 < len(seq) and seq[j + 1][0] <= d:
                j += 1
            if j >= 0:
                per_day[d] = seq[j][1]
        if per_day:
            out[code] = per_day
    return out


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from quantlab.engine import factor_mine as fm

    panel = fm._panel(days=250, max_stocks=300, market="a")
    codes = list(panel["close"].columns)
    print(f"拉 {len(codes)} 只的财务摘要…")
    cache = fetch(codes)
    dates = [str(d)[:10] for d in panel["close"].index]
    pp = pit_panel(cache, dates)
    print(f"PIT 面板:{len(pp)} 只 × {len(dates)} 天")
