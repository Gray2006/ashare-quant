# -*- coding: utf-8 -*-
"""当日盈亏与操作归因。**不对外**。

为什么单独一个模块:review.py 算的是「已平仓」的业绩,那是策略能力;
但用户看账户,看的是「今天赚了还是亏了」—— 含持仓浮盈浮亏。
两个口径都对,混在一起就都不准。

这里用**权益快照差值**做总账:
    日盈亏 = 今日权益 - 上一交易日权益
再拆成两项,加起来必须等于总账:
    已实现(当天平仓,配对时已扣双边费用) + 浮动(持仓市值变化)

浮动是倒推的:浮动 = 总账 - 已实现。
倒推比正算可靠 —— 正算要逐只取昨收,数据一缺就错;倒推只要两个快照对。
费用不单独再减:平仓盈亏在 FIFO 配对时已经扣过买卖两边的费,
再减一遍就是重复扣费,拆解出来的三项看着怎么都对不上。

操作原因从两处取:
    orders.remark        —— 下单时写的(止损/止盈/分批第几批)
    sl_decisions         —— 学习引擎记的(信号强度、参数、批次)
"""
from __future__ import annotations

import datetime as dt
import time

from ..data.store import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sl_equity (
    day          TEXT NOT NULL,
    account      TEXT NOT NULL DEFAULT 'default',
    equity       REAL NOT NULL DEFAULT 0,
    cash         REAL NOT NULL DEFAULT 0,
    market_value REAL NOT NULL DEFAULT 0,
    ts           REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, account)
);
"""

_ready = False


def _db():
    global _ready
    st = store()
    if not _ready:
        with st.conn as c:
            c.executescript(_SCHEMA)
        _ready = True
    return st


def snapshot(account: str = "default", equity: float = 0.0, cash: float = 0.0,
             market_value: float = 0.0, day: str | None = None) -> None:
    """记一条当日权益。同一天重复调用会覆盖(取最后一次)。"""
    day = day or dt.date.today().isoformat()
    st = _db()
    with st.conn as c:
        c.execute(
            "INSERT INTO sl_equity(day,account,equity,cash,market_value,ts) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(day,account) DO UPDATE SET "
            "equity=excluded.equity, cash=excluded.cash, "
            "market_value=excluded.market_value, ts=excluded.ts",
            (day, account or "default", float(equity), float(cash),
             float(market_value), time.time()))


def _prev_snapshot(account: str, day: str) -> dict | None:
    st = _db()
    r = st.conn.execute(
        "SELECT * FROM sl_equity WHERE account=? AND day<? ORDER BY day DESC LIMIT 1",
        (account, day)).fetchone()
    return dict(r) if r else None


def _today_snapshot(account: str, day: str) -> dict | None:
    st = _db()
    r = st.conn.execute(
        "SELECT * FROM sl_equity WHERE account=? AND day=?", (account, day)).fetchone()
    return dict(r) if r else None


def _closed_on(account: str, day: str, raw: list[dict] | None = None) -> list[dict]:
    """当天平仓的明细(FIFO 配对,复用 review 的逻辑)。

    raw 可传入已取好的委托,避免同一次报告里把 5000 条委托扫三遍。
    """
    from . import review as rv
    raw = raw if raw is not None else store().orders(limit=5000, account=account)
    raw, _drop = rv._alive(raw, rv._genesis())
    orders = rv._usable(raw)
    closed, _open = rv._round_trips(orders)
    out = []
    for c in closed:
        d = time.strftime("%Y-%m-%d", time.localtime(c["close_ts"]))
        if d == day:
            out.append(c)
    return out


def _closed_multi(accounts: list[str], day: str,
                  raw: list[dict] | None = None) -> list[dict]:
    """多账户合并的当日平仓。各账户各配各的 FIFO,互不串仓。"""
    if not accounts:
        return []
    if raw is None:
        raw = store().orders(limit=5000)
    out: list[dict] = []
    for a in accounts:
        sub = [o for o in raw if str(o.get("account") or "") == a]
        out.extend(_closed_on(a, day, sub))
    return out


def _orders_on(account: str, day: str, raw: list[dict] | None = None) -> list[dict]:
    raw = raw if raw is not None else store().orders(limit=5000, account=account)
    out = []
    for o in raw:
        ts = float(o.get("ts") or 0)
        if not ts:
            continue
        if time.strftime("%Y-%m-%d", time.localtime(ts)) != day:
            continue
        if str(o.get("status") or "") in ("rejected", "cancelled"):
            continue
        out.append(o)
    return out


def reasons(account: str = "default", day: str | None = None,
            raw: list[dict] | None = None) -> list[dict]:
    """当天每笔操作的原因。订单备注优先,学习记录补充。"""
    day = day or dt.date.today().isoformat()
    orders = _orders_on(account, day, raw)
    # 和总账同一把尺子:账户重建前的旧单不算「今天的操作」。
    # 少了这一步,「当日盈亏」按 28 笔算、「操作原因」却列出 247 行,
    # 两边对不上,看着就像金额算错了。
    try:
        from . import review as rv
        orders, _drop = rv._alive(orders, rv._genesis())
    except Exception:  # noqa: BLE001
        pass

    # 学习引擎记的决策,按 (code, side) 对齐
    learned: dict[tuple[str, str], dict] = {}
    try:
        from . import selflearn as sl
        sl._db()
        st = _db()
        rows = st.conn.execute(
            "SELECT code,action,strength,tranche,tranches,params,note "
            "FROM sl_decisions WHERE account=? AND ts>=? AND ts<?",
            (account, dt.datetime.strptime(day, "%Y-%m-%d").timestamp(),
             dt.datetime.strptime(day, "%Y-%m-%d").timestamp() + 86400)
        ).fetchall()
        for r in rows:
            learned[(str(r["code"]), str(r["action"]))] = dict(r)
    except Exception:  # noqa: BLE001
        pass

    out = []
    for o in orders:
        code = str(o.get("code") or "")
        side = str(o.get("side") or "")
        remark = str(o.get("remark") or "").strip()
        lr = learned.get((code, side)) or {}
        bits = []
        if remark:
            bits.append(remark)
        if lr:
            tr, ts_ = int(lr.get("tranche") or 0), int(lr.get("tranches") or 0)
            if tr and ts_:
                bits.append(f"第 {tr}/{ts_} 批")
            s = float(lr.get("strength") or 0)
            if s > 0:
                bits.append(f"信号强度 {s:.2f}")
        if not bits:
            bits.append("策略调仓")
        out.append({
            "code": code, "name": o.get("name") or "",
            "side": side, "qty": int(o.get("filled_qty") or o.get("qty") or 0),
            "price": float(o.get("filled_price") or o.get("price") or 0),
            "reason": " · ".join(bits),
            "ts": float(o.get("ts") or 0),
        })
    out.sort(key=lambda r: r["ts"])
    return out


def _resolve(account: str | None) -> str:
    """把 None / 'default' 落到真实存在的账户名上。

    历史坑:接口默认值是 'default',而模拟盘真实账户名是「默认账户」。
    传 None(全部账户)或 'default' 直接查 sl_equity 会一行都查不到,
    当日盈亏就恒为空 —— 页面上那个「--」就是这么来的。
    """
    acc = str(account or "").strip()
    st = _db()
    if acc and acc != "default":
        return acc
    if acc == "default":
        # 真有叫 default 的账户(测试就是这么建的),别抢它的名分
        hit = st.conn.execute(
            "SELECT 1 FROM sl_equity WHERE account='default' LIMIT 1").fetchone()
        if hit:
            return "default"
    row = st.conn.execute(
        "SELECT account FROM sl_equity GROUP BY account "
        "ORDER BY MAX(ts) DESC LIMIT 1").fetchone()
    if row:
        return str(row["account"])
    try:
        accts = store().sim_accounts()
        if accts:
            return str(accts[-1].get("name") or "") or "default"
    except Exception:  # noqa: BLE001
        pass
    return "default"


def _accounts(account: str | None) -> list[str]:
    """要统计的账户。None / '' = 全部账户。"""
    if account:
        return [_resolve(account)]
    st = _db()
    names = [str(r["account"]) for r in st.conn.execute(
        "SELECT DISTINCT account FROM sl_equity").fetchall()]
    try:
        for a in store().sim_accounts():
            n = str(a.get("name") or "")
            if n and n not in names:
                names.append(n)
    except Exception:  # noqa: BLE001
        pass
    return names or ["default"]


def _reasons_multi(accounts: list[str], day: str) -> list[dict]:
    out: list[dict] = []
    for a in accounts:
        out.extend(reasons(a, day))
    out.sort(key=lambda r: r["ts"])
    return out


def _close_price(code: str, day: str) -> float:
    """某只票在 day 当天或之前最近一个交易日的收盘价。"""
    try:
        r = store().conn.execute(
            "SELECT close FROM kline_cache WHERE code=? AND klt=101 AND fqt=1 "
            "AND bar_time<=? ORDER BY bar_time DESC LIMIT 1",
            (code, day)).fetchone()
        return float(r[0]) if r and r[0] else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _equity_by_close(account: str, day: str) -> float | None:
    """按 day 收盘价重算当日权益:现金 + Σ(持仓 × 收盘价)。

    快照里的 market_value 是「写快照那一刻」的价。自动交易在 14:55 跑,
    写进去的是盘中价;第二天拿它当基准算当日盈亏,昨天尾盘的涨跌就被
    算成了今天的亏损 —— 这是「当日盈亏不对」的主因。
    这里用日线收盘价重算,基准才和「收盘权益」一致。
    数据不全(缺初始资金/缺收盘价)时返回 None,调用方退回快照。
    """
    try:
        st = store()
        acct = None
        for a in st.sim_accounts():
            if str(a.get("name") or "") == account:
                acct = a
                break
        if not acct:
            return None
        initial = float(acct.get("initial_cash") or 0)
        if initial <= 0:
            return None
        from . import review as rv
        raw = st.orders(limit=5000, account=account)
        raw, _drop = rv._alive(raw, rv._genesis())
        orders = rv._usable(raw)
        end = time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400
        cash = initial
        qty: dict[str, int] = {}
        for o in orders:
            if float(o.get("ts") or 0) >= end:
                continue
            px, q = o["_px"], int(o["_qty"])
            fee = float(o.get("_fee") or 0)
            code = str(o.get("code") or "")
            if o.get("side") == "buy":
                cash -= px * q + fee
                qty[code] = qty.get(code, 0) + q
            else:
                cash += px * q - fee
                qty[code] = qty.get(code, 0) - q
        mv = 0.0
        for code, n in qty.items():
            if n <= 0:
                continue
            px = _close_price(code, day)
            if px <= 0:
                return None      # 缺收盘价就别硬算,退回快照
            mv += px * n
        return cash + mv
    except Exception:  # noqa: BLE001
        return None


def _mtm(account: str, snap: dict, quotes: dict | None) -> float:
    """按当前价重估一条快照的权益:现金 + Σ 现价×数量。

    快照里的市值是写快照那一刻算的。如果那一刻没有行情(持仓还没被
    set_mark_prices 标价),市值就退回成本价 —— 存进去的是"成本价市值",
    持仓涨跌完全没进总账,当日盈亏永远显示 0。
    有行情就当场重算一遍,比改正历史快照可靠。
    """
    base = float(snap.get("equity") or 0)
    if not quotes:
        return base
    try:
        cash = float(snap.get("cash") or 0)
        pos = store().load_positions(account=account)
    except Exception:  # noqa: BLE001
        return base
    if not pos:
        return base
    mv, priced = 0.0, False
    for p in pos:
        q = quotes.get(str(p.get("code") or "")) or {}
        px = float(q.get("price") or 0)
        if px <= 0:
            continue
        priced = True
        mv += px * int(p.get("qty") or 0)
    return (cash + mv) if priced else base


def report(account: str | None = "default", day: str | None = None,
           quotes: dict | None = None) -> dict:
    """当日盈亏 + 操作原因 + 按标的贡献。

    account=None / '' = 全部账户,各账户的权益差相加。
    """
    day = day or dt.date.today().isoformat()
    all_acc = not account
    accts = _accounts(account)
    acc = accts[0]
    label = "全部账户" if all_acc else acc

    # 总账:逐账户取 (今日 - 上一日),只有两个快照都在的账户才算得出来。
    # 「全部账户」时闭仓/委托/持仓也只统计这些算得出总账的账户 ——
    # 否则会出现「明细里有、总账里没有」的错位,数字看着就像算错了。
    total = 0.0
    eq_today = eq_prev = 0.0
    booked: list[str] = []
    for a in accts:
        t, p = _today_snapshot(a, day), _prev_snapshot(a, day)
        if not (t and p):
            continue
        # 今日快照按现价重估:快照可能是在没有行情时写的(市值退回成本价),
        # 那样持仓涨跌根本不在总账里。
        # 上一日权益优先按「昨日收盘价」重算 —— 快照里的市值是写快照那一刻
        # 的盘中价,直接拿来当基准会把昨天尾盘的涨跌算成今天的亏损。
        # 重算不出来(缺收盘价/缺初始资金)才退回快照。
        t_eq = _mtm(a, t, quotes)
        p_eq = _equity_by_close(a, str(p.get("day") or ""))
        if p_eq is None:
            p_eq = float(p.get("equity") or 0)
        eq_today += t_eq
        eq_prev += p_eq
        total += t_eq - p_eq
        booked.append(a)
    n_eq = len(booked)
    if not n_eq:
        total = None
        eq_today = eq_prev = None
        booked = accts
    elif all_acc:
        accts = booked

    # 委托只取一次:平仓配对、当日委托、操作原因都用这一份。
    # 先按账户生日剔掉重建前的旧单 —— 那些钱已经不在账户里了,
    # 留着会让「当天手续费」把上一世的费用算进这一天。
    from . import review as rv
    raw = store().orders(limit=5000, account=None if all_acc else acc)
    raw, _drop = rv._alive(raw, rv._genesis())
    if all_acc:
        raw = [o for o in raw if str(o.get("account") or "") in set(accts)]
    closed = _closed_on(acc, day, raw) if not all_acc else _closed_multi(accts, day, raw)
    orders = _orders_on(acc, day, raw) if not all_acc else [
        o for o in raw
        if time.strftime("%Y-%m-%d", time.localtime(float(o.get("ts") or 0))) == day
        and str(o.get("status") or "") not in ("rejected", "cancelled")]

    realized = sum(float(c["pnl"]) for c in closed)
    fees = sum(float(o.get("fee") or 0) for o in orders)
    # 平仓盈亏在配对时已经扣过买卖两边的费用,这里只作说明,不再减一次。
    # 以前写成「已实现 + 浮动 - 费用」,已实现本身已扣费 → 费用被扣了两遍,
    # 数字虽然还能对上总账,但拆解是错的,看着就像费用算重了。
    floating = (total - realized) if total is not None else None

    # 按标的:已实现贡献(准确) + 持仓(数量/成本)
    by_code: dict[str, dict] = {}
    for c in closed:
        b = by_code.setdefault(c["code"], {"code": c["code"],
                                           "name": c.get("name") or "",
                                           "realized": 0.0, "n": 0})
        b["realized"] += float(c["pnl"])
        b["n"] += 1
    positions = []
    try:
        if all_acc:
            for a in accts:
                positions.extend(store().load_positions(account=a))
        else:
            positions.extend(store().load_positions(account=acc))
        positions = [{
            "code": p.get("code"), "name": p.get("name") or "",
            "qty": int(p.get("qty") or 0), "cost": float(p.get("cost") or 0),
        } for p in positions]
    except Exception:  # noqa: BLE001
        pass

    return {
        "day": day, "account": label,
        "equity": {
            "today": eq_today,
            "prev": eq_prev,
            "change": total,
            "change_pct": (total / eq_prev * 100
                           if total is not None and eq_prev else None),
        },
        "pnl": {"total": total, "realized": realized,
                "floating": floating, "fees": fees},
        "closed": closed,
        "orders": len(orders),
        "reasons": reasons(acc, day) if not all_acc else _reasons_multi(accts, day),
        "by_code": sorted(by_code.values(), key=lambda x: -abs(x["realized"])),
        "positions": positions,
        "has_snapshot": bool(n_eq),
    }


def render(rep: dict) -> str:
    """markdown。数字缺失时写「—」,不写 0 —— 0 会被当成「没赚没亏」。"""
    def money(v):
        return "—" if v is None else f"{v:+,.0f}"

    def pct(v):
        return "—" if v is None else f"{v:+.2f}%"

    e, p = rep.get("equity") or {}, rep.get("pnl") or {}
    lines = [f"## 当日盈亏 · {rep.get('day')}", ""]
    if not rep.get("has_snapshot"):
        lines += ["> 缺权益快照,算不出总账。自动交易跑一轮后就会有。", ""]
    lines += [
        f"- 账户权益 **{money(e.get('today'))}**"
        + (f"(较上一交易日 {money(e.get('change'))},{pct(e.get('change_pct'))})"
           if e.get("change") is not None else ""),
        "",
        "| 项 | 金额 |",
        "|---|---|",
        f"| 已实现(当天平仓,已扣双边费用) | {money(p.get('realized'))} |",
        f"| 浮动(持仓市值变化,含未平仓费用) | {money(p.get('floating'))} |",
        f"| **合计** | **{money(p.get('total'))}** |",
    ]
    if p.get("total") is not None:
        # 恒等式:已实现 + 浮动 = 总账(费用已经在两项里面各扣过一次)
        s = (p.get("realized") or 0) + (p.get("floating") or 0)
        if abs(s - p["total"]) > 1.0:
            lines += ["", f"> 两项相加 {s:+,.0f},和总账差 {s - p['total']:+,.0f} —— "
                          "有出入金或快照缺失,以总账为准。"]
    if p.get("fees"):
        lines += ["", f"> 当天成交费用 {money(-(p.get('fees') or 0))} 元,"
                      "已分别计在「已实现」和「浮动」里,不再单独减一次。"]

    rs = rep.get("reasons") or []
    lines += ["", "## 操作原因", ""]
    if rs:
        lines += ["| 时间 | 代码 | 名称 | 动作 | 数量 | 价格 | 原因 |",
                  "|---|---|---|---|---|---|---|"]
        for r in rs:
            t = time.strftime("%H:%M", time.localtime(r["ts"])) if r.get("ts") else ""
            lines.append(
                f"| {t} | {r['code']} | {r.get('name') or ''} | "
                f"{'买' if r['side'] == 'buy' else '卖'} | {r['qty']} | "
                f"{r['price']:.2f} | {r['reason']} |")
    else:
        lines.append("- 今天没有成交")

    bc = rep.get("by_code") or []
    if bc:
        lines += ["", "## 按标的(已实现)", ""]
        for b in bc[:10]:
            lines.append(f"- {b['name'] or b['code']}:{b['realized']:+,.0f} 元"
                         f"({b['n']} 笔)")

    pos = rep.get("positions") or []
    if pos:
        lines += ["", "## 当前持仓", ""]
        for x in pos[:15]:
            lines.append(f"- {x['name'] or x['code']}:{x['qty']} 股,成本 {x['cost']:.2f}")
    return "\n".join(lines)
