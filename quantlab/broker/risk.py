# -*- coding: utf-8 -*-
"""下单前风控。任何一条不通过就不发单,并给出人话原因。

这些不是「建议」,是硬闸:同花顺自动下单最容易出事的地方是
价格填错、数量填错、重复提交,所以全部拦在网关之前。

A 股与加密现货是两套规则,按代码形态自动分流:
  * A 股:涨跌停、T+1、100 股整手、交易时段
  * 加密(BTC-USDT 这类):7×24、无涨跌停/T+1,按 lotSz/minSz/tickSz 校验
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import cfg
from ..engine import exchange
from ..utils import normalize_code

# 数值风控的写法约定(2026-11-22 审计 E2):
#   一切比较必须用「好值才通过」,不能写成「坏值才拦截」。
#   IEEE-754 下 NaN 参与任何比较都返回 False,所以 `if price <= 0: 拒绝`
#   对 NaN 完全不触发 —— 价格 NaN 会一路穿过涨跌停、金额、资金、仓位所有闸。
# 下面所有逐项检查前先过 _finite(),比较式也一律写成 `if not (0 < x <= cap)`。
BAD_NUM = "委托参数不是有效数字(NaN/Inf),已拒绝"


def _finite(*vals: object) -> bool:
    """全部是有限实数才为真。字符串、None、NaN、Inf 都算坏值。"""
    for v in vals:
        if isinstance(v, bool):
            return False
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        if not math.isfinite(f):
            return False
    return True


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""
    adjusted_qty: float | None = None


class RiskEngine:
    def __init__(self, config=None):
        self.cfg = config or cfg()

    def _r(self, key: str, default=None):
        return self.cfg.get(f"risk.{key}", default)

    def check_order(self, code: str, side: str, price: float, qty: float,
                    quote: dict | None = None, positions: list | None = None,
                    daily_orders: int = 0, account: dict | None = None,
                    live: bool = True,
                    max_daily_orders: int | None = None,
                    spec: dict | None = None) -> RiskDecision:
        # 入口统一闸:NaN/Inf/非数字/布尔一律在分流之前拒掉。
        # 分到哪条规则都不能先过这一关 —— 否则两条规则各漏一半。
        if not _finite(price):
            return RiskDecision(False, f"价格{BAD_NUM}")
        if not _finite(qty):
            return RiskDecision(False, f"数量{BAD_NUM}")
        # 统一成 float 再往下走:否则 "10" 这种可转换但比较会 TypeError 的输入
        # 会在后面的 `<` 比较里炸掉,而不是被风控明确拒绝。
        price = float(price)
        qty = float(qty)
        if exchange.is_crypto(code):
            return self._check_crypto(code, side, price, qty, quote=quote,
                                      positions=positions,
                                      daily_orders=daily_orders, account=account,
                                      max_daily_orders=max_daily_orders, spec=spec)
        return self._check_ashare(code, side, price, qty, quote=quote,
                                  positions=positions, daily_orders=daily_orders,
                                  account=account, live=live,
                                  max_daily_orders=max_daily_orders)

    def check_portfolio(self, positions: list, account: dict,
                        industry_map: dict[str, str] | None = None) -> list[dict]:
        """组合层面检查:单票仓位、行业集中度。返回警告列表。"""
        total = float((account or {}).get("total") or 0)
        if total <= 0 or not positions:
            return []
        warns = []
        max_pos = float(self._r("max_position_pct", 0.25))
        for p in positions:
            mv = float(getattr(p, "market_value", 0) or 0)
            pct = mv / total
            if pct > max_pos:
                warns.append({"level": "warn", "code": getattr(p, "code", ""),
                              "msg": f"单票仓位 {pct:.1%} 超过 {max_pos:.0%}"})
        if industry_map:
            max_ind = float(self._r("max_industry_pct", 0.40))
            by_ind: dict[str, float] = {}
            for p in positions:
                ind = industry_map.get(getattr(p, "code", ""), "其他")
                by_ind[ind] = by_ind.get(ind, 0.0) + float(
                    getattr(p, "market_value", 0) or 0)
            for ind, mv in by_ind.items():
                pct = mv / total
                if pct > max_ind:
                    warns.append({"level": "warn", "industry": ind,
                                  "msg": f"行业「{ind}」仓位 {pct:.1%} 超过 {max_ind:.0%}"})
        return warns

    # ------------------------------------------------------------ A 股
    def _check_ashare(self, code: str, side: str, price: float, qty: int,
                      quote: dict | None = None, positions: list | None = None,
                      daily_orders: int = 0, account: dict | None = None,
                      live: bool = True,
                      max_daily_orders: int | None = None) -> RiskDecision:
        code = normalize_code(code)
        if not (0 < price):
            return RiskDecision(False, "价格必须大于 0")
        if not (0 < qty):
            return RiskDecision(False, "数量必须大于 0")
        held_for_qty = None
        if side == "sell" and positions is not None:
            h = next((x for x in positions if normalize_code(x.code) == code), None)
            held_for_qty = h.qty if h else None
        ok_qty, why = exchange.valid_qty(code, qty, side, held_for_qty)
        if not ok_qty:
            return RiskDecision(False, why)

        # 模拟盘允许收盘后练习;真钱通道必须卡交易时段(按交易所时段表判断)
        if live and not self._r("allow_outside_trading_hours", False):
            ph = exchange.session_phase()
            if not ph["can_order"]:
                return RiskDecision(False, f"当前{ph['phase']},不可委托"
                                           f"(可在设置里放开)")

        amount = price * qty
        cap = float(self._r("max_order_amount", 50000))
        if amount > cap * 1.0001:
            max_qty = int(cap / price / 100) * 100
            if max_qty < 100:
                return RiskDecision(False, f"单笔金额 {amount:,.0f} 超过上限 {cap:,.0f},且不够 100 股")
            return RiskDecision(False,
                                f"单笔金额 {amount:,.0f} 超过上限 {cap:,.0f};按上限最多 {max_qty} 股",
                                adjusted_qty=max_qty)

        # 每日笔数上限:<=0 表示不限,交给用户自己决定要不要全自动
        max_daily = int(max_daily_orders if max_daily_orders is not None
                        else self._r("max_daily_orders", 30))
        if max_daily > 0 and daily_orders >= max_daily:
            return RiskDecision(False, f"今日已发 {daily_orders} 单,达到上限 {max_daily} 单")

        if quote:
            last = quote.get("price") or quote.get("last")
            if last is not None and not _finite(last):
                return RiskDecision(False, "行情价格异常(NaN/Inf),拒绝下单")
            if last:
                if not (last > 0):
                    return RiskDecision(False, "行情价格异常(<=0),拒绝下单")
                dev = abs(price / last - 1)
                max_dev = float(self._r("max_price_deviation", 0.03))
                if not (dev <= max_dev):
                    return RiskDecision(False,
                                        f"委托价 {price:.2f} 偏离现价 {last:.2f} 达 {dev*100:.2f}%"
                                        f",超过 {max_dev*100:.1f}% 上限")
            pre = quote.get("pre_close")
            if pre is not None and not _finite(pre):
                return RiskDecision(False, "昨收价异常(NaN/Inf),拒绝下单")
            if pre:
                up, down = exchange.limit_prices(pre, code, quote.get("name"))
                if up is not None and side == "buy" and price > up + 1e-6 \
                        and self._r("forbid_limit_up_buy", True):
                    return RiskDecision(False, f"买入价高于涨停价 {up}")
                if down is not None and side == "sell" and price < down - 1e-6 \
                        and self._r("forbid_limit_down_sell", True):
                    return RiskDecision(False, f"卖出价低于跌停价 {down}")

        if side == "sell" and positions is not None:
            held = next((p for p in positions if normalize_code(p.code) == code), None)
            if held is None:
                return RiskDecision(False, "没有该股票持仓,不能卖出")
            if not _finite(getattr(held, "qty", None), getattr(held, "avail_qty", None)):
                return RiskDecision(False, "持仓数据异常(NaN/Inf),拒绝下单,请先修复账户")
            if not (qty <= held.avail_qty):
                return RiskDecision(False,
                                    f"可卖数量不足(持仓 {held.qty},可卖 {held.avail_qty},"
                                    f"T+1 当日买入不可卖)")

        if side == "buy" and account and positions is not None:
            cash = float(account.get("cash") or 0)
            total_raw = float(account.get("total") or 0)
            if not _finite(cash, total_raw):
                return RiskDecision(False, "账户数据异常(NaN/Inf),拒绝下单,请先修复账户")
            if not (amount <= cash):
                return RiskDecision(False, f"可用资金不足(需 {amount:,.0f},可用 {cash:,.0f})")
            ratio_cap = float(self._r("max_position_ratio", 0.35))
            total = total_raw or 1.0
            held = next((p for p in positions if normalize_code(p.code) == code), None)
            cur_val = float(held.market_value if held else 0.0)
            if not _finite(cur_val):
                return RiskDecision(False, "持仓市值异常(NaN/Inf),拒绝下单,请先修复账户")
            if not ((cur_val + amount) / total <= ratio_cap):
                return RiskDecision(False,
                                    f"该股仓位将达 {(cur_val + amount) / total * 100:.1f}%"
                                    f",超过单票上限 {ratio_cap*100:.0f}%")
        return RiskDecision(True, "")

    # ------------------------------------------------------------ 加密现货
    def _check_crypto(self, code: str, side: str, price: float, qty: float,
                      quote: dict | None = None, positions: list | None = None,
                      daily_orders: int = 0, account: dict | None = None,
                      max_daily_orders: int | None = None,
                      spec: dict | None = None) -> RiskDecision:
        inst = str(code).strip().upper()
        spec = spec or {}
        lot = float(spec.get("lot_sz") or 0) or 1e-8
        min_sz = float(spec.get("min_sz") or 0)
        tick = float(spec.get("tick_sz") or 0)
        if not _finite(lot, min_sz, tick):
            return RiskDecision(False, "交易规格异常(NaN/Inf),拒绝下单")

        if not (price >= 0):
            return RiskDecision(False, "委托价不能为负(0 = 市价)")
        if price > 0 and tick > 0:
            px = exchange.floor_step(price, tick)
            if abs(px - price) > tick * 1e-6:
                return RiskDecision(False,
                                    f"委托价必须是 {tick:g} 的整数倍;按步长应填 {px:g}")

        ok, why, adj = exchange.crypto_valid_qty(qty, lot, min_sz, side)
        if not ok:
            return RiskDecision(False, why, adjusted_qty=adj or None)

        last = float((quote or {}).get("price") or 0)
        if not _finite(last) or not _finite(adj):
            return RiskDecision(False, "行情或数量异常(NaN/Inf),拒绝下单")
        ref = price if price > 0 else last
        amount = ref * adj if ref else 0.0
        if not _finite(amount):
            return RiskDecision(False, "委托金额异常(NaN/Inf),拒绝下单")

        cap = float(self._r("max_crypto_order_amount",
                            self._r("max_order_amount", 50000)) or 0)
        if not _finite(cap):
            return RiskDecision(False, "单笔上限配置异常(NaN/Inf),拒绝下单")
        if cap > 0 and not (amount <= cap * 1.0001):
            max_qty = exchange.floor_step(cap / ref, lot) if ref else 0.0
            if max_qty < min_sz or max_qty <= 0:
                return RiskDecision(False,
                                    f"单笔金额 {amount:,.2f} 超过上限 {cap:,.2f},"
                                    f"且不够最小申报量 {min_sz:g}")
            return RiskDecision(False,
                                f"单笔金额 {amount:,.2f} 超过上限 {cap:,.2f};"
                                f"按上限最多 {max_qty:g}",
                                adjusted_qty=max_qty)

        max_daily = int(max_daily_orders if max_daily_orders is not None
                        else self._r("max_daily_orders", 30))
        if max_daily > 0 and daily_orders >= max_daily:
            return RiskDecision(False, f"今日已发 {daily_orders} 单,达到上限 {max_daily} 单")

        if price > 0 and last:
            if not (last > 0):
                return RiskDecision(False, "行情价格异常(<=0),拒绝下单")
            dev = abs(price / last - 1)
            max_dev = float(self._r("max_price_deviation", 0.03))
            # 加密波动大,偏离上限放宽到 3 倍(默认 9%),仍挡明显填错
            max_dev = max(max_dev, float(self._r("crypto_price_deviation", 0.09)))
            if not (dev <= max_dev):
                return RiskDecision(False,
                                    f"委托价 {price:g} 偏离现价 {last:g} 达 {dev*100:.2f}%"
                                    f",超过 {max_dev*100:.1f}% 上限")

        if positions is not None:
            held = next((p for p in positions if str(p.code).upper() == inst), None)
            held_qty = float(held.qty) if held else 0.0
            avail = float(held.avail_qty) if held else 0.0
            if not _finite(held_qty, avail):
                return RiskDecision(False, "持仓数据异常(NaN/Inf),拒绝下单,请先修复账户")
            if side == "sell":
                if not (held_qty > 0):
                    return RiskDecision(False, f"没有 {inst} 持仓,不能卖出")
                if not (adj <= avail + 1e-12):
                    return RiskDecision(False,
                                        f"可用数量不足(持仓 {held_qty:g},可用 {avail:g})")

        if side == "buy" and account is not None:
            cash = float(account.get("cash") or 0)
            total_raw = float(account.get("total") or 0)
            if not _finite(cash, total_raw):
                return RiskDecision(False, "账户数据异常(NaN/Inf),拒绝下单,请先修复账户")
            total = total_raw or 1.0
            need = amount * (1 + 0.001) if amount else 0.0
            if not (need <= cash + 1e-9):
                return RiskDecision(False,
                                    f"可用 {self._r('crypto_quote', 'USDT')} 不足"
                                    f"(需 {need:,.2f},可用 {cash:,.2f})")
            ratio_cap = float(self._r("max_position_ratio", 0.35))
            cur_val = 0.0
            if positions is not None:
                held = next((p for p in positions if str(p.code).upper() == inst), None)
                cur_val = float(held.market_value if held else 0.0)
            if not _finite(cur_val):
                return RiskDecision(False, "持仓市值异常(NaN/Inf),拒绝下单,请先修复账户")
            if not ((cur_val + amount) / total <= ratio_cap):
                return RiskDecision(False,
                                    f"{inst} 仓位将达 {(cur_val + amount) / total * 100:.1f}%"
                                    f",超过单币上限 {ratio_cap*100:.0f}%")
        return RiskDecision(True, "")
