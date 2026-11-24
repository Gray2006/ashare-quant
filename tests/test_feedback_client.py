# -*- coding: utf-8 -*-
"""公开版数据回流客户端测试(离线,不发网络请求)。

    python tests/test_feedback_client.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["GRAY_HOME"] = tempfile.mkdtemp(prefix="gray-fbc-")

from quantlab import cloud, feedback  # noqa: E402
from quantlab.config import cfg  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def _set(**kw):
    for k, v in kw.items():
        cfg().set(k, v)


print("== 默认关闭 ==")
ok("默认不启用", feedback.enabled() is False and cloud.enabled() is False)
ok("关闭时 push 不落盘",
   feedback.push("backtest", "a", {"metrics": {"sharpe": 1.2}}) is False
   and not (Path(os.environ["GRAY_HOME"]) / "feedback" / "spool.jsonl").exists())

print("== 开启后脱敏入队 ==")
_set(**{"cloud.enabled": True, "cloud.base_url": "https://example.com",
        "cloud.key": "k-test", "feedback.enabled": True})
ok("开启后可用", feedback.enabled() is True)
payload = {"metrics": {"sharpe": 1.2, "total_return_pct": 8.5},
           "params": {"per_position_pct": 0.15},
           "account_no": "123456", "api_key": "secret",
           "cash": 1000000, "note": "ok"}
ok("push 成功", feedback.push("backtest", "a", payload, strategy="multifactor") is True)
spool = Path(os.environ["GRAY_HOME"]) / "feedback" / "spool.jsonl"
ok("spool 已落盘", spool.exists())
raw = spool.read_text(encoding="utf-8")
ev = json.loads(raw.splitlines()[-1])
ok("敏感键被剔除",
   "account_no" not in ev["payload"] and "api_key" not in ev["payload"]
   and "cash" not in ev["payload"], ev["payload"])
ok("业务字段保留", ev["payload"]["metrics"]["sharpe"] == 1.2)
ok("渠道标记为 public", ev["channel"] == "public")

print("== 上传成功/失败 ==")
calls = []


def fake_ok(events, timeout=20):
    calls.append(events)
    return {"ok": True, "accepted": len(events)}


def fake_fail(events, timeout=20):
    return {"ok": False, "error": "503"}


cloud.feedback_push = fake_ok
r = feedback.flush()
ok("成功后 spool 清空", r["sent"] == 1 and feedback.state()["queued"] == 0, r)

feedback.push("backtest", "a", {"metrics": {"sharpe": 0.9}})
cloud.feedback_push = fake_fail
r2 = feedback.flush()
ok("失败保留队列", r2.get("error") and feedback.state()["queued"] == 1, r2)

print("== 地址校验 ==")
def raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


ok("拒绝 http 公网", raises(lambda: cloud._check_url("http://example.com/x")))
ok("允许 https", cloud._check_url("https://example.com/x") is None)
ok("允许本机 http", cloud._check_url("http://127.0.0.1:18080/x") is None)

print(f"\n===== 通过 {PASS} / 失败 {FAIL} =====")
def test_script_assertions_passed():
    """import 期已跑完脚本断言;包一层让 pytest 能收集。"""
    assert FAIL == 0, f"{FAIL} 项脚本断言失败"


if __name__ == "__main__":
    raise SystemExit(0 if FAIL == 0 else 1)