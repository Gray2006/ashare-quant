# -*- coding: utf-8 -*-
"""因子进化闭环:定级规则 + 库写入 + 衰减标记。"""
from __future__ import annotations

from quantlab.engine import factor_evolve as fe


def test_verdict_both_significant():
    s, why = fe._verdict_of(2.5, 2.1)
    assert s == "promoted" and "都 ≥" in why


def test_verdict_beta_disguise():
    s, why = fe._verdict_of(2.5, 0.4)
    assert s == "rejected" and "beta 伪装" in why


def test_verdict_both_negative():
    s, why = fe._verdict_of(-2.0, -1.5)
    assert s == "rejected" and "都为负" in why


def test_verdict_weak():
    s, why = fe._verdict_of(1.2, 0.8)
    assert s == "rejected" and "未达" in why


def test_verdict_handles_none():
    s, _why = fe._verdict_of(None, None)
    assert s == "rejected"


def test_write_back_creates_and_updates(tmp_path, monkeypatch):
    lib = tmp_path / "lib.json"
    monkeypatch.setattr(fe.fm, "LIBRARY", lib)

    out = {"expr": "close/ma(close,20)", "label": "均线偏离",
           "raw_t": 2.5, "neu_t": 2.2, "raw_excess": 0.01,
           "neu_excess": 0.009, "ic": 0.03, "horizon": 5,
           "status": "promoted", "why": "两份都过", "at": "2026-11-25 01:00"}
    fe._write_back(out)
    libdata = fe.fm.load_library()
    assert len(libdata) == 1
    assert libdata[0]["status"] == "promoted"
    assert libdata[0]["history"][0]["raw_t"] == 2.5

    # 第二次审计不达标 → 变成 decayed
    out2 = dict(out, raw_t=1.0, neu_t=0.5, status="rejected",
                why="原始未达", at="2026-11-26 01:00")
    fe._write_back(out2)
    libdata = fe.fm.load_library()
    assert libdata[0]["status"] == "decayed"
    assert len(libdata[0]["history"]) == 2


def test_history_is_capped(tmp_path, monkeypatch):
    lib = tmp_path / "lib.json"
    monkeypatch.setattr(fe.fm, "LIBRARY", lib)
    for i in range(30):
        fe._write_back({"expr": "x", "label": "x", "raw_t": 1.0, "neu_t": 0.5,
                        "raw_excess": 0, "neu_excess": 0, "ic": 0, "horizon": 5,
                        "status": "rejected", "why": "-",
                        "at": f"2026-11-{i + 1:02d} 00:00"})
    assert len(fe.fm.load_library()[0]["history"]) == 20


def test_leaderboard_orders_by_min_t(tmp_path, monkeypatch):
    lib = tmp_path / "lib.json"
    monkeypatch.setattr(fe.fm, "LIBRARY", lib)
    for expr, rt, nt in (("a", 2.5, 2.1), ("b", 3.0, 0.2), ("c", 1.0, 0.9)):
        fe._write_back({"expr": expr, "label": expr, "raw_t": rt, "neu_t": nt,
                        "raw_excess": 0, "neu_excess": 0, "ic": 0, "horizon": 5,
                        "status": "promoted" if min(rt, nt) >= 2 else "rejected",
                        "why": "-", "at": "2026-11-25 00:00"})
    rows = fe.leaderboard()
    # 按两份 t 的较小值排:min(2.5,2.1)=2.1 > min(1.0,0.9)=0.9 > min(3.0,0.2)=0.2
    assert [r["expr"] for r in rows] == ["a", "c", "b"]
