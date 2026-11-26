# -*- coding: utf-8 -*-
"""安全提交:避免 `git add -A` 把不该进库的东西带进去。

为什么要有:2026-11-26 两次事故
  1 config.json.bak(含 OKX 明文密钥)被 add -A 推上 GitHub
  2 _smoke.txt(冒烟测试产物)被 add -A 带进去

做法:
  1 只自动加**已跟踪文件**的修改(git add -u)
  2 未跟踪文件逐个列出来,要显式 --new 才加
  3 提交前跑凭证扫描,命中就拒绝
  4 打印最终会提交的文件清单,确认后再提交

用法:
    python tools/safe_commit.py -m "提交说明"
    python tools/safe_commit.py -m "..." --new tools/foo.py tests/test_foo.py
    python tools/safe_commit.py -m "..." --dry-run
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.security import secret_scan as ss  # noqa: E402


def _git(*args: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       encoding="utf-8", errors="ignore")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _status() -> tuple[list[str], list[str]]:
    """返回 (已跟踪的修改, 未跟踪的新文件)。"""
    _, out = _git("status", "--porcelain")
    tracked, untracked = [], []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if code.strip() == "??":
            untracked.append(path)
        else:
            tracked.append(path)
    return tracked, untracked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("--new", nargs="*", default=[],
                    help="要一起提交的新文件(必须显式列出)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tracked, untracked = _status()
    if not tracked and not untracked:
        print("没有要提交的改动")
        return 0

    print(f"已跟踪的修改 {len(tracked)} 个:")
    for f in tracked[:20]:
        print(f"  M {f}")
    if len(tracked) > 20:
        print(f"  ...还有 {len(tracked) - 20} 个")

    if untracked:
        print(f"\n未跟踪的新文件 {len(untracked)} 个(默认不加):")
        for f in untracked[:20]:
            mark = "✓" if f in args.new else " "
            print(f"  [{mark}] {f}")
        if len(untracked) > 20:
            print(f"  ...还有 {len(untracked) - 20} 个")

    # 要加的新文件必须真的存在且未跟踪
    bad_new = [f for f in args.new if f not in untracked]
    if bad_new:
        print(f"\n这些 --new 指定的文件不在未跟踪列表里: {bad_new}")
        return 1

    # 凭证扫描:扫所有将要提交的文件
    to_scan = [str(ROOT / f) for f in (tracked + list(args.new))
               if (ROOT / f).is_file()]
    hits = 0
    for f in to_scan:
        h = ss.scan_file(f)
        if h:
            hits += len(h)
            print(f"\n[!] {f}")
            for x in h[:3]:
                print(f"    第 {x['line']} 行 [{x['name']}] {x['snippet'][:70]}")
    if hits:
        print(f"\n凭证扫描命中 {hits} 处,拒绝提交。")
        return 1

    if args.dry_run:
        print(f"\n[dry-run] 会提交 {len(tracked) + len(args.new)} 个文件")
        return 0

    # 只加已跟踪的修改
    if tracked:
        rc, out = _git("add", "-u")
        if rc != 0:
            print(f"git add -u 失败: {out[:200]}")
            return 1
    # 显式指定的新文件
    if args.new:
        rc, out = _git("add", *args.new)
        if rc != 0:
            print(f"git add 新文件失败: {out[:200]}")
            return 1

    rc, out = _git("commit", "-m", args.message)
    print(out.strip()[:500])
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
