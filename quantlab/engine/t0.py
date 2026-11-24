# -*- coding: utf-8 -*-
"""个股做T:底仓日内高抛低吸。

A 股是 T+1,「做T」靠底仓绕开:卖出的是昨天以前买的底仓,当天买回的份额当天不能再卖,
所以同一份底仓一天只能做一轮 —— 这个约束写死在引擎里,不是靠调用方自觉。

两种方向:
  反T(reverse):先卖后买。开盘后冲高到 anchor*(1+min_pct) 挂卖,回落 take_pct 挂买回。
                买不回来 → 收盘按最后价强制回补,记「尾盘回补」,底仓不变。
  正T(forward):先买后卖。回落到 anchor*(1-min_pct) 挂买,反弹 take_pct 挂卖底仓。
                卖不掉 → 收盘按最后价强制卖出,记「尾盘卖出」,底仓不变。

撮合模型:限价单按 bar 的 high/low 判成交,成交价就是挂单价(不偷价)。
同一根 bar 里既能卖又能买时,按「先卖后买」处理 —— 反T 的挂单价天然一高一低,
顺序对结果没影响;日线近似模式会显式给出先到哪一侧。

日线近似(daily bars)是给批量筛选用:把一根日K拆成两段(O→H→L→C 或 O→L→H→C),
精度不如 5 分钟线,但**不会偷看未来**。真回测请喂分钟线。
注意:日线近似的路径顺序会系统性地偏袒其中一种方向(阴线偏正T、阳线偏反T),
日线单看某一方向的正收益基本是路径假设的产物,不能当结论。

不做的事:不做择时、不猜方向、不改变底仓数量(未平掉的腿一律收盘强制平掉)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import exchange


@dataclass(frozen=True)
class T0Config:
    base_shares: int = 1000        # 底仓股数
    ratio: float = 0.30            # 每轮动用底仓的比例
    min_pct: float = 0.010         # 触发偏离(相对 anchor)
    take_pct: float = 0.004        # 回补/止盈幅度(相对成交价)
    max_rounds: int = 1            # 每个交易日最多几轮(买回即锁,通常 1)
    mode: str = "reverse"          # reverse=反T / forward=正T
    anchor: str = "open"           # 触发基准:open / prev_close
    fee_rate: float = 0.00025      # 佣金(单边)
    stamp: float = 0.0005          # 印花税(仅卖出)
    transfer: float = 0.00001      # 过户费(买卖都收)
    slippage: float = 0.0005       # 滑点(单边,按成交额估)
    min_fee: float = 5.0           # 单笔最低佣金
    skip_limit: bool = True        # 一字板/涨跌停不开新仓
    # 撮合加严:touch=触碰挂单价即成交(偏乐观) / through=必须穿过挂单价(排队未成交更接近真实)
    fill_mode: str = "touch"
    better_fill: bool = True       # 开盘价已越过挂单价时按开盘价成交(真实:集合竞价成交价更好)
    # 自适应触发:min_pct 按近 atr_window 日 ATR% 缩放,避免固定阈值在低波动票上永不触发
    auto_trigger: bool = False
    atr_mult: float = 0.5
    atr_window: int = 20
    min_pct_floor: float = 0.004
    min_pct_cap: float = 0.030
    # 未平掉的腿:False=当日收盘强平(原口径) / True=留仓到次日继续挂,超时才强平
    carry_unfilled: bool = False
    max_carry_days: int = 3


@dataclass
class DayResult:
    date: str
    mode: str
    qty: int = 0
    sell_px: float | None = None
    buy_px: float | None = None
    filled: bool = False           # 一轮完整做完
    rounds: int = 0
    gross: float = 0.0             # 未扣成本的差价
    cost: float = 0.0
    net: float = 0.0
    base_end: int = 0
    min_pct: float = 0.0           # 当日实际触发幅度(自适应时逐日不同)
    note: str = ""

    def as_dict(self) -> dict:
        return {"date": self.date, "mode": self.mode, "qty": self.qty,
                "sell_px": self.sell_px, "buy_px": self.buy_px,
                "filled": self.filled, "rounds": self.rounds,
                "gross": round(self.gross, 2), "cost": round(self.cost, 2),
                "net": round(self.net, 2), "base_end": self.base_end,
                "min_pct": self.min_pct, "note": self.note}


def buy_fee(px: float, qty: int, cfg: T0Config) -> float:
    amt = px * qty
    return max(amt * cfg.fee_rate, cfg.min_fee) + amt * cfg.transfer + amt * cfg.slippage


def sell_fee(px: float, qty: int, cfg: T0Config) -> float:
    amt = px * qty
    return (max(amt * cfg.fee_rate, cfg.min_fee) + amt * cfg.stamp
            + amt * cfg.transfer + amt * cfg.slippage)


def round_qty(shares: int, code: str = "600000") -> int:
    """按交易所申报单位取整(创业板/科创板 200 股起、1 股递增)。"""
    base, step = exchange.lot_of(code)
    if shares < base:
        return 0
    return int(shares // step * step)


def _hit_high(bar: dict, px: float, cfg: T0Config) -> float | None:
    """卖单是否成交,返回成交价;没成交返回 None。"""
    hi = float(bar.get("high") or 0.0)
    hit = hi > px if cfg.fill_mode == "through" else hi >= px
    if not hit:
        return None
    op = float(bar.get("open") or 0.0)
    if cfg.better_fill and op > px:
        return op
    return px


def _hit_low(bar: dict, px: float, cfg: T0Config) -> float | None:
    """买单是否成交,返回成交价;没成交返回 None。"""
    lo = float(bar.get("low") or 0.0)
    hit = lo < px if cfg.fill_mode == "through" else lo <= px
    if not hit:
        return None
    op = float(bar.get("open") or 0.0)
    if cfg.better_fill and 0 < op < px:
        return op
    return px


def atr_pct_series(days: list[dict], window: int = 20) -> list[float | None]:
    """逐日 ATR%(只用当日之前的数据,不看未来)。样本不够给 None。

    days 可以是分钟线日(带 bars,没有 high/low)或日线;缺 high/low 时从 bars 取极值。
    """
    trs: list[float | None] = []
    for d in days:
        hi = float(d.get("high") or 0.0)
        lo = float(d.get("low") or 0.0)
        bars = d.get("bars") or []
        if (hi <= 0 or lo <= 0) and bars:
            hi = max(float(b.get("high") or 0) for b in bars)
            lo = min(float(b.get("low") or 0) for b in bars if float(b.get("low") or 0) > 0)
        pc = float(d.get("pre_close") or 0.0)
        if hi <= 0 or lo <= 0 or pc <= 0:
            trs.append(None)
        else:
            trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    out: list[float | None] = []
    need = max(5, window // 4)
    for i, d in enumerate(days):
        hist = [t for t in trs[max(0, i - window):i] if t]
        close = float(d.get("close") or 0.0) or float(d.get("pre_close") or 0.0)
        if len(hist) < need or close <= 0:
            out.append(None)
        else:
            out.append(sum(hist) / len(hist) / close)
    return out


class T0Engine:
    """单只股票、单日粒度。喂分钟线或日线近似都行。"""

    def __init__(self, code: str, cfg: T0Config):
        self.code = code
        self.cfg = cfg
        self.base = int(cfg.base_shares)
        # carry_unfilled 模式下未平掉的腿:[{side, px, qty, target, date, age}]
        self.pending: list[dict] = []

    # ------------------------------------------------------------ 单日
    def run_day(self, bars: list[dict], pre_close: float,
                date: str = "", min_pct: float | None = None) -> DayResult:
        cfg = self.cfg
        mp = cfg.min_pct if min_pct is None else float(min_pct)
        res = DayResult(date=date, mode=cfg.mode, base_end=self.base, min_pct=mp)
        if not bars or pre_close <= 0:
            res.note = "无行情"
            return res

        if self.pending:
            self._work_pending(bars, res)
            # 当天先处理留仓,处理完也不开新轮 —— 一天一轮,口径才清楚
            res.base_end = self.base
            if not res.note:
                res.note = "留仓未清,今日不开新轮"
            return res

        qty = round_qty(int(self.base * cfg.ratio), self.code)
        if qty <= 0:
            res.note = "底仓太小,不足一手"
            return res
        res.qty = qty

        up, down = exchange.limit_prices(pre_close, self.code)
        first = bars[0]
        anchor = float(first.get("open") or 0.0)
        if cfg.anchor == "prev_close":
            anchor = float(pre_close)
        if anchor <= 0:
            res.note = "无有效基准价"
            return res
        if cfg.skip_limit and up is not None and anchor >= up:
            res.note = "开盘涨停,不挂单"
            return res
        if cfg.skip_limit and down is not None and anchor <= down:
            res.note = "开盘跌停,不挂单"
            return res

        if cfg.mode == "forward":
            self._forward(bars, anchor, qty, res, mp)
        else:
            self._reverse(bars, anchor, qty, res, mp)
        res.base_end = self.base
        return res

    def _work_pending(self, bars, res: DayResult) -> None:
        """处理留仓的腿:到价就平,超时按收盘强平,还没到就继续留。"""
        cfg = self.cfg
        still: list[dict] = []
        for leg in self.pending:
            leg["age"] += 1
            qty, px0, target = leg["qty"], leg["px"], leg["target"]
            close = float(bars[-1].get("close") or 0.0)
            px = None
            for bar in bars:
                px = (_hit_low(bar, target, cfg) if leg["side"] == "sell"
                      else _hit_high(bar, target, cfg))
                if px is not None:
                    break
            forced = px is None and leg["age"] >= cfg.max_carry_days
            if px is None and not forced:
                still.append(leg)
                continue
            fill = px if px is not None else close
            if leg["side"] == "sell":            # 之前卖了,现在买回
                gross = (px0 - fill) * qty
                cost = sell_fee(px0, qty, cfg) + buy_fee(fill, qty, cfg)
                self.base += qty
            else:                                # 之前买了,现在卖出
                gross = (fill - px0) * qty
                cost = buy_fee(px0, qty, cfg) + sell_fee(fill, qty, cfg)
                self.base -= qty
            res.gross += gross
            res.cost += cost
            res.net += gross - cost
            res.filled = True
            res.rounds += 1
            res.note = ("留仓超时强平" if forced else
                        ("次日回补" if leg["side"] == "sell" else "次日卖出"))
        self.pending = still

    def _reverse(self, bars, anchor, qty, res: DayResult, mp: float) -> None:
        cfg = self.cfg
        sell_px = round(anchor * (1 + mp), 2)
        stage = 0
        for bar in bars:
            if stage == 0:
                px = _hit_high(bar, sell_px, cfg)
                if px is not None:
                    res.sell_px = round(px, 2)
                    stage = 1
                    continue
            if stage == 1:
                buy_px = round(res.sell_px * (1 - cfg.take_pct), 2)
                px = _hit_low(bar, buy_px, cfg)
                if px is not None:
                    res.buy_px = round(px, 2)
                    stage = 2
                    break
        if stage < 2:
            if stage == 1:
                if cfg.carry_unfilled:
                    # 留仓:不追高强平,底仓先减掉,次日继续挂回补价
                    self.base -= qty
                    self.pending.append({
                        "side": "sell", "px": res.sell_px, "qty": qty,
                        "target": round(res.sell_px * (1 - cfg.take_pct), 2),
                        "date": res.date, "age": 0})
                    res.cost = sell_fee(res.sell_px, qty, cfg)
                    res.net = -res.cost
                    res.note = "留仓待回补"
                    return
                # 卖了没到回补价:收盘前必须平掉,否则就是隔夜减仓(不是做T)
                close = float(bars[-1].get("close") or 0.0)
                res.buy_px = close
                res.gross = (res.sell_px - close) * qty
                res.cost = sell_fee(res.sell_px, qty, cfg) + buy_fee(close, qty, cfg)
                res.net = res.gross - res.cost
                res.note = "尾盘回补(未到目标价)"
            else:
                res.note = "未触发"
            return
        res.filled = True
        res.rounds = 1
        res.gross = (res.sell_px - res.buy_px) * qty
        res.cost = sell_fee(res.sell_px, qty, cfg) + buy_fee(res.buy_px, qty, cfg)
        res.net = res.gross - res.cost

    def _forward(self, bars, anchor, qty, res: DayResult, mp: float) -> None:
        cfg = self.cfg
        buy_px = round(anchor * (1 - mp), 2)
        stage = 0
        for bar in bars:
            if stage == 0:
                px = _hit_low(bar, buy_px, cfg)
                if px is not None:
                    res.buy_px = round(px, 2)
                    stage = 1
                    continue
            if stage == 1:
                sell_px = round(res.buy_px * (1 + cfg.take_pct), 2)
                px = _hit_high(bar, sell_px, cfg)
                if px is not None:
                    res.sell_px = round(px, 2)
                    stage = 2
                    break
        if stage < 2:
            if stage == 1:
                if cfg.carry_unfilled:
                    # 留仓:不砸盘强卖,底仓先加上,次日继续挂卖出价
                    self.base += qty
                    self.pending.append({
                        "side": "buy", "px": res.buy_px, "qty": qty,
                        "target": round(res.buy_px * (1 + cfg.take_pct), 2),
                        "date": res.date, "age": 0})
                    res.cost = buy_fee(res.buy_px, qty, cfg)
                    res.net = -res.cost
                    res.note = "留仓待卖出"
                    return
                # 买了没到卖出价:收盘前平掉(T+1 允许卖昨天的底仓,今天买的也不能留着)
                close = float(bars[-1].get("close") or 0.0)
                res.sell_px = close
                res.gross = (close - res.buy_px) * qty
                res.cost = buy_fee(res.buy_px, qty, cfg) + sell_fee(close, qty, cfg)
                res.net = res.gross - res.cost
                res.note = "尾盘卖出(未到目标价)"
            else:
                res.note = "未触发"
            return
        res.filled = True
        res.rounds = 1
        res.gross = (res.sell_px - res.buy_px) * qty
        res.cost = sell_fee(res.sell_px, qty, cfg) + buy_fee(res.buy_px, qty, cfg)
        res.net = res.gross - res.cost

    # ------------------------------------------------------------ 多日
    def run(self, days: list[dict]) -> list[DayResult]:
        """days: [{date, open, high, low, close, pre_close, bars?}] 按时间升序。

        有 bars 就用 bars(分钟线),没有就用日线拆两段近似。
        """
        out: list[DayResult] = []
        atr = atr_pct_series(days, self.cfg.atr_window) if self.cfg.auto_trigger else []
        for i, d in enumerate(days):
            bars = d.get("bars") or daily_pseudo_bars(d)
            mp = None
            if self.cfg.auto_trigger:
                a = atr[i] if i < len(atr) else None
                if a:
                    mp = min(max(self.cfg.atr_mult * a, self.cfg.min_pct_floor),
                             self.cfg.min_pct_cap)
            out.append(self.run_day(bars, float(d.get("pre_close") or 0),
                                    str(d.get("date") or ""), min_pct=mp))
        if self.pending and out:
            # 期末把没平的腿按最后一天收盘强平,保证底仓归位
            last = out[-1]
            bars = days[-1].get("bars") or daily_pseudo_bars(days[-1])
            close = float(bars[-1].get("close") or 0.0) if bars else 0.0
            for leg in self.pending:
                qty, px0 = leg["qty"], leg["px"]
                if leg["side"] == "sell":
                    gross = (px0 - close) * qty
                    cost = buy_fee(close, qty, self.cfg)
                    self.base += qty
                else:
                    gross = (close - px0) * qty
                    cost = sell_fee(close, qty, self.cfg)
                    self.base -= qty
                last.gross += gross
                last.cost += cost
                last.net += gross - cost
                last.note = (last.note + "+期末强平") if last.note else "期末强平"
            self.pending = []
            last.base_end = self.base
        return out


def daily_pseudo_bars(d: dict) -> list[dict]:
    """把一根日K拆成两段,保留「先到哪一侧」的信息。

    阳线按 O→H→L→C,阴线按 O→L→H→C。这不是真实路径,是显式假设 ——
    批量筛选够用,回测结论请用分钟线复核。
    """
    o = float(d.get("open") or 0)
    h = float(d.get("high") or 0)
    l = float(d.get("low") or 0)
    c = float(d.get("close") or 0)
    if min(o, h, l, c) <= 0:
        return []
    if c >= o:      # 阳线:先冲高,再回落
        return [{"open": o, "high": h, "low": o, "close": h},
                {"open": h, "high": h, "low": l, "close": c}]
    return [{"open": o, "high": o, "low": l, "close": l},      # 阴线:先砸低,再反弹
            {"open": l, "high": h, "low": l, "close": c}]


def summarize(records: list[DayResult], base_value: float,
              trading_days: int | None = None) -> dict:
    """把逐日结果汇总成给人看的指标。base_value = 底仓市值(算收益率的分母)。"""
    n = len(records)
    traded = [r for r in records if r.rounds > 0]
    net = sum(r.net for r in records)
    gross = sum(r.gross for r in records)
    cost = sum(r.cost for r in records)
    wins = sum(1 for r in traded if r.net > 0)
    days = trading_days or max(n, 1)
    ann = (net / base_value) * (252.0 / days) if base_value > 0 else 0.0
    return {
        "days": n,
        "traded_days": len(traded),
        "trade_rate": round(len(traded) / n, 3) if n else 0.0,
        "win_days": wins,
        "win_rate": round(wins / len(traded), 3) if traded else 0.0,
        "gross": round(gross, 2),
        "cost": round(cost, 2),
        "net": round(net, 2),
        "net_per_traded_day": round(net / len(traded), 2) if traded else 0.0,
        "net_pct": round(net / base_value, 4) if base_value > 0 else 0.0,
        "annual": round(ann, 4),
        "unfilled": sum(1 for r in records if r.note.startswith("尾盘回补")),
        "overweight": sum(1 for r in records if r.note.startswith("尾盘卖出")),
    }

def min5_days(code: str) -> list[dict]:
    """从 kline_cache 取 5 分钟线,按交易日分组,补 pre_close(前一日收盘)。

    返回 [{date, bars, pre_close, close}];没有 5 分钟数据的日子跳过。
    """
    from ..data.store import store

    rows = store().conn.execute(
        "select bar_time, open, high, low, close from kline_cache "
        "where klt=5 and fqt=1 and code=? order by bar_time", (code,)).fetchall()
    days: dict[str, list[dict]] = {}
    for r in rows:
        day = str(r["bar_time"])[:10]
        days.setdefault(day, []).append(
            {"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])})
    out = []
    pre = None
    for day in sorted(days):
        if pre is None:                    # 第一天没有前收,跳过
            pre = days[day][-1]["close"]
            continue
        out.append({"date": day, "bars": days[day], "pre_close": pre,
                    "close": days[day][-1]["close"]})
        pre = days[day][-1]["close"]
    return out


def daily_days(code: str, start: str = "2020-01-01") -> list[dict]:
    """日线(前复权),补 pre_close;返回 [{date, open, high, low, close, pre_close}]。"""
    from ..data.store import store

    rows = store().conn.execute(
        "select bar_time, open, high, low, close from kline_cache "
        "where klt=101 and fqt=1 and code=? and bar_time>=? order by bar_time",
        (code, start)).fetchall()
    out = []
    prev = None
    for r in rows:
        if prev is not None:
            out.append({"date": r["bar_time"], "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"]), "pre_close": prev})
        prev = float(r["close"])
    return out
