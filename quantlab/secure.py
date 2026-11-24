# -*- coding: utf-8 -*-
"""本地敏感数据的加解密。

分两层:
  1. **凭据字段**(config 里的 API key / 本机接口 token 等):Windows 用 DPAPI
     (CryptProtectData,绑定当前用户),其他平台退回到 USER_DIR/.field.key 的
     Fernet 密钥(文件权限 600)。加密后的字符串带 `dpapi1:` / `fernet1:` 前缀,
     读的时候透明还原。
  2. **整目录保险箱**(config + data 目录):AES-256-GCM 分块流式加密,口令用
     scrypt 派生,适合把不运行时的数据打包成一个 .vault 文件。见 tools/secure_data.py。

不做的事:运行中的 SQLite 仍是明文(SQLCipher 需要换驱动,风险大),交易数据
靠「不跑时打包加密」保护;忘了保险箱口令就解不开,没有后门。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

MAGIC = b"GRAY1:"
TEXT_DPAPI = "dpapi1:"
TEXT_FERNET = "fernet1:"
VAULT_MAGIC = b"GRAYVAULT1"

_FIELD_KEY_NAME = ".field.key"


# ---------------------------------------------------------------- DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _backend():
    if sys.platform != "win32":
        return None
    try:
        return ctypes.windll.crypt32, ctypes.windll.kernel32
    except Exception:  # noqa: BLE001
        return None


def _to_blob(data: bytes) -> tuple[object, _Blob]:
    buf = ctypes.create_string_buffer(data, len(data))
    return buf, _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _take(blob: _Blob, kernel32) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32.LocalFree(blob.pbData)


def protect(text: str) -> bytes:
    """DPAPI 加密一段文本(cloud.key 用的就是这个)。不支持时抛 OSError。"""
    pair = _backend()
    if pair is None:
        raise OSError("当前平台不支持 DPAPI")
    crypt32, kernel32 = pair
    buf, blob_in = _to_blob((text or "").encode("utf-8"))
    blob_out = _Blob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01,
        ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptProtectData 失败:{ctypes.get_last_error()}")
    try:
        return MAGIC + _take(blob_out, kernel32)
    finally:
        del buf


def unprotect(raw: bytes) -> str:
    """DPAPI 解密。不支持或数据损坏时抛 OSError。"""
    pair = _backend()
    if pair is None:
        raise OSError("当前平台不支持 DPAPI")
    crypt32, kernel32 = pair
    body = raw[len(MAGIC):] if raw.startswith(MAGIC) else raw
    buf, blob_in = _to_blob(body)
    blob_out = _Blob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01,
        ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"CryptUnprotectData 失败:{ctypes.get_last_error()}")
    try:
        return _take(blob_out, kernel32).decode("utf-8", "replace")
    finally:
        del buf


def available() -> bool:
    return _backend() is not None


# ---------------------------------------------------------------- 凭据字段
def _field_key() -> bytes:
    from cryptography.fernet import Fernet
    path = _user_dir() / _FIELD_KEY_NAME
    if not path.exists():
        key = Fernet.generate_key()
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    return path.read_bytes()


def _user_dir() -> Path:
    from .config import USER_DIR
    return Path(USER_DIR)


def protect_text(text: str) -> str:
    """加密一个配置字段,返回可写进 JSON 的字符串。

    Windows 优先 DPAPI;非 Windows 用 .field.key 的 Fernet。
    空串原样返回(别把空凭据加密成非空)。
    """
    if not text:
        return ""
    if available():
        return TEXT_DPAPI + base64.b64encode(protect(text)).decode("ascii")
    from cryptography.fernet import Fernet
    token = Fernet(_field_key()).encrypt(text.encode("utf-8"))
    return TEXT_FERNET + token.decode("ascii")


def unprotect_text(value: str) -> str:
    """还原 protect_text 的结果;不是密文(旧明文)就原样返回。"""
    if not isinstance(value, str):
        return value
    if value.startswith(TEXT_DPAPI):
        try:
            raw = base64.b64decode(value[len(TEXT_DPAPI):])
            return unprotect(raw)
        except Exception:  # noqa: BLE001
            return ""
    if value.startswith(TEXT_FERNET):
        try:
            from cryptography.fernet import Fernet, InvalidToken
            try:
                return Fernet(_field_key()).decrypt(
                    value[len(TEXT_FERNET):].encode("ascii")).decode("utf-8")
            except InvalidToken:
                return ""
        except Exception:  # noqa: BLE001
            return ""
    return value


def is_protected(value) -> bool:
    return isinstance(value, str) and value.startswith((TEXT_DPAPI, TEXT_FERNET))


# ---------------------------------------------------------------- 整目录保险箱
def _derive(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1).derive(
        password.encode("utf-8"))


def encrypt_path(src: Path, dst: Path, password: str,
                 skip: tuple[str, ...] = ("__pycache__",)) -> dict:
    """把目录(src)加密成单个 .vault 文件。

    格式:GRAYVAULT1 | salt(16) | nonce(12) | 清单块 | 文件块序列。
    第一块是 JSON 清单(相对路径 + 大小),之后按清单顺序流式写文件内容,
    内存占用只跟单块大小有关,适合几 GB 的库。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    src = Path(src)
    files = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src).as_posix()
        if any(part in skip for part in p.parts):
            continue
        files.append({"path": rel, "size": p.stat().st_size})

    salt = os.urandom(16)
    key = _derive(password, salt)
    aes = AESGCM(key)
    nonce = os.urandom(12)
    manifest = json.dumps({"files": files}, ensure_ascii=False).encode("utf-8")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with open(tmp, "wb") as out:
        out.write(VAULT_MAGIC)
        out.write(salt)
        out.write(nonce)
        ct = aes.encrypt(nonce, manifest, None)
        out.write(len(ct).to_bytes(4, "big"))
        out.write(ct)
        for f in files:
            p = src / f["path"]
            with open(p, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    nonce = os.urandom(12)
                    blob = aes.encrypt(nonce, chunk, None)
                    out.write(nonce)
                    out.write(len(blob).to_bytes(4, "big"))
                    out.write(blob)
    tmp.replace(dst)
    return {"files": len(files), "bytes": dst.stat().st_size}


def decrypt_path(vault: Path, dst: Path, password: str) -> dict:
    """还原保险箱到目录;目标文件已存在会覆盖。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data = Path(vault).read_bytes()
    if not data.startswith(VAULT_MAGIC):
        raise ValueError("不是本程序的保险箱文件")
    pos = len(VAULT_MAGIC)
    salt, nonce = data[pos:pos + 16], data[pos + 16:pos + 28]
    pos += 28
    n = int.from_bytes(data[pos:pos + 4], "big")
    pos += 4
    key = _derive(password, salt)
    aes = AESGCM(key)
    manifest = json.loads(aes.decrypt(nonce, data[pos:pos + n], None))
    pos += n
    dst = Path(dst)
    count = 0
    for f in manifest.get("files") or []:
        target = dst / f["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        remaining = int(f["size"])
        with open(target, "wb") as out:
            while remaining > 0:
                nonce = data[pos:pos + 12]
                pos += 12
                n = int.from_bytes(data[pos:pos + 4], "big")
                pos += 4
                blob = data[pos:pos + n]
                pos += n
                plain = aes.decrypt(nonce, blob, None)
                out.write(plain)
                remaining -= len(plain)
        count += 1
    return {"files": count}
