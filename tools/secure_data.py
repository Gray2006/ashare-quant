# -*- coding: utf-8 -*-
"""本地数据保险箱:把 config.json + data 目录加密成一个 .vault 文件。

为什么不在程序里实时加密 SQLite:换 SQLCipher 要换驱动、迁移库,风险大;
而运行中的库本来就在内存/文件里明文。这里解决的是「不跑的时候」:
关机前打包加密,要用时再解出来,加密文件放网盘/移动硬盘也不怕。

用法(先退出程序):
    py tools/secure_data.py status
    py tools/secure_data.py encrypt                 # → data/vault/backup-<时间>.vault
    py tools/secure_data.py encrypt --out D:\\bak\\q.vault
    py tools/secure_data.py decrypt D:\\bak\\q.vault            # 还原到用户目录
    py tools/secure_data.py decrypt q.vault --dst D:\\restore

口令忘了解不开;没有后门。加密算法:AES-256-GCM(分块流式),scrypt 派生密钥。
"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab import secure  # noqa: E402
from quantlab.config import CONFIG_PATH, DATA_DIR, USER_DIR  # noqa: E402


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _tree_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _ask_password(confirm: bool) -> str:
    pw = getpass.getpass("保险箱口令(至少 8 位): ")
    if len(pw) < 8:
        print("口令太短,至少 8 位")
        raise SystemExit(2)
    if confirm and getpass.getpass("再输一次: ") != pw:
        print("两次输入不一致")
        raise SystemExit(2)
    return pw


def cmd_status(_args) -> int:
    print(f"平台:{sys.platform}  DPAPI:{'可用' if secure.available() else '不可用'}")
    cfg_txt = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    marks = [m for m in ("dpapi1:", "fernet1:") if m in cfg_txt]
    print(f"配置文件:{CONFIG_PATH}  ({_human(CONFIG_PATH.stat().st_size) if CONFIG_PATH.exists() else '不存在'})")
    print(f"  凭据字段:{'已加密(' + '/'.join(marks) + ')' if marks else '明文或为空'}")
    print(f"数据目录:{DATA_DIR}  ({_human(_tree_size(DATA_DIR))})")
    vaults = sorted((USER_DIR / "vault").glob("*.vault")) if (USER_DIR / "vault").is_dir() else []
    print(f"已有保险箱:{len(vaults)} 个" + (f",最新 {vaults[-1].name}" if vaults else ""))
    return 0


def cmd_encrypt(args) -> int:
    src = Path(args.src) if args.src else USER_DIR
    if not Path(CONFIG_PATH).exists() and not Path(DATA_DIR).exists():
        print("没有可打包的数据(先运行一次程序)")
        return 1
    out = Path(args.out) if args.out else (
        Path(USER_DIR) / "vault" / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.vault")
    if out.exists() and not args.force:
        print(f"目标已存在:{out}(加 --force 覆盖)")
        return 1
    print(f"打包 {src} → {out}")
    print("提示:先退出程序,避免打包到写了一半的数据库")
    pw = _ask_password(confirm=True)
    info = secure.encrypt_path(src, out, pw)
    print(f"完成:{info['files']} 个文件,{_human(info['bytes'])}")
    print("原目录未删除;确认保险箱能解开后,可自行清理。")
    return 0


def cmd_decrypt(args) -> int:
    vault = Path(args.vault)
    if not vault.exists():
        print(f"找不到:{vault}")
        return 1
    dst = Path(args.dst) if args.dst else USER_DIR
    if dst.exists() and any(dst.iterdir()) and not args.force:
        print(f"目标目录非空:{dst}(加 --force 覆盖)")
        return 1
    pw = _ask_password(confirm=False)
    try:
        info = secure.decrypt_path(vault, dst, pw)
    except Exception as e:                       # noqa: BLE001
        print(f"解密失败(口令错或文件损坏):{e}")
        return 1
    print(f"完成:{info['files']} 个文件 → {dst}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="本地数据保险箱")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="看加密状态")
    s.set_defaults(fn=cmd_status)
    e = sub.add_parser("encrypt", help="把用户目录打包加密")
    e.add_argument("--src", default="")
    e.add_argument("--out", default="")
    e.add_argument("--force", action="store_true")
    e.set_defaults(fn=cmd_encrypt)
    d = sub.add_parser("decrypt", help="还原保险箱")
    d.add_argument("vault")
    d.add_argument("--dst", default="")
    d.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_decrypt)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
