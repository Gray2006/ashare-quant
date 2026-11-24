import os, sys, traceback, json
os.environ.pop("GRAY_HOME", None)
sys.path.insert(0, os.environ["AQ_PUB"])
from quantlab.engine import factor_mine as fm
try:
    r = fm.iter_mine(days=120, max_stocks=30, horizon=5, market="a", rounds=1)
    print("ok:", r.get("ok"), "error:", r.get("error"), "rows:", len(r.get("rows") or []), flush=True)
    rows = r.get("rows") or []
    if rows:
        print("first:", json.dumps({k: rows[0].get(k) for k in ("label","expr","ic","icir","coverage","mono","oos_ok")}, ensure_ascii=False), flush=True)
except Exception:
    traceback.print_exc()
    print("FAILED", flush=True)
