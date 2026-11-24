# -*- coding: utf-8 -*-
"""盘后复盘:成交记录 → 平仓配对 → 成绩单 + 纪律检查。

先说清楚这份复盘的边界,免得数字被当成它算不出来的东西:

· 数据源只有 orders 表(成交/委托)。表里没有「策略名」字段,所以
  「哪条策略赚的钱」这一层归因做不了。能做的是按标的、按日、按
  持仓天数归因,以及在 remark/gateway 里找到的线索。
· 平仓用 FIFO 配对。A 股是 T+1,但模拟盘可能当天买当天卖,
  所以配对不做日期限制,只按时间顺序。
· 手续费取订单上的 fee 字段;买单的手续费按配对数量摊到各笔。
· 纪律检查只认「从订单能证明的事」。需要盘口或实时价才能判断的
  (比如是否追在涨停板上),这里明确写「需行情」,不猜。
"""
from __future__ import annotations

import time

from ..config import cfg
from ..data.store import store


def _fill_of(o: dict) -> tuple[float, int]:
    qty = int(o.get("filled_qty") or 0)
    px = o.get("filled_price") or o.get("price") or 0.0
    return float(px), qty


def _genesis() -> dict[str, float]:
    """每个模拟账户的「生日」。

    sim_accounts.created_at 会在 reset()/create_account() 时被刷新,所以它
    代表的是「这一世」的起点。早于它的订单是上一世的成交:库里还留着,但
    钱早就不在那个账户里了,算进来就会凭空多出几十万盈亏和一堆幽灵持仓。
    """
    out: dict[str, float] = {}
    try:
        for a in store().sim_accounts():
            name = str(a.get("name") or "")
            if name:
                out[name] = float(a.get("created_at") or 0.0)
    except Exception:  # noqa: BLE001
        return {}
    return out


def _alive(orders: list[dict], genesis: dict[str, float]) -> tuple[list[dict], int]:
    """剔掉账户重建之前的订单。返回 (留下的, 剔掉的条数)。

    没有 genesis 记录的账户(比如演示账户)一律保留 —— 不知道生日就别乱删。
    """
    if not genesis:
        return orders, 0
    keep: list[dict] = []
    drop = 0
    for o in orders:
        g = genesis.get(str(o.get("account") or ""))
        if g and float(o.get("ts") or 0) < g:
            drop += 1
            continue
        keep.append(o)
    return keep, drop


def _usable(orders: list[dict]) -> list[dict]:
    out = []
    for o in orders:
        if str(o.get("status") or "") in ("rejected", "cancelled"):
            continue
        px, qty = _fill_of(o)
        if qty <= 0 or px <= 0:
            continue
        if str(o.get("side") or "") not in ("buy", "sell"):
            continue
        d = dict(o)
        d["_px"], d["_qty"] = px, qty
        d["_fee"] = float(o.get("fee") or 0.0)
        out.append(d)
    out.sort(key=lambda r: (float(r.get("ts") or 0), int(r.get("id") or 0)))
    return out


def _round_trips(orders: list[dict]) -> tuple[list[dict], list[dict]]:
    """FIFO 配对。返回 (已平仓明细, 未平仓的买入批次)。"""
    lots: dict[str, list[dict]] = {}
    closed: list[dict] = []
    for o in orders:
        code = str(o.get("code") or "")
        if not code:
            continue
        px, qty = o["_px"], o["_qty"]
        if o.get("side") == "buy":
            lots.setdefault(code, []).append(
                {"qty": qty, "px": px, "ts": float(o.get("ts") or 0),
                 "fee_ps": o["_fee"] / qty if qty else 0.0,
                 "oid": o.get("id"), "name": o.get("name") or ""})
            continue
        # 卖出:按买入顺序消耗
        remain, fee_ps_sell = qty, (o["_fee"] / qty if qty else 0.0)
        book = lots.get(code) or []
        while remain > 0 and book:
            lot = book[0]
            take = min(remain, int(lot["qty"]))
            pnl = (px - lot["px"]) * take - (lot["fee_ps"] + fee_ps_sell) * take
            closed.append({
                "code": code, "name": o.get("name") or lot.get("name") or "",
                "open_ts": lot["ts"], "close_ts": float(o.get("ts") or 0),
                "qty": take, "buy_px": lot["px"], "sell_px": px,
                "pnl": pnl, "pnl_pct": (px / lot["px"] - 1.0) * 100.0 if lot["px"] else 0.0,
                "days": max(0.0, (float(o.get("ts") or 0) - lot["ts"]) / 86400.0),
                "buy_oid": lot["oid"], "sell_oid": o.get("id"),
            })
            lot["qty"] -= take
            remain -= take
            if lot["qty"] <= 0:
                book.pop(0)
        # 卖超出持仓的部分不记(脏数据),不做空
    open_lots = [dict(lot, code=code) for code, book in lots.items() for lot in book
                 if int(lot["qty"]) > 0]
    return closed, open_lots


def _stats(closed: list[dict], orders: list[dict]) -> dict:
    n = len(closed)
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    gross_win = sum(c["pnl"] for c in wins)
    gross_loss = -sum(c["pnl"] for c in losses)
    fees = sum(o["_fee"] for o in orders)
    turnover = sum(o["_px"] * o["_qty"] for o in orders)

    # 最大连亏(按平仓时间)
    streak = worst = 0
    for c in sorted(closed, key=lambda r: r["close_ts"]):
        if c["pnl"] < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0

    # 按日累计(用卖出日归属)
    by_day: dict[str, dict] = {}
    for c in closed:
        day = time.strftime("%Y-%m-%d", time.localtime(c["close_ts"]))
        d = by_day.setdefault(day, {"day": day, "pnl": 0.0, "n": 0, "win": 0})
        d["pnl"] += c["pnl"]
        d["n"] += 1
        d["win"] += 1 if c["pnl"] > 0 else 0
    equity, peak, mdd = 0.0, 0.0, 0.0
    for d in sorted(by_day.values(), key=lambda r: r["day"]):
        equity += d["pnl"]
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
        d["cum"] = equity

    by_code: dict[str, dict] = {}
    for c in closed:
        b = by_code.setdefault(c["code"], {"code": c["code"], "name": c["name"],
                                           "n": 0, "pnl": 0.0, "win": 0, "days": 0.0})
        b["n"] += 1
        b["pnl"] += c["pnl"]
        b["win"] += 1 if c["pnl"] > 0 else 0
        b["days"] += c["days"]

    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / n * 100.0) if n else None,
        "pnl": sum(c["pnl"] for c in closed),
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": (sum(c["pnl"] for c in closed) / n) if n else None,
        "max_consec_loss": worst,
        "avg_days": (sum(c["days"] for c in closed) / n) if n else None,
        "fees": fees, "turnover": turnover,
        "orders": len(orders),
        "max_dd": mdd, "by_day": sorted(by_day.values(), key=lambda r: r["day"]),
        "by_code": sorted(by_code.values(), key=lambda r: -r["pnl"]),
    }


def _discipline(orders: list[dict], closed: list[dict], open_lots: list[dict]) -> list[dict]:
    """只查「从订单能证明」的几条。每条给结论 + 触发明细 + 一句建议。"""
    rk = cfg().get("risk") or {}
    max_amt = float(rk.get("max_order_amount") or 50_000)
    max_ratio = float(rk.get("max_position_ratio") or 0.35)
    max_daily = int(rk.get("max_daily_orders") or 30)
    stop = float(cfg().get("auto.stop_loss_pct") or 0.0)
    stop_pct = stop * 100.0 if stop else 8.0
    stop_is_default = not stop

    out: list[dict] = []

    # 1) 单笔金额超风控上限
    over = [o for o in orders if o["_px"] * o["_qty"] > max_amt + 1e-6]
    out.append({
        "name": "单笔金额", "level": "bad" if over else ("ok" if orders else "none"),
        "detail": f"上限 {max_amt:,.0f} 元" + (f",超限 {len(over)} 笔" if over else ""),
        "advice": "超限单是风控闸没拦住的信号,去设置页看一眼 max_order_amount" if over else "",
        "items": [f"#{o['id']} {o.get('name') or o.get('code')} "
                  f"{o['_px'] * o['_qty']:,.0f} 元" for o in over[:8]],
    })

    # 2) 单票集中度(按买入成交额近似)
    buys = [o for o in orders if o.get("side") == "buy"]
    total = sum(o["_px"] * o["_qty"] for o in buys) or 0.0
    conc = {}
    for o in buys:
        conc[o["code"]] = conc.get(o["code"], 0.0) + o["_px"] * o["_qty"]
    hot = [(c, v / total * 100.0) for c, v in conc.items() if total and v / total > max_ratio]
    hot.sort(key=lambda x: -x[1])
    out.append({
        "name": "单票集中度", "level": "warn" if hot else ("ok" if buys else "none"),
        "detail": f"阈值 {max_ratio * 100:.0f}%(按买入额近似)" + (f",{len(hot)} 只超" if hot else ""),
        "advice": "集中度是回撤的主要来源;要么降低单票上限,要么分两笔建仓" if hot else "",
        "items": [f"{c} 占买入额 {p:.0f}%" for c, p in hot[:8]],
    })

    # 3) 日内下单过密
    per_day: dict[str, int] = {}
    for o in orders:
        day = time.strftime("%Y-%m-%d", time.localtime(float(o.get("ts") or 0)))
        per_day[day] = per_day.get(day, 0) + 1
    dense = sorted([(d, n) for d, n in per_day.items() if n > max_daily],
                   key=lambda x: -x[1])
    out.append({
        "name": "日内频率", "level": "warn" if dense else ("ok" if per_day else "none"),
        "detail": f"上限 {max_daily} 单/日" + (f",{len(dense)} 天超出" if dense else ""),
        "advice": "手数变多通常是随手交易开始;先想清楚为什么这一单必须下" if dense else "",
        "items": [f"{d} 共 {n} 单" for d, n in dense[:8]],
    })

    # 4) 亏损离场没走止损
    deep = [c for c in closed if c["pnl_pct"] <= -stop_pct]
    tail = "(止损线未设,按 8% 兜底)" if stop_is_default else f"(止损线 {stop_pct:.0f}%)"
    out.append({
        "name": "止损纪律", "level": "bad" if deep else ("ok" if closed else "none"),
        "detail": f"亏损超 {stop_pct:.0f}% 才离场 {len(deep)} 笔 {tail}",
        "advice": "止损位在买入时就该定下来;事后砍的亏损永远比计划内的大" if deep else "",
        "items": [f"{c['name'] or c['code']} {c['pnl_pct']:.1f}% "
                  f"({time.strftime('%m-%d', time.localtime(c['close_ts']))})"
                  for c in sorted(deep, key=lambda x: x["pnl_pct"])[:8]],
    })

    # 5) 亏损加仓
    add: list[str] = []
    seq: dict[str, list[dict]] = {}
    for o in orders:
        seq.setdefault(o["code"], []).append(o)
    for code, lst in seq.items():
        prev_sell_loss = False
        for o in lst:
            if o.get("side") == "sell":
                prev_sell_loss = any(c["code"] == code and c["sell_oid"] == o.get("id")
                                     and c["pnl"] < 0 for c in closed)
            elif prev_sell_loss and o.get("side") == "buy":
                add.append(f"{o.get('name') or code} 亏着卖完又买回 #{o.get('id')}")
    out.append({
        "name": "亏损加仓", "level": "warn" if add else ("ok" if seq else "none"),
        "detail": f"同标的亏损卖出后又买回 {len(add)} 次",
        "advice": "亏着走又追回来,说明决策靠情绪而不是计划;要做就把理由写进备注" if add else "",
        "items": add[:8],
    })

    # 6) 长期未平的持仓
    now = time.time()
    stale = [l for l in open_lots if (now - l["ts"]) / 86400.0 > 20]
    out.append({
        "name": "滞留持仓", "level": "warn" if stale else ("ok" if open_lots else "none"),
        "detail": f"持有超 20 个交易日未动 {len(stale)} 笔",
        "advice": "没有卖出理由的持仓,等于没有买入理由;给它补一条退出条件" if stale else "",
        "items": [f"{l.get('name') or l['code']} 已持 {(now - l['ts']) / 86400:.0f} 天" 
                  for l in stale[:8]],
    })
    return out


def build(account: str | None = None, days: int = 90,
          quotes: dict | None = None,
          hidden_days: list[str] | None = None) -> dict:
    """跑一遍复盘。days 只用来标注时间窗,不截断配对(配对要完整)。

    quotes 可选:传了就算未平仓浮盈,stat 里多 unrealized / total_pnl。
    hidden_days:显示层隐藏的日期。只从 by_day 和 pnl 里扣,订单不动,
    FIFO 配对完整,学习链不受影响。
    """
    st = store()
    cutoff = time.time() - days * 86400
    # 只读窗口前一年的订单:FIFO 配对要完整,但没必要把几年前的翻出来。
    raw = st.orders(limit=5000, account=account, since=cutoff - 365 * 86400)
    # 账户重建过的话,重建之前的成交不属于这一世的资金 —— 先剔掉再配对,
    # 否则「已实现」会把这辈子根本没拿到手的钱算成业绩。
    genesis = _genesis()
    raw, dropped = _alive(raw, genesis)
    orders = _usable(raw)
    window = [o for o in orders if float(o.get("ts") or 0) >= cutoff]
    closed, open_lots = _round_trips(orders)
    # 窗口内没有平仓就是没有,不回退到全部 —— 回退会让「近 90 天」变成
    # 「全部历史」,数字对不上用户看到的账户盈亏。
    closed_window = [c for c in closed if c["close_ts"] >= cutoff]

    hidden = {str(x)[:10] for x in (hidden_days or [])}
    if hidden:
        # 按平仓日/订单日过滤,再重新算 stat —— 胜率、手续费、盈亏比、
        # 最大连亏都跟着变,不是只改显示。
        def _day_of(ts):
            return time.strftime("%Y-%m-%d", time.localtime(float(ts or 0)))
        closed_window = [c for c in closed_window
                         if _day_of(c["close_ts"]) not in hidden]
        window = [o for o in window if _day_of(o.get("ts")) not in hidden]
    stat = _stats(closed_window, window)

    unrealized = 0.0
    priced = 0
    if quotes:
        for lot in open_lots:
            q = quotes.get(lot["code"]) or {}
            px = float(q.get("price") or 0)
            if px > 0:
                priced += int(lot["qty"])
                unrealized += ((px - lot["px"]) * lot["qty"]
                               - lot["fee_ps"] * lot["qty"])
    # 没取到行情时浮盈不是 0,是「不知道」。标记出来,免得页面把
    # 「取不到价」显示成「不赚不亏」——那是最容易被当成算错的一种。
    stat["unrealized"] = unrealized
    stat["total_pnl"] = stat["pnl"] + unrealized
    stat["priced"] = priced
    stat["has_quotes"] = bool(priced)
    stat["dropped"] = dropped
    checks = _discipline(window, closed_window, open_lots)
    bad = sum(1 for c in checks if c["level"] == "bad")
    warn = sum(1 for c in checks if c["level"] == "warn")
    return {
        "ok": bool(orders),
        "account": account or "全部账户",
        "days": days,
        "asof": time.strftime("%Y-%m-%d %H:%M"),
        "stat": stat, "closed": closed_window, "open_lots": open_lots,
        "checks": checks, "bad": bad, "warn": warn,
        "dropped": dropped,
        "empty": (f"这一世的账户还没有成交(已跳过重建前的 {dropped} 笔历史成交)"
                  if dropped and not orders else
                  "还没有成交记录。先在「交易」页下一单,或跑一次自动交易。"),
    }


def verdict(rep: dict) -> str:
    if not rep.get("ok"):
        return rep.get("empty") or "无数据"
    s = rep["stat"]
    if rep["bad"]:
        return f"{rep['bad']} 项纪律红灯,{rep['warn']} 项黄灯 — 先修纪律,再谈收益"
    if rep["warn"]:
        return f"纪律基本及格,{rep['warn']} 项要留意"
    if s["trades"] == 0:
        return "有委托但没有平仓,成绩单还看不出来"
    if s["win_rate"] is not None and s["win_rate"] >= 55:
        return "纪律与胜率都在线,继续保持"
    return "纪律没大问题,赢面偏低 — 看盈亏比和持仓天数是不是拖累"


def to_text(rep: dict) -> str:
    if not rep.get("ok"):
        # 没有成交也要把六项纪律列出来:它是「将来要守的规矩」,
        # 只回一句「还没成交」等于什么都没说。
        lines = [f"盘后复盘 · {rep.get('account') or '全部账户'} · "
                 f"近 {rep.get('days') or 0} 天 · {rep.get('asof') or ''}",
                 rep.get("empty") or "无数据", "", "【纪律检查】"]
        for c in rep.get("checks") or []:
            mark = {"ok": "✓", "warn": "!", "bad": "✗", "none": "·"}.get(c["level"], "·")
            lines.append(f"  {mark} {c['name']:<6}{c['detail']}")
            if c["advice"]:
                lines.append(f"      建议:{c['advice']}")
        return "\n".join(lines)
    s = rep["stat"]
    lines = [f"盘后复盘 · {rep['account']} · 近 {rep['days']} 天 · {rep['asof']}",
             verdict(rep), "",
             f"成交 {s['orders']} 笔 / 平仓 {s['trades']} 笔 · 成交额 {s['turnover']:,.0f} 元 · "
             f"手续费 {s['fees']:,.0f} 元",
             f"已实现盈亏 {s['pnl']:+,.0f} 元 · 胜率 "
             + ("—" if s["win_rate"] is None else f"{s['win_rate']:.0f}%")
             + " · 盈亏比 " + ("—" if not s["profit_factor"] else f"{s['profit_factor']:.2f}")
             + f" · 最大连亏 {s['max_consec_loss']} 笔",
             (f"持仓浮盈 {s['unrealized']:+,.0f} 元 · "
              f"合计 {s.get('total_pnl', s['pnl']):+,.0f} 元"
              "(浮盈按当前价,和上面已实现是两个口径)")
             if s.get("has_quotes")
             else "持仓浮盈未取到行情,合计只含已实现",
             ""]
    if rep.get("dropped"):
        lines.insert(3, f"· 已跳过账户重建前的 {rep['dropped']} 笔历史成交"
                        "(钱已不在账户里,算进去就是虚增)")
    lines.append("【纪律检查】")
    for c in rep["checks"]:
        mark = {"ok": "✓", "warn": "!", "bad": "✗", "none": "·"}.get(c["level"], "·")
        lines.append(f"  {mark} {c['name']:<6}{c['detail']}")
        if c["advice"]:
            lines.append(f"      建议:{c['advice']}")
        for it in c["items"]:
            lines.append(f"      · {it}")
    if s["by_code"]:
        lines += ["", "【按标的】"]
        for b in s["by_code"][:15]:
            lines.append(f"  {b['name'] or b['code']:<8} {b['n']:>2} 笔  "
                         f"{b['pnl']:>+10,.0f} 元  胜率 {b['win'] / b['n'] * 100:.0f}%")
    if s["by_day"]:
        lines += ["", "【按日】"]
        for d in s["by_day"][-15:]:
            lines.append(f"  {d['day']}  {d['pnl']:>+10,.0f} 元  {d['n']} 笔  "
                         f"累计 {d.get('cum', 0):+,.0f}")
    return "\n".join(lines)
