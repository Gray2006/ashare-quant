# -*- coding: utf-8 -*-
"""每日维护:一个入口把该做的事按顺序跑完。

被 Windows 计划任务调用(19:00 / 21:30),也可以手动跑。

做三件事,互不阻塞 —— 前一件失败不影响后一件:
  1. 日线快照补全(sync_snapshot):全市场当日 OHLC 一次入库。
     不做的话,没人点开的票永远不更新,选股池会慢慢缩到几百只。
  2. 龙虎榜采集 + 预测 + 验证(daily_lhb 的逻辑)
  3. 因子库重审(factor_evolve.evolve):把衰减的标出来,新因子定级

用法:
    python tools/daily_maintain.py                # 三步都跑
    python tools/daily_maintain.py --skip-factors  # 只补数据
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def log(msg: str, fp=None) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if fp is not None:
        fp.write(line + "\n")
        fp.flush()


def step_snapshot(fp) -> None:
    log("① 补全当日日线…", fp)
    try:
        from tools.sync_snapshot import snapshot_bars

        from quantlab.data.kline import _last_trade_day
        from quantlab.data.store import store

        # 短路:当日已经补过就不重复拉(非交易日跑会拿到同一天的数据)
        st0 = store()
        day0 = _last_trade_day()
        have0 = st0.conn.execute(
            "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
            (day0,)).fetchone()[0]
        if have0 >= 3000:
            log(f"  {day0} 已有 {have0} 只,跳过", fp)
            return

        bars = snapshot_bars(pages=60, sleep=0.2)
        if not bars:
            log("  没拿到快照(非交易日?)", fp)
            return
        day = next(iter(bars.values()))["datetime"]
        st = store()
        before = st.conn.execute(
            "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
            (day,)).fetchone()[0]
        ok = 0
        for code, bar in bars.items():
            try:
                st.save_kline(code, 101, 1, [bar])
                ok += 1
            except Exception:  # noqa: BLE001
                pass
        after = st.conn.execute(
            "select count(*) from kline_cache where klt=101 and fqt=1 and bar_time=?",
            (day,)).fetchone()[0]
        log(f"  {day}:{before} → {after} 只(写入 {ok})", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_lhb(fp) -> None:
    log("② 龙虎榜采集 / 预测 / 验证…", fp)
    try:
        from quantlab.engine import hotmoney as hm
        out = hm.sync(days=3, max_stocks=12)
        acc = out["accuracy"]
        log(f"  采集 {out['collect']['days']} 天 {out['collect']['rows']} 条;"
            f"预测 {out['predict_rows']} 条;验证 {out['review']['reviewed']} 条;"
            f"准确率 {acc['acc']:.1%}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_factors(fp, limit: int = 8) -> None:
    log(f"③ 因子库重审(最多 {limit} 条)…", fp)
    try:
        from quantlab.engine import factor_evolve as fe
        from quantlab.engine import factor_mine as fm
        lib = fm.load_library()
        if not lib:
            log("  库是空的,跳过", fp)
            return
        res = fe.evolve(limit=limit)
        log(f"  重审 {res.get('n', 0)} 条:{res.get('counts')}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_verify(fp) -> None:
    log("④ 管道自检(偷看未来的因子应该被识别)…", fp)
    try:
        from quantlab.engine import factor_analysis as fa
        from quantlab.engine import factor_mine as fm
        panel = fm._panel(days=200, max_stocks=200, market="a")
        frames = fm._frames(panel)
        fwd = fm._fwd_panel(panel, 5)
        rep = fa.audit_scores(frames, fwd, horizon=5)
        t = (rep.get("raw") or {}).get("excess_t") or -9
        log(f"  偷看未来 t={t:.2f} -> " + ("正常" if t > 5 else "⚠ 管道可能有问题"), fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:100]}", fp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--skip-lhb", action="store_true")
    ap.add_argument("--skip-factors", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--factor-limit", type=int, default=8)
    args = ap.parse_args()

    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    fname = time.strftime("maintain-%Y-%m-%d.log")
    fp = open(logdir / fname, "a", encoding="utf-8")
    try:
        if not args.skip_snapshot:
            step_snapshot(fp)
        if not args.skip_lhb:
            step_lhb(fp)
        if not args.skip_factors:
            step_factors(fp, args.factor_limit)
        if not args.skip_verify:
            step_verify(fp)
        log("完成", fp)
        return 0
    finally:
        fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
