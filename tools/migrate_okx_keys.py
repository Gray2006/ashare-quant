# -*- coding: utf-8 -*-
"""把 config.json 里的 OKX 明文凭证迁进 okx.key(DPAPI 加密)。

用法:
    python tools/migrate_okx_keys.py --dry-run   # 只看会做什么
    python tools/migrate_okx_keys.py             # 真迁
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.security import okx_creds as oc  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    c = oc.load_creds()
    have = [k for k, v in c.items() if v]
    print(f"读到的凭证字段: {have or '无'}")
    if not all(c.get(k) for k in oc.FIELDS):
        print("三个字段不齐(api_key / api_secret / passphrase),不迁移")
        return 1

    if args.dry_run:
        print(f"[dry-run] 会写入 {oc.OKX_KEY_PATH}")
        print("[dry-run] 会清空 config.json 里的 broker.okx.api_key/api_secret/passphrase")
        return 0

    ok = oc.save_creds(c)
    print(f"写入 {oc.OKX_KEY_PATH.name}: {'成功' if ok else '失败'}")
    if not ok:
        return 1
    msg = oc.clear_config_plaintext()
    print(msg)
    # 验证:重新读,且 config.json 里没明文了
    c2 = oc.load_creds()
    same = all(c2.get(k) == c.get(k) for k in oc.FIELDS)
    print(f"重新读一致: {'是' if same else '否'}")
    from quantlab.config import cfg
    o = cfg().get("broker.okx", {}) or {}
    left = [k for k in oc.FIELDS if o.get(k)]
    print(f"config.json 残留明文字段: {left or '无'}")
    return 0 if same and not left else 1


if __name__ == "__main__":
    sys.exit(main())
