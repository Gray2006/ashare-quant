# -*- coding: utf-8 -*-
"""OKX 凭证加密存储自检。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import importlib

import pytest

# quantlab.security.okx_creds 依赖 config.USER_DIR,测试里换掉路径
oc = importlib.import_module("quantlab.security.okx_creds")


@pytest.fixture()
def tmp_key(monkeypatch):
    p = Path(tempfile.mkdtemp()) / "okx.key"
    monkeypatch.setattr(oc, "OKX_KEY_PATH", p)
    # 清掉环境变量,避免干扰
    for e in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
        monkeypatch.delenv(e, raising=False)
    return p


def test_save_and_load(tmp_key):
    doc = {"api_key": "aaa111", "api_secret": "bbb222", "passphrase": "ccc333"}
    assert oc.save_creds(doc) is True
    got = oc.load_creds()
    assert got == doc
    assert oc.has_creds() is True


def test_encrypted_not_plaintext(tmp_key):
    """写进文件的不能是明文。"""
    oc.save_creds({"api_key": "SECRETKEY", "api_secret": "SECRETSEC",
                   "passphrase": "SECRETPASS"})
    raw = tmp_key.read_bytes()
    assert b"SECRETKEY" not in raw, "密钥原文出现在文件里"
    assert raw[:7] == b"dpapi1:" or not raw.startswith(b"{")


def test_env_overrides_file(tmp_key, monkeypatch):
    oc.save_creds({"api_key": "fromfile", "api_secret": "s1",
                   "passphrase": "p1"})
    monkeypatch.setenv("OKX_API_KEY", "fromenv")
    got = oc.load_creds()
    assert got["api_key"] == "fromenv"


def test_empty_clears(tmp_key):
    oc.save_creds({"api_key": "a", "api_secret": "b", "passphrase": "c"})
    assert oc.has_creds() is True
    oc.save_creds({})
    assert oc.has_creds() is False


def test_legacy_plaintext_file_migrated(tmp_key):
    """旧版明文 json 文件读到就顺手加密。"""
    tmp_key.write_text(json.dumps({"api_key": "old1", "api_secret": "old2",
                                   "passphrase": "old3"}), encoding="utf-8")
    got = oc.load_creds()
    assert got["api_key"] == "old1"
    raw = tmp_key.read_bytes()
    assert b"old1" not in raw, "旧明文没被加密替换"
