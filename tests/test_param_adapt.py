# -*- coding: utf-8 -*-
"""参数自适应自检:只测规则和边界,不打网络。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from quantlab.engine import param_adapt as pa


def _setup(monkeypatch, buckets, strategies=None, total=3000):
    tmp = Path(tempfile.mkdtemp())
    old_state, old_conf = pa.STATE, pa.CONFIG
    pa.STATE = tmp / "adapt.json"
    pa.CONFIG = tmp / "config.json"
    pa.CONFIG.write_text(json.dumps(
        {"autotrade": {"atr_mult": 2.5, "rr_ratio": 2.5}}), encoding="utf-8")
    pa.live_stats = lambda market="a": {"total": total, "rows": []}
    pa._bucket_health = lambda: {"ok": True, "buckets": buckets,
                                 "strategies": strategies or {}}
    return old_state, old_conf


def _restore(old):
    pa.STATE, pa.CONFIG = old


def test_halt_when_win_and_pf_both_low(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 100, "win": 0.30, "mean": -0.04},
        {"key": "b", "n": 100, "win": 0.32, "mean": -0.05},
    ])
    try:
        s = pa.suggest("a")
        assert s["action"] == "halt"
        assert "失效" in s["why"]
    finally:
        _restore(old)


def test_loosen_when_negative_but_win_ok(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 100, "win": 0.38, "mean": -0.02},
    ])
    try:
        s = pa.suggest("a")
        assert s["action"] == "loosen_stop"
        assert s["atr_mult"]["suggest"] > s["atr_mult"]["now"]
    finally:
        _restore(old)


def test_keep_when_healthy(monkeypatch):
    # 落在中间区:均值不为负、胜率不到 45%
    old = _setup(monkeypatch, [
        {"key": "a", "n": 100, "win": 0.42, "mean": 0.001},
    ])
    try:
        s = pa.suggest("a")
        assert s["action"] == "keep"
    finally:
        _restore(old)


def test_apply_refuses_on_small_sample(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 30, "win": 0.38, "mean": -0.02},
    ], total=50)
    try:
        r = pa.apply("a")
        # 样本不到 MIN_SAMPLES 时 suggest 直接返回 ok=False
        assert r.get("ok") is False or r.get("applied") is False
        assert "样本" in str(r.get("msg") or "")
    finally:
        _restore(old)


def test_apply_respects_interval(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 100, "win": 0.38, "mean": -0.02},
    ])
    try:
        pa._save_state({"last_apply": pa.time.time()})
        r = pa.apply("a")
        assert r["applied"] is False
        assert "24 小时" in r["msg"]
    finally:
        _restore(old)


def test_apply_and_rollback(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 100, "win": 0.38, "mean": -0.02},
    ])
    try:
        r = pa.apply("a", force=True)
        assert r["applied"] is True
        conf = json.loads(pa.CONFIG.read_text(encoding="utf-8"))
        assert conf["autotrade"]["atr_mult"] != 2.5
        rb = pa.rollback()
        assert rb["ok"] is True
        conf2 = json.loads(pa.CONFIG.read_text(encoding="utf-8"))
        assert conf2["autotrade"]["atr_mult"] == 2.5
    finally:
        _restore(old)


def test_atr_bounds(monkeypatch):
    old = _setup(monkeypatch, [
        {"key": "a", "n": 200, "win": 0.30, "mean": -0.06},
        {"key": "b", "n": 200, "win": 0.34, "mean": -0.05},
    ])
    try:
        for _ in range(10):
            pa.apply("a", force=True)
        conf = json.loads(pa.CONFIG.read_text(encoding="utf-8"))
        v = float(conf["autotrade"]["atr_mult"])
        assert pa.ATR_MIN <= v <= pa.ATR_MAX
    finally:
        _restore(old)
