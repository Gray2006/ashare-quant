# -*- coding: utf-8 -*-
"""每交易日盘后采集龙虎榜席位,并把预测/验证闭环跑一遍。

被 Windows 计划任务调用,也可以手动跑:
    python tools/daily_lhb.py              # 补最近 3 个自然日
    python tools/daily_lhb.py --days 7     # 补一周
    python tools/daily_lhb.py --scan       # 采完顺手扫一遍席位反向价值

设计取舍:
  · 不做交易日历判断 —— 非交易日拉一次接口返回空,代价很小,
    比「判断错了漏采一天」划算。
  · 每次都补最近几天,不假设只跑了一次:机器可能关机、断网。
  · 日志写 logs/lhb-YYYY-MM-DD.log,计划任务的输出也重定向到那里。
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--max-stocks", type=int, default=20)
    ap.add_argument("--scan", action="store_true",
                    help="采完顺手跑反向价值扫描(慢,可选)")
    args = ap.parse_args()

    from quantlab.engine import hotmoney as hm

    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    fname = time.strftime("lhb-%Y-%m-%d.log")
    fp = open(logdir / fname, "a", encoding="utf-8")

    try:
        before = hm.coverage(90)
        log(f"采集前:最新 {before['latest'] or '-'} · {before['n']} 条", fp)

        out = hm.sync(days=args.days, max_stocks=args.max_stocks)
        after = hm.coverage(90)
        real_new = max(0, after["n"] - before["n"])
        log(f"采集 {out['collect']['days']} 天 / 写入 {out['collect']['rows']} 条"
            f"(净增 {real_new} 条,其余是覆盖已有记录);回填 {out['filled']} 条", fp)
        log(f"预测 {out['predict_days']} 天 / {out['predict_rows']} 条;"
            f"验证 {out['review']['reviewed']} 条", fp)
        acc = out["accuracy"]
        log(f"准确率 {acc['acc']:.1%}({acc['n']} 条)"
            f" · 明确方向 {acc['acc_dir']:.1%}({acc['n_dir']} 条)", fp)

        log(f"采集后:最新 {after['latest']} · {after['n']} 条", fp)

        if real_new == 0:
            log("本次没有新增(非交易日或接口没更新)", fp)

        # ---- 数据量够不够做验证:看「既有 5 日收益、又能算出市场基准」的天数
        # 覆盖 35 天 ≠ 可验证 35 天:后面的日期还没走完 5 个交易日,基准也算不出来
        try:
            from quantlab.engine import seat_profile as sp
            c = hm._db()
            try:
                day_rows = [str(r[0]) for r in c.execute(
                    "select distinct day from lhb_seats "
                    "where day >= date('now','-120 day') order by day")]
                n_seats = c.execute(
                    "select count(distinct seat) from lhb_seats "
                    "where day >= date('now','-120 day')").fetchone()[0]
            finally:
                c.close()
            bench = sp.market_bench(day_rows)
            n_valid = sum(1 for v in bench.values() if v is not None)
            log(f"累计覆盖 {len(day_rows)} 个交易日 / {int(n_seats)} 个席位;"
                f"其中 {n_valid} 天可验证(有 5 日收益 + 市场基准)", fp)
            if n_valid >= 30:
                flag = ROOT / "reports" / "lhb-ready.flag"
                flag.parent.mkdir(exist_ok=True)
                if not flag.exists():
                    flag.write_text(
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} 已有 {n_valid} 个交易日"
                        f"可验证,可以重跑反向验证:\n"
                        f"  python tools/seat_reverse_scan.py\n"
                        f"  python tools/seat_reports.py\n",
                        encoding="utf-8")
                    log("★ 可验证天数已够 30,可以重跑反向验证(reports/lhb-ready.flag)",
                        fp)
        except Exception as e:  # noqa: BLE001
            log(f"就绪检查失败:{str(e)[:60]}", fp)

        if args.scan:
            try:
                from quantlab.engine import seat_profile as sp
                stats = hm.seat_stats(min_samples=3, days=60)
                rows = []
                for s in stats:
                    if any(k in s["seat"] for k in ("专用",)):
                        continue
                    p = sp.profile(s["seat"], enrich=False)
                    if p.get("ok") and p["n"] >= 30 and p["n_days"] >= 10:
                        rows.append(p)
                rows.sort(key=lambda r: -r["reverse_t"])
                strong = [r for r in rows if r["reverse_t"] >= 2]
                log(f"样本够的席位 {len(rows)} 个,反向 t≥2 的 {len(strong)} 个", fp)
            except Exception as e:  # noqa: BLE001
                log(f"扫描失败:{str(e)[:80]}", fp)

        return 0
    except Exception as e:  # noqa: BLE001
        log(f"失败:{type(e).__name__}: {str(e)[:200]}", fp)
        return 1
    finally:
        fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
