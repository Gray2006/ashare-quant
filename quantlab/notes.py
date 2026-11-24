# -*- coding: utf-8 -*-
"""意见反馈:本地收件箱 + 可粘贴到 GitHub Issue 的正文。

不做静默上传。用户点「提交」只写本机 `USER_DIR/feedback/notes.jsonl`,
再自己决定是复制到剪贴板、发邮件,还是贴进仓库的 Issues。

写进去的东西可以带环境信息(版本/系统/界面缩放),但默认不勾选,
勾了也只带这些,不带股票代码、自选股、持仓、路径。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import sys
from pathlib import Path

from .config import USER_DIR
from .version import __version__

NOTES_DIR = Path(USER_DIR) / "feedback"
NOTES_PATH = NOTES_DIR / "notes.jsonl"


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def env_block() -> str:
    """给 issue 用的环境摘要,不含任何用户数据。"""
    import PySide6
    return "\n".join([
        f"- 版本: {__version__}",
        f"- 系统: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- Python: {sys.version.split()[0]}",
        f"- Qt: {PySide6.__version__}",
        f"- 运行方式: {'打包版' if getattr(sys, 'frozen', False) else '源码'}",
    ])


def build_text(body: str, contact: str = "", with_env: bool = False) -> str:
    """拼成一段可以直接贴进 Issue / 邮件的正文。"""
    parts = [body.strip()]
    if contact.strip():
        parts.append(f"联系方式: {contact.strip()}")
    if with_env:
        parts.append("环境:\n" + env_block())
    return "\n\n".join(p for p in parts if p)


def save(body: str, contact: str = "", with_env: bool = False) -> Path:
    """追加一条到本地收件箱,返回文件路径。"""
    text = body.strip()
    if not text:
        raise ValueError("先写点内容")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _now(),
        "version": __version__,
        "body": text[:8000],
        "contact": contact.strip()[:200],
        "env": env_block() if with_env else "",
    }
    with open(NOTES_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return NOTES_PATH


def count() -> int:
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def open_dir() -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(NOTES_DIR))  # noqa: S606
