# -*- coding: utf-8 -*-
"""凭证泄露扫描 —— pre-commit 钩子和手动检查共用。

为什么要有:2026-11-26 一次 `git add -A` 把含 OKX 明文 api_key/api_secret
的 config.json.bak 推到了 GitHub。事后清历史很麻烦,而且密钥必须轮换。

这个模块只做一件事:给定文本,判断里面有没有**看起来像真凭证**的东西。

设计原则:
  宁可漏一点点,不要天天误报 —— 误报会让人直接绕开钩子。
  所以只匹配「高置信度」模式,并排除注释、占位符、示例值。
"""
from __future__ import annotations

import re

# 高置信度模式:(名字, 正则, 说明)
PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("okx_secret",
     re.compile(r"(?i)(api[_-]?secret|secret[_-]?key)\s*[:=]\s*[\"']?([0-9A-F]{32})"),
     "32 位大写 hex,长度和字符集都对得上 OKX secret"),
    ("okx_key",
     re.compile(r"(?i)(api[_-]?key)\s*[:=]\s*[\"']?([0-9a-f]{8}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
     "UUID 形式的 api_key"),
    ("openai_sk",
     re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
     "OpenAI 风格 sk- 开头"),
    ("gray_gk",
     re.compile(r"\bgk_[0-9a-f]{16,}\b"),
     "Gray 服务端签发的 gk_ key"),
    ("dpapi_blob",
     re.compile(r"\bdpapi1:[A-Za-z0-9+/=]{40,}"),
     "DPAPI 加密串(本机可解,泄露等于泄露)"),
    ("aws_key",
     re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
     "AWS Access Key"),
    ("private_key",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
     "私钥文件头"),
    ("telegram_bot",
     re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
     "Telegram bot token"),
]

# 这些行整行跳过(注释、文档示例)
SKIP_LINE = re.compile(
    r"^\s*(#|//|/\*|\*|<!--)"          # 注释
    r"|placeholder|example|your[_-]?key|xxx+|\*\*\*+|\.\.\.|省略|示例|占位",
    re.I)

# 这些文件整份跳过
SKIP_EXT = {".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif",
            ".pdf", ".zip", ".gz", ".whl", ".pyc"}

# 这些文件整份跳过:扫描器自己 + 它的测试里必然有假密钥样本,
# 不跳过的话 pre-commit 钩子会拦下自己的源码(踩过一次)
SKIP_NAMES = {
    "secret_scan.py",
    "test_secret_scan.py",
}


def looks_placeholder(val: str) -> bool:
    """占位符:全同一个字符、太短、明显是模板。"""
    v = val.strip().strip("\"'")
    if len(v) < 16:
        return True
    if len(set(v)) <= 3:                    # aaaa / 0000 / abab
        return True
    low = v.lower()
    for w in ("your", "example", "placeholder", "changeme", "todo",
              "xxxx", "****", "secret", "test"):
        if w in low:
            return True
    return False


def scan_text(text: str, path: str = "") -> list[dict]:
    """扫一段文本,返回命中列表 [{line, name, why, snippet}]。"""
    if path:
        p = path.replace("\\", "/")
        if p.rsplit("/", 1)[-1] in SKIP_NAMES:
            return []
        ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p else ""
        if ext in SKIP_EXT:
            return []
    out: list[dict] = []
    for i, line in enumerate(text.splitlines(), 1):
        if SKIP_LINE.search(line):
            continue
        for name, pat, why in PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            # 取匹配到的值部分做占位符判断
            val = m.group(m.lastindex) if m.lastindex else m.group(0)
            if looks_placeholder(val):
                continue
            # 打码:只留前 6 位
            mask = val[:6] + "*" * max(0, len(val) - 6)
            out.append({"line": i, "name": name, "why": why,
                        "snippet": line.strip()[:90].replace(val, mask)})
    return out


def scan_file(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return scan_text(f.read(), path)
    except Exception:  # noqa: BLE001
        return []


def main(argv: list[str] | None = None) -> int:
    import sys

    files = argv if argv is not None else sys.argv[1:]
    if not files:
        print("用法: python -m quantlab.security.secret_scan <文件...>")
        return 0
    bad = 0
    for f in files:
        hits = scan_file(f)
        if not hits:
            continue
        bad += 1
        print(f"\n[!] {f}")
        for h in hits:
            print(f"  第 {h['line']} 行  [{h['name']}] {h['why']}")
            print(f"    {h['snippet']}")
    if bad:
        print(f"\n{bad} 个文件命中。凭证不要进库:")
        print("  1) 密钥放独立文件(如 cloud.key),用 DPAPI 加密")
        print("  2) 配置备份加进 .gitignore")
        print("  3) 已经提交过的,清历史 + 轮换密钥")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
