#!/bin/sh
# 提交前扫明文凭证。由 tools/install_secret_hook.py 安装。
#
# 为什么要有:2026-11-26 一次 git add -A 把含 OKX 明文 api_key/api_secret
# 的 config.json.bak 推到了 GitHub,事后清历史 + 轮换密钥很麻烦。
#
# 命中就拒绝提交,并打印改法。误报可以用 git commit --no-verify 跳过,
# 但请先确认真的不是凭证。

files=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$files" ] && exit 0

# 用项目根目录的 python 跑扫描
if [ -f "quantlab/security/secret_scan.py" ]; then
    python -m quantlab.security.secret_scan $files
    exit $?
fi

exit 0
