# -*- coding: utf-8 -*-
"""扫本仓库所有已跟踪文件的明文凭证。"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quantlab.security import secret_scan as ss  # noqa: E402

# 想顺便扫别的仓库,用环境变量追加:(分号分隔的绝对路径)
EXTRA = [p for p in (os.environ.get("SCAN_SECRETS_EXTRA") or "").split(os.pathsep) if p.strip()]

REPOS = [ROOT] + [Path(p) for p in EXTRA]

total = 0
for repo in REPOS:
    if not repo.exists():
        continue
    try:
        out = subprocess.run(["git", "ls-files"], cwd=repo,
                             capture_output=True, encoding="utf-8",
                             errors="ignore", timeout=120)
        files = [f for f in out.stdout.splitlines() if f.strip()]
    except Exception as e:  # noqa: BLE001
        print(f"{repo.name}: git ls-files 失败 {e}")
        continue
    print(f"\n=== {repo.name}: {len(files)} 个已跟踪文件 ===")
    hits_total = 0
    for f in files:
        p = repo / f
        if not p.is_file():
            continue
        hits = ss.scan_file(str(p))
        if not hits:
            continue
        hits_total += len(hits)
        print(f"  ✗ {f}")
        for h in hits[:3]:
            print(f"      第 {h['line']} 行 [{h['name']}] {h['snippet'][:70]}")
    print(f"  命中 {hits_total} 处" if hits_total else "  干净")
    total += hits_total

print(f"\n合计命中 {total} 处")
raise SystemExit(0 if total == 0 else 1)
