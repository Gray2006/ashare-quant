# -*- coding: utf-8 -*-
"""OKX 凭证加密存储 —— 照搬 cloud.key 的 DPAPI 模式。

问题:OKX 的 api_key / api_secret / passphrase 明文存在 config.json,
而 config.json 里还有一堆配置。一旦它被误提交(2026-11-26 就发生过),
密钥直接泄露。

改法:
  凭证单独存 USER_DIR/okx.key(DPAPI 加密,仅本机当前用户可解),
  config.json 里留空。
  读取时:环境变量 > okx.key > config.json(向后兼容旧配置)。

迁移:tools/migrate_okx_keys.py 把 config.json 里的明文搬进 okx.key。
"""
from __future__ import annotations

import json
import os

from ..config import USER_DIR
from .. import secure

OKX_KEY_PATH = USER_DIR / "okx.key"

FIELDS = ("api_key", "api_secret", "passphrase")


def load_creds() -> dict:
    """读 OKX 凭证。优先环境变量,其次加密文件,最后 config.json(兼容)。"""
    out = {k: "" for k in FIELDS}
    # 1) 环境变量
    env_map = {"api_key": "OKX_API_KEY", "api_secret": "OKX_API_SECRET",
               "passphrase": "OKX_PASSPHRASE"}
    for k, env in env_map.items():
        v = os.environ.get(env)
        if v:
            out[k] = v.strip()
    if all(out.values()):
        return out

    # 2) 加密文件
    try:
        raw = OKX_KEY_PATH.read_bytes()
    except Exception:
        raw = b""
    if raw:
        if raw.startswith(secure.MAGIC):
            try:
                doc = json.loads(secure.unprotect(raw))
                for k in FIELDS:
                    if not out[k] and doc.get(k):
                        out[k] = str(doc[k])
            except Exception:
                pass
        else:  # 旧版明文文件,顺手迁移
            try:
                doc = json.loads(raw.decode("utf-8", "replace"))
                for k in FIELDS:
                    if not out[k] and doc.get(k):
                        out[k] = str(doc[k])
                save_creds(out)
            except Exception:
                pass
    if all(out.values()):
        return out

    # 3) config.json(向后兼容)
    try:
        from ..config import cfg
        o = cfg().get("broker.okx", {}) or {}
        for k in FIELDS:
            if not out[k] and o.get(k):
                out[k] = str(o[k])
    except Exception:
        pass
    return out


def save_creds(doc: dict) -> bool:
    """把凭证写进 okx.key(DPAPI 加密)。返回是否成功。"""
    payload = {k: str((doc or {}).get(k) or "").strip() for k in FIELDS}
    if not any(payload.values()):
        try:
            OKX_KEY_PATH.write_bytes(b"")
        except Exception:
            return False
        return True
    OKX_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    try:
        OKX_KEY_PATH.write_bytes(secure.protect(text))
    except Exception:
        return False
    try:
        os.chmod(OKX_KEY_PATH, 0o600)
    except Exception:
        pass
    return True


def has_creds() -> bool:
    c = load_creds()
    return bool(c["api_key"] and c["api_secret"] and c["passphrase"])


def clear_config_plaintext() -> str:
    """把 config.json 里的 OKX 明文字段清空。返回说明。"""
    from ..config import cfg
    o = cfg().get("broker.okx", {}) or {}
    had = [k for k in FIELDS if o.get(k)]
    if not had:
        return "config.json 里没有 OKX 明文字段"
    for k in FIELDS:
        cfg().set(f"broker.okx.{k}", "")
    cfg().save()
    return f"已清空 config.json 的 {', '.join(had)}(凭证在 okx.key 里)"
