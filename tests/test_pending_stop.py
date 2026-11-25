# -*- coding: utf-8 -*-
"""待执行止损自检。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from quantlab.engine import pending_stop as ps


def _setup(monkeypatch=None):
    tmp = Path(tempfile.mkdtemp()) / "pending_stop.json"
    old = ps.PATH
    ps.PATH = tmp
    return old


def test_record_and_pending():
    old = _setup()
    try:
        ps.clear_all()
        ps.record("600519", "T+1 不可卖", -0.08)
        d = ps.pending()
        assert "600519" in d
        assert d["600519"]["tries"] == 1
        assert abs(float(d["600519"]["pnl"]) + 0.08) < 1e-9
    finally:
        ps.PATH = old


def test_record_twice_bumps_tries():
    old = _setup()
    try:
        ps.clear_all()
        ps.record("000001", "T+1", -0.05)
        ps.record("000001", "T+1", -0.06)
        d = ps.pending()
        assert d["000001"]["tries"] == 2
        assert abs(float(d["000001"]["pnl"]) + 0.06) < 1e-9
    finally:
        ps.PATH = old


def test_clear():
    old = _setup()
    try:
        ps.clear_all()
        ps.record("600000", "T+1", -0.03)
        assert ps.count() == 1
        ps.clear("600000")
        assert ps.count() == 0
    finally:
        ps.PATH = old


def test_expired_dropped():
    old = _setup()
    try:
        ps.clear_all()
        ps.record("600001", "T+1", -0.03)
        d = ps._load()
        d["600001"]["ts"] = 0                    # 很久以前
        ps._save(d)
        assert ps.count() == 0
    finally:
        ps.PATH = old


def test_empty_code_ignored():
    old = _setup()
    try:
        ps.clear_all()
        ps.record("", "T+1", -0.03)
        assert ps.count() == 0
    finally:
        ps.PATH = old
