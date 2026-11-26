# -*- coding: utf-8 -*-
"""安装/卸载 pre-commit 凭证扫描钩子。

用法:
    python tools/install_secret_hook.py          # 装
    python tools/install_secret_hook.py --remove # 卸
    python tools/install_secret_hook.py --status # 看装没装

会写到 .git/hooks/pre-commit(两个仓库都装)。
"""
from __future__ import annotations

import argparse
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOS = [ROOT, Path(r"G:\DeepSeek\工作区\ashare-quant")]
SRC = ROOT / "tools" / "pre-commit-secret.sh"

MARK = "# 提交前扫明文凭证"


def _hook_path(repo: Path) -> Path:
    return repo / ".git" / "hooks" / "pre-commit"


def install(repo: Path) -> str:
    if not (repo / ".git").exists():
        return f"跳过 {repo.name}(不是 git 仓库)"
    dst = _hook_path(repo)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and MARK not in dst.read_text(encoding="utf-8", errors="ignore"):
        bak = dst.with_suffix(".bak")
        shutil.copy2(dst, bak)
        return f"{repo.name}: 已有别的 pre-commit,备份到 {bak.name},未覆盖"
    shutil.copy2(SRC, dst)
    dst.chmod(dst.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f"{repo.name}: 已装"


def remove(repo: Path) -> str:
    dst = _hook_path(repo)
    if dst.exists() and MARK in dst.read_text(encoding="utf-8", errors="ignore"):
        dst.unlink()
        return f"{repo.name}: 已卸"
    return f"{repo.name}: 没装"


def status(repo: Path) -> str:
    dst = _hook_path(repo)
    if not dst.exists():
        return f"{repo.name}: 未装"
    txt = dst.read_text(encoding="utf-8", errors="ignore")
    return f"{repo.name}: {'已装' if MARK in txt else '装了别的钩子'}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    for repo in REPOS:
        if args.status:
            print(status(repo))
        elif args.remove:
            print(remove(repo))
        else:
            print(install(repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
