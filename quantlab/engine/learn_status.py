# -*- coding: utf-8 -*-
"""学习闭环体检:一页看完归因、预测、参数、空间、观察池。

只读,不写库、不改配置。给人看的一页纸,不是给机器调的接口。

用法:
    python tools/learn_status.py [--account 默认账户] [--days 90]
"""
from __future__ import annotations

import time
from pathlib import Path


def collect(account: str | None = None, days: int = 90) -> dict:
    """把五个模块的只读接口拼成一份体检结果。任一模块失败只记错误,不连坐。"""
    out: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "account": account or "全部", "days": days,
                 "attribution": {}, "predict": {}, "versions": [],
                 "spaces": {}, "pool": {}, "errors": []}

    try:
        from . import strategy_review as sr
        out["attribution"] = sr.build(account=account, days=days)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"归因:{str(e)[:100]}")

    try:
        from . import predict_learn as pl
        acc = account or "默认账户"
        out["predict"] = pl.PredictLog(acc).stats(window=300)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"预测:{str(e)[:100]}")

    try:
        from ..data.store import store
        rows = store().conn.execute(
            "SELECT ts,account,strategy,params,reason FROM sl_versions "
            "ORDER BY ts DESC LIMIT 5").fetchall()
        out["versions"] = [dict(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"参数版本:{str(e)[:100]}")

    try:
        from . import factor_space as fsp
        out["spaces"] = fsp.report()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"空间:{str(e)[:100]}")

    try:
        from . import promote as pm
        out["pool"] = pm.summary()
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"观察池:{str(e)[:100]}")

    # 涨幅预选:跨日证据(单日 t 不算数)
    try:
        from . import pick_scorecard as ps
        out["picks"] = ps.collect(days=60)
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"预选:{str(e)[:100]}")

    # 生效的自学习参数(config.json 覆盖默认值,这里看真实值)
    try:
        from ..config import cfg as _c
        sl = _c().get("selflearn") or {}
        out["selflearn"] = {
            "enabled": bool(sl.get("enabled")),
            "tranche": bool(sl.get("tranche")),
            "tune": bool(sl.get("tune")),
            "auto_suspend": bool(sl.get("auto_suspend")),
            "hysteresis": float(sl.get("target_hysteresis_pct") or 0),
            "replan_tolerance": float(sl.get("replan_tolerance") or 0.05),
        }
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"自学习参数:{str(e)[:100]}")

    # 实盘 vs 基准:同一段日子里,账户赚了多少、指数涨了多少
    try:
        from ..data.store import store
        st = store()
        acc = account or "默认账户"
        rows = st.conn.execute(
            "SELECT day, equity FROM sl_equity WHERE account=? ORDER BY day",
            (acc,)).fetchall()
        if len(rows) >= 2:
            d0, d1 = str(rows[0]["day"]), str(rows[-1]["day"])
            e0, e1 = float(rows[0]["equity"] or 0), float(rows[-1]["equity"] or 0)
            idx = st.conn.execute(
                "SELECT bar_time, close FROM kline_cache WHERE code='idx:000300' "
                "AND klt=101 AND bar_time BETWEEN ? AND ? ORDER BY bar_time",
                (d0, d1)).fetchall()
            bench = None
            if len(idx) >= 2:
                i0, i1 = float(idx[0]["close"]), float(idx[-1]["close"])
                if i0:
                    bench = i1 / i0 - 1
            ret = (e1 / e0 - 1) if e0 else 0.0
            out["equity"] = {"account": acc, "days": len(rows), "start": d0,
                             "end": d1, "ret": round(ret, 4),
                             "bench": None if bench is None else round(bench, 4),
                             "diff": (None if bench is None
                                      else round(ret - bench, 4))}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"实盘:{str(e)[:100]}")

    # 数据积累:分钟线样本够不够重审微观结构
    try:
        from ..data.store import store
        st = store()
        acc = {}
        # 日线覆盖:多少只有最新交易日、总共多少只
        try:
            last = st.conn.execute(
                "SELECT MAX(bar_time) d FROM kline_cache WHERE klt=101 AND fqt=1"
            ).fetchone()["d"]
            row = st.conn.execute(
                "SELECT COUNT(DISTINCT code) c FROM kline_cache "
                "WHERE klt=101 AND fqt=1 AND bar_time=? AND length(code)=6",
                (last,)).fetchone()
            total = st.conn.execute(
                "SELECT COUNT(DISTINCT code) c FROM kline_cache "
                "WHERE klt=101 AND fqt=1 AND length(code)=6").fetchone()
            acc["日线"] = {"days": 0, "codes": int(row["c"] or 0),
                           "per_code_median": 0, "per_code_min": 0,
                           "latest": last, "total": int(total["c"] or 0)}
        except Exception:  # noqa: BLE001
            pass
        for klt, label in ((5, "5分钟"), (60, "60分钟")):
            r = st.conn.execute(
                "SELECT COUNT(DISTINCT substr(bar_time,1,10)) d, "
                "COUNT(DISTINCT code) c FROM kline_cache WHERE klt=?",
                (klt,)).fetchone()
            # 每只票各自有多少天:并集天数会虚高(不同票抓取起点不同)
            per = [int(x["n"]) for x in st.conn.execute(
                "SELECT COUNT(DISTINCT substr(bar_time,1,10)) n FROM kline_cache "
                "WHERE klt=? GROUP BY code", (klt,)).fetchall()]
            per.sort()
            med = per[len(per) // 2] if per else 0
            acc[label] = {"days": int(r["d"] or 0), "codes": int(r["c"] or 0),
                          "per_code_median": int(med),
                          "per_code_min": per[0] if per else 0}
        out["data"] = acc
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"数据:{str(e)[:100]}")

    # 假设生成:今天模型提了几条、过了几条、几条是 beta 伪装
    try:
        import json as _json
        rep = Path(__file__).resolve().parents[2] / "reports"
        files = sorted(rep.glob("hypothesis-*.json"))
        if files:
            h = _json.loads(files[-1].read_text(encoding="utf-8"))
            out["hypothesis"] = {"day": h.get("day"), "runs": h.get("runs", 0),
                                 "generated": h.get("generated", 0),
                                 "audited": len(h.get("audited") or []),
                                 "near_miss": h.get("near_miss", 0),
                                 "last": h.get("last_ts", "")}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"假设:{str(e)[:100]}")

    # 分批 churn:同一天里同批号被反复买(目标漂移的症状)
    try:
        from ..data.store import store
        from tools.check_tranche_churn import estimate_fees, summarize
        since = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        rows = [dict(r) for r in store().conn.execute(
            "SELECT code,name,tranche,tranches,qty,price,ts,action "
            "FROM sl_decisions WHERE ts>=?", (since,)).fetchall()]
        buys = [r for r in rows if str(r.get("action") or "") == "buy"]
        sells = [r for r in rows if str(r.get("action") or "") == "sell"]
        items = summarize(buys)
        dup = [x for x in items if x["dup"] and x["orders"] >= 2]
        fee_buy = estimate_fees(buys)
        # 卖出多一道印花税
        try:
            from quantlab.config import cfg as _c
            s = ((_c().get("broker") or {}).get("sim") or {})
            stamp = float(s.get("stamp_tax_rate") or 0.0)
        except Exception:  # noqa: BLE001
            stamp = 0.0
        sell_amt = sum(float(r.get("price") or 0) * int(r.get("qty") or 0)
                       for r in sells)
        fee_sell = estimate_fees(sells)["fees"] + sell_amt * stamp
        buy_amt = sum(float(r.get("price") or 0) * int(r.get("qty") or 0)
                      for r in buys)
        out["churn"] = {"codes": len(dup), "orders": len(buys),
                        "buy_n": len(buys), "sell_n": len(sells),
                        "buy_amt": round(buy_amt, 2), "sell_amt": round(sell_amt, 2),
                        "fees": round(fee_buy["fees"] + fee_sell, 2),
                        "avg_fee": fee_buy.get("avg"),
                        "top": [{"code": x["code"], "name": x["name"],
                                 "orders": x["orders"], "dup": x["dup"]}
                                for x in dup[:3]]}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"churn:{str(e)[:100]}")

    # 口径漂移:config 写的策略 vs 库里最近实际写库的策略
    try:
        from ..config import cfg as _c
        from ..data.store import store
        want = str((_c().get("autotrade") or {}).get("strategy") or "")
        row = store().conn.execute(
            "SELECT strategy, COUNT(*) n, MAX(ts) last FROM sl_decisions "
            "GROUP BY strategy ORDER BY last DESC LIMIT 1").fetchone()
        got = str(row["strategy"]) if row else ""
        out["key_drift"] = {
            "config": want, "db": got,
            "n": int(row["n"]) if row else 0,
            "mismatch": bool(want and got and want != got),
        }
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"口径:{str(e)[:100]}")
    return out


def render(d: dict) -> str:
    """把 collect() 的结果排成给人看的一页。"""
    lines = [f"学习闭环体检 · {d.get('account')} · 最近 {d.get('days')} 天 · "
             f"{d.get('ts')}"]

    at = d.get("attribution") or {}
    if at.get("n"):
        o = at.get("overall") or {}
        lines += ["", f"## 归因(样本 {at.get('n')} 条,扣费后均值 "
                      f"{(o.get('net_mean') or 0) * 100:+.2f}%/笔)"]
        for s in at.get("by_strategy") or []:
            lines.append(f"- {s.get('key')}:n={s.get('n')} "
                         f"t={s.get('t')} → {s.get('state')}"
                         f"(×{s.get('mult')})")
            if s.get("why"):
                lines.append(f"  理由:{s.get('why')}")
        for a in (at.get("advice") or [])[:3]:
            lines.append(f"- 建议:{a}")
    else:
        lines += ["", "## 归因", "- 没有已回填的决策"]

    p = d.get("predict") or {}
    if p:
        lines += ["", "## 预测层",
                  f"- n={p.get('n')} 准确率 {(p.get('acc') or 0) * 100:.1f}%"
                  f"(原始 {p.get('raw_n', p.get('n'))} 条)"]
        by_h = p.get("by_horizon") or {}
        if by_h:
            lines.append("- 按周期:" + " · ".join(
                f"{k}日 {(v.get('acc') or 0) * 100:.0f}%(n={v.get('n')})"
                for k, v in by_h.items()))

    vs = d.get("versions") or []
    if vs:
        lines += ["", "## 最近的参数调整"]
        for v in vs:
            lines.append(f"- {v.get('account')}/{v.get('strategy')}:"
                         f"{str(v.get('reason') or '')[:80]}")

    sp = d.get("spaces") or {}
    if sp.get("spaces"):
        lines += ["", "## 搜索空间"]
        for r in sp["spaces"]:
            tested = r.get("tested", r.get("audited"))
            lines.append(f"- {r.get('label')}:测 {tested} / 过 "
                         f"{r.get('promoted')} → {r.get('verdict')}"
                         f"(预算 {r.get('budget')})")
        if sp.get("verdict"):
            lines.append(f"- {sp['verdict']}")

    pool = d.get("pool") or {}
    if pool:
        lines += ["", "## 观察池",
                  f"- {pool.get('n', 0)} 个候选 "
                  + " · ".join(f"{k} {v}" for k, v in (pool.get("by_status") or {}).items())]

    pk = d.get("picks") or {}
    if pk.get("days"):
        lines += ["", "## 涨幅预选(跨日,单日 t 不算数)"]
        lines.append(f"- {pk.get('days')} 个交易日 / {pk.get('runs')} 次运行:"
                     f"超额均值 {(pk.get('excess_mean') or 0) * 100:+.3f}%/次"
                     f"(t={pk.get('excess_t')})")
        if not pk.get("enough"):
            lines.append(f"- ⚠ 样本不够下结论(要 ≥{pk.get('min_days')} 个交易日)")
        if pk.get("verify_n"):
            lines.append(f"- 次日验证 {pk.get('verify_n')} 条:胜率 "
                         f"{(pk.get('win_rate') or 0) * 100:.0f}%,"
                         f"达标率 {(pk.get('hit_rate') or 0) * 100:.0f}%")

    slv = d.get("selflearn") or {}
    if slv:
        lines += ["", "## 自学习参数(生效值)"]
        lines.append(f"- 开关:{'开' if slv.get('enabled') else '关'} · "
                     f"分批:{'开' if slv.get('tranche') else '关'} · "
                     f"调参:{'开' if slv.get('tune') else '关'} · "
                     f"自动暂停:{'开' if slv.get('auto_suspend') else '关'}")
        lines.append(f"- 目标缓动 {slv.get('hysteresis', 0):.0%} · "
                     f"同日重开容差 {slv.get('replan_tolerance', 0):.0%}")

    eq = d.get("equity") or {}
    if eq:
        lines += ["", f"## 实盘 vs 基准({eq.get('account')})"]
        lines.append(f"- {eq.get('start')} ~ {eq.get('end')}({eq.get('days')} 天):"
                     f"账户 {(eq.get('ret') or 0) * 100:+.2f}%")
        if eq.get("bench") is not None:
            lines.append(f"- 沪深300 同期 {(eq.get('bench') or 0) * 100:+.2f}%"
                         f",差 {(eq.get('diff') or 0) * 100:+.2f}%")
        lines.append("- 账户口径是总权益(含现金),上面「扣费均值」按已平仓单笔算,两者别混")

    da = d.get("data") or {}
    if da:
        lines += ["", "## 数据积累"]
        for label, v in da.items():
            if label == "日线":
                miss = int(v.get("total") or 0) - int(v.get("codes") or 0)
                lines.append(f"- 日线:{v.get('codes')}/{v.get('total')} 只有最新日"
                             f"({v.get('latest')})"
                             + (f",缺 {miss} 只(多为 9 开头/北交所)" if miss else ""))
                continue
            med = int(v.get("per_code_median", 0) or 0)
            if label == "5分钟":
                need = "够重审" if med >= 60 else f"还差 {max(0, 60 - med)} 个交易日"
            else:
                need = ""
            lines.append(f"- {label}:{v['days']} 个交易日 / {v['codes']} 只"
                         f"(每只中位 {med} 天)"
                         + (f"({need})" if need else ""))

    hy = d.get("hypothesis") or {}
    if hy:
        lines += ["", "## LLM 假设(最近一次)"]
        lines.append(f"- {hy.get('day')} · {hy.get('runs')} 次生成 / "
                     f"提 {hy.get('generated')} 条 / 审计 {hy.get('audited')} 条 / "
                     f"beta 伪装 {hy.get('near_miss')} 条")
        if not hy.get("generated"):
            lines.append("- 今天没有产出(模型没配或都未通过校验)")

    ch = d.get("churn") or {}
    if ch.get("orders") or ch.get("sell_n"):
        lines += ["", "## 当日交易与成本"]
        lines.append(f"- 买 {ch.get('buy_n', 0)} 笔 {ch.get('buy_amt', 0):,.0f} 元 · "
                     f"卖 {ch.get('sell_n', 0)} 笔 {ch.get('sell_amt', 0):,.0f} 元 · "
                     f"估费用 {ch.get('fees', 0):,.0f} 元")
        if ch.get("codes"):
            lines.append(f"- 同批号重复 {ch.get('codes')} 只(目标漂移会重开计划)")
        for x in ch.get("top") or []:
            dup = " ".join(f"{k}×{v}" for k, v in sorted((x.get("dup") or {}).items()))
            lines.append(f"- {x.get('code')} {x.get('name')}:"
                         f"{x.get('orders')} 笔({dup})")

    kd = d.get("key_drift") or {}
    if kd:
        lines += ["", "## 口径"]
        lines.append(f"- config 策略:`{kd.get('config') or '(空)'}` · "
                     f"库里最近写:`{kd.get('db') or '(无)'}`(共 {kd.get('n')} 条)")
        if kd.get("mismatch"):
            lines.append("- ⚠ 两者不一致:报告按 config 过滤,交易按实例写库 —— "
                         "日报可能看不到今天的决策(见 resolve_key 自愈)")

    if d.get("errors"):
        lines += ["", "## 读取失败"] + [f"- {e}" for e in d["errors"]]
    return "\n".join(lines)


def main() -> int:
    import argparse
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT))
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    print(render(collect(account=args.account or None, days=args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
