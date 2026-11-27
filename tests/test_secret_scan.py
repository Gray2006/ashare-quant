# -*- coding: utf-8 -*-
"""凭证扫描自检:真密钥要命中,示例/占位符不能误报。"""
from __future__ import annotations


from quantlab.security import secret_scan as ss


def _hits(text: str, path: str = "x.py") -> list[dict]:
    return ss.scan_text(text, path)


def test_okx_secret_hit():
    t = 'api_secret = "A1B2C3D4E5F60718293A4B5C6D7E8F90"'
    h = _hits(t)
    assert any(x["name"] == "okx_secret" for x in h)


def test_okx_secret_placeholder_miss():
    """占位符不能报。"""
    for t in ('api_secret = "XXXX"',
              'api_secret = "00000000000000000000000000000000"',
              'api_secret = "your_secret_here"',
              '# api_secret = "A1B2C3D4E5F60718293A4B5C6D7E8F90"'):
        assert _hits(t) == [], f"误报: {t}"


def test_openai_sk_hit():
    t = 'key = "sk-abcdefghijklmnopqrstuvwxyz123456"'
    assert any(x["name"] == "openai_sk" for x in _hits(t))


def test_gray_gk_hit():
    t = 'key = "gk_0123456789abcdef0123456789abcdef"'
    assert any(x["name"] == "gray_gk" for x in _hits(t))


def test_dpapi_blob_hit():
    t = ('"token": "dpapi1:R1JBWTE6AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA'
         '+u1SngYp70avVhW1SiJIwwAAAAACAAAAAAAQZgAAAAEAACAAAADGRa1ou"')
    assert any(x["name"] == "dpapi_blob" for x in _hits(t))


def test_private_key_hit():
    t = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
    assert any(x["name"] == "private_key" for x in _hits(t))


def test_doc_files_skipped():
    """文档里的示例不扫。"""
    t = 'api_secret = "A1B2C3D4E5F60718293A4B5C6D7E8F90"'
    assert _hits(t, "README.md") == []
    assert _hits(t, "docs/x.md") == []


def test_normal_code_no_hit():
    t = """
def foo(api_key: str) -> None:
    pass
password = os.environ.get("PASS")
"""
    assert _hits(t) == []


def test_mask_hides_value():
    """命中时不能把密钥原文打出来。"""
    t = 'api_secret = "A1B2C3D4E5F60718293A4B5C6D7E8F90"'
    h = _hits(t)[0]
    assert "A1B2C3D4E5F60718293A4B5C6D7E8F90" not in h["snippet"]
    assert "*" in h["snippet"]
