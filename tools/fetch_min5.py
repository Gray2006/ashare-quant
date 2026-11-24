# -*- coding: utf-8 -*-
"""拉 5 分钟线,存进 kline_cache(klt=5,fqt=1)。

东财 push2his 拉分钟线会被掐(实测 RemoteDisconnected),新浪这个接口还通:
  https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData
  ?symbol=sh600519&scale=5&ma=no&datalen=1023
单次最多 1023 根,5 分钟线约 21 个交易日。历史攒不回来,只能每天增量补 ——
所以做T的长期回测仍要走日线近似,分钟线用来校准日线假设。

用法:
    python tools/fetch_min5.py                 # 默认一批流动性好的票
    python tools/fetch_min5.py --codes 600519,000001 --datalen 1023
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quantlab.data import http  # noqa: E402
from quantlab.data.store import store  # noqa: E402

URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
       "CN_MarketDataService.getKLineData")
HEADERS = {"Referer": "https://finance.sina.com.cn",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

DEFAULT_CODES = [
    "600519", "601318", "600036", "600030", "601888",
    "000001", "000002", "000333", "002415", "002594",
    "300750", "300059", "300760", "688981", "688111",
]


def sina_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def fetch(code: str, datalen: int = 1023) -> list[dict]:
    txt = http.get_text(URL, params={"symbol": sina_symbol(code), "scale": "5",
                                     "ma": "no", "datalen": str(datalen)},
                        headers=HEADERS, timeout=8.0, retries=2)
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b < 0:
        raise ValueError(f"{code} 返回不是 JSONP 数组")
    raw = json.loads(txt[a:b + 1])
    bars = []
    for it in raw:
        day = str(it.get("day") or "")
        if len(day) < 16:
            continue
        bars.append({"datetime": day,
                     "open": float(it["open"]), "high": float(it["high"]),
                     "low": float(it["low"]), "close": float(it["close"]),
                     "volume": float(it.get("volume") or 0),
                     "amount": float(it.get("volume") or 0) * float(it["close"])})
    return bars


def top_liquid(n: int) -> list[str]:
    """按最近一个交易日的成交额取流动性最好的 n 只(只取有日线的)。"""
    st = store()
    day = st.conn.execute(
        "select max(bar_time) from kline_cache where klt=101 and fqt=1").fetchone()[0]
    if not day:
        return []
    rows = st.conn.execute(
        "select code, sum(coalesce(amount,0)) amt from kline_cache "
        "where klt=101 and fqt=1 and bar_time=? group by code "
        "order by amt desc limit ?", (day, n)).fetchall()
    return [r["code"] for r in rows if r["amt"] and r["amt"] > 0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--datalen", type=int, default=1023)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--top", type=int, default=0, help="按成交额取前 N 只")
    args = ap.parse_args()
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    if not codes and args.top > 0:
        codes = top_liquid(args.top)
        print(f"按成交额取 {len(codes)} 只", flush=True)
    if not codes:
        codes = DEFAULT_CODES

    st = store()
    ok, fail = 0, []
    for i, code in enumerate(codes, 1):
        try:
            bars = fetch(code, args.datalen)
            st.save_kline(code, 5, 1, bars)
            ok += 1
            print(f"[{i}/{len(codes)}] {code} {len(bars)} 根 "
                  f"{bars[0]['datetime']} → {bars[-1]['datetime']}", flush=True)
        except Exception as e:  # noqa: BLE001
            fail.append(code)
            print(f"[{i}/{len(codes)}] {code} 失败 {str(e)[:80]}", flush=True)
        time.sleep(args.sleep)
    print(f"完成 {ok}/{len(codes)}，失败 {fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
