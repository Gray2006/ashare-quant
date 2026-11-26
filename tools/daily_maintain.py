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


def step_config(fp) -> None:
    """⓪ 配置守护:强制修正关键配置项。

    2026-11-26 踩过:selflearn.target_hysteresis_pct 改成 0.15 后,
    某个进程内存里还是旧值,保存配置时又覆盖回 0.0。
    只改文件不够,得每次维护都跑一遍。
    """
    log("⓪ 配置守护…", fp)
    try:
        # 直接读写 config.json,不用 cfg() 单例 ——
        # 单例在 set+save 后同一进程内 get 仍可能拿到旧值(2026-11-26 实测)
        from tools.enforce_config import ENFORCE, _get, _load, _set
        data = _load()
        if not data:
            log("  config.json 读不到", fp)
            return
        bad = [(k, _get(data, k), w) for k, w, _ in ENFORCE
               if _get(data, k) != w]
        if not bad:
            log(f"  {len(ENFORCE)} 项全部正确", fp)
            return
        for k, got, want in bad:
            log(f"  {k}: {got!r} → {want!r}", fp)
            _set(data, k, want)
        from tools.enforce_config import CONFIG
        import json
        CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        log(f"  已修正 {len(bad)} 项", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  配置守护失败:{type(e).__name__}: {str(e)[:120]}", fp)


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


def step_min5(fp, top: int = 300) -> None:
    """补 5 分钟线。日内因子和做T参数学习都靠它。"""
    log(f"⑤ 补 5 分钟线(成交额前 {top} 只)…", fp)
    try:
        import time as _t

        from tools.fetch_min5 import (codes_with_min5, fetch, merge_sticky,
                                      top_liquid)

        from quantlab.data.store import store

        codes = top_liquid(top, skip_fetched=False)
        if not codes:
            log("  取不到票列表", fp)
            return
        # 并上已经有历史的票:流动性轮换会让人断档,覆盖不连贯
        sticky = codes_with_min5(min_days=5, limit=150)
        merged = merge_sticky(codes, sticky)
        if len(merged) > len(codes):
            log(f"  并上 {len(merged) - len(codes)} 只有历史的票(共 {len(merged)} 只)", fp)
        codes = merged
        st = store()
        ok, fail = 0, 0
        for i, code in enumerate(codes, 1):
            try:
                bars = fetch(code)
                if bars:
                    st.save_kline(code, 5, 1, bars)
                    ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
            if i % 50 == 0:
                log(f"  {i}/{len(codes)} 成功 {ok} 失败 {fail}", fp)
            _t.sleep(0.25)
        log(f"  完成:{ok}/{len(codes)}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_intraday(fp, top: int = 300) -> None:
    """补小时线(scale=60,约 1 年窗口)。默认不跑:日内结构空间已证否,
    只有还想继续挖这个信息集时才加 --intraday。"""
    log(f"⑥ 补小时线(成交额前 {top} 只)…", fp)
    try:
        import time as _t

        from tools.fetch_min5 import fetch, top_liquid

        from quantlab.data.store import store

        codes = top_liquid(top, skip_fetched=False, scale=60)
        if not codes:
            log("  取不到票列表", fp)
            return
        st = store()
        ok, fail = 0, 0
        for i, code in enumerate(codes, 1):
            try:
                bars = fetch(code, scale=60)
                if bars:
                    st.save_kline(code, 60, 1, bars)
                    ok += 1
            except Exception:  # noqa: BLE001
                fail += 1
            if i % 50 == 0:
                log(f"  {i}/{len(codes)} 成功 {ok} 失败 {fail}", fp)
            _t.sleep(0.3)
        log(f"  完成:{ok}/{len(codes)}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def pick_accounts(names: list[str], used: set[str]) -> list[str]:
    """要快照的账户:在用的那些。测试户(selftest/fee-test)自动排除。"""
    keep = []
    for n in names:
        n = str(n)
        if n in used and n not in ("selftest", "fee-test"):
            keep.append(n)
    return keep


def looks_reset(equity: float, market_value: float, initial: float,
                prev_equity: float | None) -> bool:
    """账户像被重置了吗:现金回到初始、无持仓,而昨天不是这个状态。

    模拟账户被测试代码删了重建时,现金会回到初始值、持仓清空 —— 这种快照
    写进权益曲线会把回撤/归因带偏,宁可跳过并告警。
    """
    if prev_equity is None:
        return False
    try:
        return (abs(float(equity) - float(initial)) < 1.0
                and abs(float(market_value)) < 1.0
                and abs(float(prev_equity) - float(initial)) > 1.0)
    except (TypeError, ValueError):
        return False


def step_equity(fp) -> None:
    """给模拟账户记一条当日权益快照(离线:持仓 + 收盘价)。"""
    log("⑨ 权益快照…", fp)
    try:
        from quantlab.broker.sim import SimGateway
        from quantlab.engine import daily_review as dr

        gw = SimGateway(strict_hours=False)
        # 只快照「有持仓」或「配置里在用的」账户,别把 selftest 之类的测试户也写进去
        from quantlab.config import cfg as _cfg
        from quantlab.data.store import store as _store
        try:
            used = {str(r["account"]) for r in _store().conn.execute(
                "SELECT DISTINCT account FROM positions WHERE qty>0").fetchall()}
        except Exception:  # noqa: BLE001
            used = set()
        cfg_acc = str((_cfg().get("autotrade") or {}).get("account") or "")
        if cfg_acc:
            used.add(cfg_acc)
        names = []
        try:
            raw = gw.list_accounts()
            flat = [str(a.get("name") if isinstance(a, dict) else a) for a in raw]
            names = pick_accounts(flat, used)
        except Exception:  # noqa: BLE001
            names = []
        if not names:
            log("  没有需要快照的账户,跳过", fp)
            return
        for name in names:
            try:
                gw.switch_account(name)
                acc = gw.account()
                try:
                    prev = _store().conn.execute(
                        "SELECT equity FROM sl_equity WHERE account=? "
                        "ORDER BY day DESC LIMIT 1", (name,)).fetchone()
                    prev_eq = float(prev["equity"]) if prev else None
                except Exception:  # noqa: BLE001
                    prev_eq = None
                if looks_reset(float(acc.get("total") or 0),
                               float(acc.get("market_value") or 0),
                               float(acc.get("initial") or 0), prev_eq):
                    log(f"  {name}:疑似账户被重置(现金回到初始、无持仓),"
                        f"跳过本次快照", fp)
                    continue
                dr.snapshot(account=name, equity=float(acc.get("total") or 0),
                            cash=float(acc.get("cash") or 0),
                            market_value=float(acc.get("market_value") or 0))
                log(f"  {name}:总 {float(acc.get('total') or 0):,.0f} "
                    f"现金 {float(acc.get('cash') or 0):,.0f}", fp)
            except Exception as e:  # noqa: BLE001
                log(f"  {name} 失败:{str(e)[:80]}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def backup_payload(account: str, acc: dict, positions: list[dict]) -> dict:
    """模拟账户当日快照(纯函数,便于测试)。"""
    return {"account": account,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cash": float(acc.get("cash") or 0),
            "market_value": float(acc.get("market_value") or 0),
            "total": float(acc.get("total") or 0),
            "positions": [{"code": p.get("code"), "name": p.get("name"),
                           "qty": int(p.get("qty") or 0),
                           "cost": float(p.get("cost") or 0)}
                          for p in positions]}


def prune_backups(folder, keep: int = 30) -> int:
    """只留最近 keep 份备份,返回删掉几个。"""
    from pathlib import Path as _P
    files = sorted(_P(folder).glob("sim-*.json"))
    drop = files[:-int(keep)] if int(keep) > 0 else []
    for f in drop:
        try:
            f.unlink()
        except Exception:  # noqa: BLE001
            pass
    return len(drop)


def cleanup_tranche_state(days: int = 7) -> int:
    """清掉过期的分批状态(账户被重置/换策略后留下的孤儿行)。"""
    from quantlab.data.store import store as _store
    cut = time.time() - int(days) * 86400
    try:
        with _store().conn as c:
            cur = c.execute("DELETE FROM sl_tranche WHERE updated < ?", (cut,))
            return int(cur.rowcount or 0)
    except Exception:  # noqa: BLE001
        return 0


def step_backup(fp) -> None:
    """把模拟账户的持仓和现金存一份,防止被误删/误重置后无从对账。"""
    log("⑩ 账户备份…", fp)
    try:
        import json as _json

        from quantlab.broker.sim import SimGateway
        from quantlab.config import cfg as _cfg
        from quantlab.data.store import store as _store

        gw = SimGateway(strict_hours=False)
        used = {str(r["account"]) for r in _store().conn.execute(
            "SELECT DISTINCT account FROM positions WHERE qty>0").fetchall()}
        cfg_acc = str((_cfg().get("autotrade") or {}).get("account") or "")
        if cfg_acc:
            used.add(cfg_acc)
        names = pick_accounts(gw.list_accounts(), used)
        if not names:
            log("  没有需要备份的账户,跳过", fp)
            return
        n = cleanup_tranche_state(7)
        if n:
            log(f"  清理过期分批状态 {n} 条", fp)
        folder = ROOT / "data" / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        for name in names:
            try:
                gw.switch_account(name)
                acc = gw.account()
                pos = [{"code": p.code, "name": p.name, "qty": p.qty,
                        "cost": p.cost} for p in gw.positions()]
                payload = backup_payload(name, acc, pos)
                (folder / f"sim-{day}-{name}.json").write_text(
                    _json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                log(f"  {name}:{len(pos)} 只持仓 / 总 {payload['total']:,.0f}", fp)
            except Exception as e:  # noqa: BLE001
                log(f"  {name} 备份失败:{str(e)[:80]}", fp)
        prune_backups(folder, keep=30)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_hypothesis(fp) -> None:
    """LLM 假设生成(默认关;config hypothesis.daily > 0 才跑)。"""
    log("⑧ LLM 假设生成…", fp)
    try:
        from quantlab.config import cfg
        n = int((cfg().get("hypothesis") or {}).get("daily") or 0)
        if n <= 0:
            log("  未开启(hypothesis.daily=0),跳过", fp)
            return
        from quantlab.engine import hypothesis as hy
        r = hy.run(n=n, dry_run=False)
        for x in (r.get("audited") or [])[:10]:
            log(f"  {x.get('status')} raw_t={x.get('raw_t')} {x['expr'][:60]}", fp)
        log(f"  生成 {r.get('generated')} / 审计 {len(r.get('audited') or [])} "
            f"/ 跳过 {len(r.get('skipped') or [])}", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def step_promote(fp) -> None:
    """观察池每日检查(前推 + FDR)。池子空时直接跳过,几乎零成本。"""
    log("⑦ 观察池检查…", fp)
    try:
        from quantlab.engine import promote as pm

        pool = pm.list_pool()
        if not pool:
            log("  池子空,跳过", fp)
            return
        r = pm.tick()
        for c in (r.get("checked") or [])[:10]:
            log(f"  {c.get('name')} ok={c.get('ok')} q={c.get('q')} "
                f"status={c.get('status')}", fp)
        log(f"  完成:{r.get('n')} 个候选", fp)
    except Exception as e:  # noqa: BLE001
        log(f"  失败:{type(e).__name__}: {str(e)[:120]}", fp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-snapshot", action="store_true")
    ap.add_argument("--skip-lhb", action="store_true")
    ap.add_argument("--skip-factors", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--skip-min5", action="store_true")
    ap.add_argument("--min5-top", type=int, default=300)
    ap.add_argument("--intraday", action="store_true",
                    help="顺手补小时线(默认不跑,日内结构空间已证否)")
    ap.add_argument("--intraday-top", type=int, default=300)
    ap.add_argument("--skip-promote", action="store_true",
                    help="跳过观察池每日检查")
    ap.add_argument("--skip-backup", action="store_true",
                    help="跳过模拟账户备份")
    ap.add_argument("--skip-hypothesis", action="store_true",
                    help="跳过 LLM 假设生成(默认按 config 开关)")
    ap.add_argument("--skip-equity", action="store_true",
                    help="跳过模拟账户权益快照")
    ap.add_argument("--factor-limit", type=int, default=8)
    args = ap.parse_args()

    logdir = ROOT / "logs"
    logdir.mkdir(exist_ok=True)
    fname = time.strftime("maintain-%Y-%m-%d.log")
    fp = open(logdir / fname, "a", encoding="utf-8")
    failed: list[str] = []

    def run(name: str, fn, *a) -> None:
        """单步失败只记日志,不中断后面的步骤 —— 夜间任务不能因为一个网络抖动就停摆。"""
        try:
            fn(fp, *a)
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            log(f"  ✗ {name} 失败:{type(e).__name__}: {str(e)[:150]}", fp)

    try:
        run("config", step_config)
        if not args.skip_snapshot:
            run("snapshot", step_snapshot)
        if not args.skip_lhb:
            run("lhb", step_lhb)
        if not args.skip_factors:
            run("factors", step_factors, args.factor_limit)
        if not args.skip_verify:
            run("verify", step_verify)
        if not args.skip_min5:
            run("min5", step_min5, args.min5_top)
        if args.intraday:
            run("intraday", step_intraday, args.intraday_top)
        if not args.skip_promote:
            run("promote", step_promote)
        if not args.skip_hypothesis:
            run("hypothesis", step_hypothesis)
        if not args.skip_equity:
            run("equity", step_equity)
        if not args.skip_backup:
            run("backup", step_backup)
        if failed:
            log(f"完成(失败 {len(failed)} 步:{'、'.join(failed)})", fp)
            return 1
        log("完成", fp)
        return 0
    finally:
        fp.close()


if __name__ == "__main__":
    raise SystemExit(main())
