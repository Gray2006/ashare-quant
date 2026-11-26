# -*- coding: utf-8 -*-
"""OKX 现货网关:真实下单 + 真实账户,支持模拟交易(demo)环境。

与 SimGateway 的区别:
  * 24×7,没有 T+1、涨跌停、交易时段这些 A 股规则;数量是小数,按 lotSz 取整
  * 下单是**不可逆的外部动作**:没收到回执时状态记 unverified(待核对),
    绝不自动重试下单,避免同一笔重复成交
  * 持仓来自账户余额(现货没有「开仓价」概念),成本价由本程序记的成交推算;
    不是本程序买的币会显示成本=现价,盈亏 0,并在备注里说明

密钥只从 config.json 的 broker.okx 读,不落日志、不进异常消息。
"""
from __future__ import annotations

import json
import time

from ..config import cfg
from ..data import okx as okx_api
from ..data.okx import OKXClient, OKXError, decimals_of, fmt_step, normalize_inst
from ..data.store import store
from ..engine.models import (STATUS_CANCELLED, STATUS_FILLED, STATUS_PARTIAL,
                             STATUS_REJECTED, STATUS_SUBMITTED, STATUS_UNKNOWN,
                             Order, Position, SIDE_BUY)
from .base import Gateway

COST_KEY = "okx_pos:{inst}"
DUST = 1e-9

_STATE_MAP = {
    "live": STATUS_SUBMITTED,
    "partially_filled": STATUS_PARTIAL,
    "filled": STATUS_FILLED,
    "canceled": STATUS_CANCELLED,
    "mmp_canceled": STATUS_CANCELLED,
}

_STATUS_LABEL = {
    STATUS_SUBMITTED: "已挂单", STATUS_PARTIAL: "部分成交", STATUS_FILLED: "已成交",
    STATUS_CANCELLED: "已撤单", STATUS_REJECTED: "被拒", STATUS_UNKNOWN: "待核对",
}


class OKXGateway(Gateway):
    name = "okx"
    # 类属性给基类/别处按名字取用;实例上由下面的 property 覆盖,不是笔误
    display_name = "OKX 现货"  # noqa: F811
    is_live = True
    need_confirm = True

    def __init__(self, config=None, demo: bool | None = None):
        self.cfg = config or cfg()
        o = self.cfg.get("broker.okx", {}) or {}
        self.demo = bool(o.get("demo", True)) if demo is None else bool(demo)
        self.ref_quote = str(o.get("ref_quote", "USDT") or "USDT").upper()
        self.inst_type = str(o.get("inst_type", "SPOT") or "SPOT")
        # 凭证优先从 okx.key(DPAPI 加密)读,config.json 只作向后兼容。
        # 2026-11-26 明文密钥被误提交到 GitHub 之后改的。
        try:
            from ..security import okx_creds as _oc
            _c = _oc.load_creds()
        except Exception:  # noqa: BLE001
            _c = {"api_key": str(o.get("api_key", "") or ""),
                  "api_secret": str(o.get("api_secret", "") or ""),
                  "passphrase": str(o.get("passphrase", "") or "")}
        self.client = OKXClient(
            api_key=str(_c.get("api_key") or ""),
            api_secret=str(_c.get("api_secret") or ""),
            passphrase=str(_c.get("passphrase") or ""),
            demo=self.demo,
            proxy=str(o.get("proxy", "") or ""),
            base_url=str(o.get("base_url", "") or ""),
        )
        self.st = store()
        self.account_name = "okx-demo" if self.demo else "okx-live"
        self.acct_level = ""
        self.perm = ""
        self._specs: dict[str, dict] = {}
        self._marks: dict[str, float] = {}
        self._bal: dict = {}
        self._bal_ts = 0.0
        self._pending: list[Order] = []
        self._pending_ts = 0.0
        self.last_error = ""

    # ------------------------------------------------------------ 工具
    @property
    def display_name(self) -> str:  # noqa: D401,F811
        return "OKX 现货 · " + ("模拟交易" if self.demo else "实盘")

    def _inst(self, code: str) -> str:
        raw = str(code or "").strip()
        inst = normalize_inst(raw)
        if "-" not in inst and inst:
            inst = f"{inst}-{self.ref_quote}"
        return inst

    def spec(self, code: str) -> dict:
        inst = self._inst(code)
        if inst in self._specs:
            return self._specs[inst]
        s = okx_api.spec_of(inst, self.inst_type)
        self._specs[inst] = s
        return s

    def fee_rate(self) -> float:
        """现货默认 0.1%(OKX 普通用户档位),只用于资金预估,真实费用以成交为准。"""
        try:
            return float(self.cfg.get("broker.okx.fee_rate", 0.001))
        except Exception:
            return 0.001

    # ------------------------------------------------------------ 连接
    def connect(self) -> tuple[bool, str]:
        if not (self.client.api_key and self.client.api_secret and self.client.passphrase):
            return False, "未配置 OKX API Key / Secret / Passphrase(设置页 → OKX)"
        if not self.client.api_key.strip().startswith(("a", "b")) and len(self.client.api_key) < 8:
            return False, "API Key 看起来不像 OKX 的(通常 36 位)"
        try:
            cfgs = self.client.private_request("GET", "/api/v5/account/config")
            c = (cfgs or [{}])[0]
            self.acct_level = str(c.get("acctLv") or "")
            self.perm = str(c.get("perm") or "")
            if "trade" not in self.perm:
                return False, f"该 API Key 没有交易权限(perm={self.perm or '空'})"
            self._refresh_balance(force=True)
            total = float(self._bal.get("totalEq") or 0)
            env = "模拟交易" if self.demo else "实盘"
            return True, (f"OKX {env} 已连接 · 账户等级 {self.acct_level or '?'} · "
                          f"总权益 {total:,.2f} USD")
        except OKXError as e:
            self.last_error = e.human()
            return False, e.human()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return False, f"连接失败:{e}"

    # ------------------------------------------------------------ 账户
    def _refresh_balance(self, force: bool = False) -> dict:
        if not force and time.monotonic() - self._bal_ts < 3.0 and self._bal:
            return self._bal
        data = self.client.private_request("GET", "/api/v5/account/balance")
        self._bal = (data or [{}])[0]
        self._bal_ts = time.monotonic()
        return self._bal

    def _detail(self, ccy: str) -> dict:
        for d in self._bal.get("details") or []:
            if (d.get("ccy") or "").upper() == ccy.upper():
                return d
        return {}

    def _cost(self, inst: str) -> float | None:
        rec = self.st.get_account(COST_KEY.format(inst=inst), None) or {}
        try:
            c = float(rec.get("cost") or 0)
            return c if c > 0 else None
        except Exception:
            return None

    def _save_cost(self, inst: str, qty: float, cost: float) -> None:
        if qty <= DUST:
            self.st.set_account(COST_KEY.format(inst=inst), {})
        else:
            self.st.set_account(COST_KEY.format(inst=inst), {"qty": qty, "cost": cost})

    def positions(self) -> list[Position]:
        self._refresh_balance()
        out: list[Position] = []
        for d in self._bal.get("details") or []:
            ccy = str(d.get("ccy") or "").upper()
            try:
                eq = float(d.get("eq") or 0)
                avail = float(d.get("availBal") or 0)
                eq_usd = float(d.get("eqUsd") or 0)
            except (TypeError, ValueError):
                continue
            if not ccy or eq <= DUST or ccy == self.ref_quote:
                continue
            inst = f"{ccy}-{self.ref_quote}"
            last = self._marks.get(inst)
            if last is None:
                try:
                    last = float(okx_api.ticker(inst).get("price") or 0) or None
                except Exception:
                    last = None
            cost = self._cost(inst)
            if cost is None:
                # 不是本程序开的仓:没有成本信息,用现值折算(盈亏显示 0)
                cost = (eq_usd / eq) if (eq_usd and eq) else (last or 0.0)
            out.append(Position(code=inst, name=ccy, qty=round(eq, 12),
                                avail_qty=round(avail, 12), cost=float(cost),
                                last_price=last))
        return out

    def set_mark_prices(self, quotes: dict) -> None:
        for k, q in (quotes or {}).items():
            try:
                self._marks[self._inst(k)] = float(q.get("price") or 0)
            except Exception:
                continue

    def account(self) -> dict:
        self._refresh_balance()
        total = float(self._bal.get("totalEq") or 0)
        usdt = self._detail(self.ref_quote)
        cash = float(usdt.get("availBal") or 0)
        frozen = float(usdt.get("frozenBal") or 0)
        eq_quote = float(usdt.get("eqUsd") or 0)
        mv = max(0.0, total - eq_quote)
        pnl = 0.0
        for p in self.positions():
            if p.last_price and p.cost:
                pnl += (p.last_price - p.cost) * p.qty
        base = total - pnl
        return {
            "cash": cash, "frozen": frozen, "market_value": mv,
            "total": total, "pnl": pnl,
            "pnl_pct": (pnl / base * 100) if base > 0 else 0.0,
            "account": self.account_name, "initial": 0.0,
            "note": ("模拟交易(虚拟资金)" if self.demo else "实盘 · 真实资金")
                    + f" · {self.ref_quote} 计价",
        }

    # ------------------------------------------------------------ 下单
    def place_order(self, code: str, side: str, price: float, qty: float,
                    name: str = "") -> Order:
        inst = self._inst(code)
        spec = self.spec(inst)
        o = Order(code=inst, name=name or inst, side=side,
                  price=float(price or 0), qty=float(qty or 0), gateway=self.name)
        try:
            lot = float(spec.get("lot_sz") or 1e-8)
            tick = float(spec.get("tick_sz") or 0.01)
            min_sz = float(spec.get("min_sz") or 0)
            sz = okx_api.floor_step(o.qty, lot)
            px = okx_api.floor_step(o.price, tick) if o.price > 0 else 0.0
            if sz <= 0 or sz < min_sz:
                return self._reject(o, f"数量 {sz:g} 低于最小下单量 {min_sz:g}"
                                        f"(步长 {lot:g})")
            market = px <= 0
            body = {
                "instId": inst,
                "tdMode": "cash",
                "side": side,
                "ordType": "market" if market else "limit",
                "sz": fmt_step(sz, lot),
            }
            if not market:
                body["px"] = fmt_step(px, tick)
            if market and side == SIDE_BUY:
                # 市价买入默认 sz 是计价币金额;显式按基础币数量下单,口径统一
                body["tgtCcy"] = "base_ccy"
            data = self.client.private_request("POST", "/api/v5/trade/order", body=body)
            r = (data or [{}])[0]
            if str(r.get("sCode", "0")) != "0":
                return self._reject(o, f"OKX 拒单 {r.get('sCode')}:{r.get('sMsg') or ''}")
            ord_id = str(r.get("ordId") or "")
            o.id = self.st.insert_order(self._rec(o, extra={"ordId": ord_id, "req": body}))
            self._settle(o, ord_id, inst, market)
            return o
        except OKXError as e:
            if e.unverified:
                o.status = STATUS_UNKNOWN
                o.remark = f"未收到回执,可能已提交:{e.human()}"
            else:
                o.status = STATUS_REJECTED
                o.remark = e.human()
            o.id = self.st.insert_order(self._rec(o, extra={"error": e.code}))
            return o
        except Exception as e:  # noqa: BLE001
            o.status = STATUS_UNKNOWN
            o.remark = f"未知异常,请到 OKX 核对:{e}"
            o.id = self.st.insert_order(self._rec(o))
            return o

    def _rec(self, o: Order, extra: dict | None = None) -> dict:
        rec = o.to_dict()
        rec["account"] = self.account_name
        rec["raw"] = dict(o.raw or {}, **(extra or {}))
        return rec

    def _reject(self, o: Order, why: str) -> Order:
        o.status = STATUS_REJECTED
        o.remark = why
        o.id = self.st.insert_order(self._rec(o))
        return o

    def _settle(self, o: Order, ord_id: str, inst: str, market: bool) -> None:
        """回查订单:挂单/成交/部分成交都按真实状态落库。"""
        detail: dict = {}
        for attempt in range(3):
            try:
                data = self.client.private_request(
                    "GET", "/api/v5/trade/order", {"instId": inst, "ordId": ord_id})
                detail = (data or [{}])[0]
                break
            except OKXError as e:
                if attempt == 2:
                    o.status = STATUS_UNKNOWN
                    o.remark = f"已提交 ordId={ord_id},回查失败:{e.human()}"
                    self.st.update_order(o.id, status=o.status, remark=o.remark)
                    return
                time.sleep(0.35 * (attempt + 1))
        state = str(detail.get("state") or "")
        o.status = _STATE_MAP.get(state, STATUS_UNKNOWN)
        o.raw = dict(o.raw or {}, state=state, ordId=ord_id)
        fill_px = float(detail.get("avgPx") or detail.get("fillPx") or 0)
        fill_sz = float(detail.get("accFillSz") or 0)
        if fill_sz > 0:
            o.filled_qty = fill_sz
            o.filled_price = fill_px or o.price
            o.fee = self._fee_quote(detail, fill_px)
            self._apply_fill(o, inst, side=o.side, qty=fill_sz,
                             price=o.filled_price, fee=o.fee)
        if not o.remark:
            o.remark = f"ordId {ord_id} · {_STATUS_LABEL.get(o.status, o.status)}"
            if o.status == STATUS_SUBMITTED:
                o.remark += "(未成交,挂在盘口)"
        self.st.update_order(o.id, status=o.status, filled_price=o.filled_price,
                             filled_qty=o.filled_qty, fee=o.fee, remark=o.remark)

    @staticmethod
    def _fee_quote(detail: dict, fill_px: float) -> float:
        """把手续费折算成计价币(现货买入手续费常以基础币计)。"""
        try:
            fee = abs(float(detail.get("fee") or 0))
            ccy = str(detail.get("feeCcy") or "")
        except (TypeError, ValueError):
            return 0.0
        if not fee:
            return 0.0
        base = str(detail.get("instId") or "").split("-")[0]
        if ccy and base and ccy.upper() == base.upper() and fill_px:
            return fee * fill_px
        return fee

    def _apply_fill(self, o: Order, inst: str, side: str, qty: float,
                    price: float, fee: float) -> None:
        """按成交维护成本(现货没有持仓成本字段,只能自己记)。"""
        rec = self.st.get_account(COST_KEY.format(inst=inst), None) or {}
        try:
            q0 = float(rec.get("qty") or 0)
            c0 = float(rec.get("cost") or 0)
        except (TypeError, ValueError):
            q0, c0 = 0.0, 0.0
        if side == SIDE_BUY:
            q1 = q0 + qty
            c1 = ((q0 * c0) + (price * qty) + fee) / q1 if q1 > 0 else price
            self._save_cost(inst, q1, c1)
        else:
            q1 = max(0.0, q0 - qty)
            self._save_cost(inst, q1, c0 if q1 > 0 else 0.0)

    # ------------------------------------------------------------ 委托/撤单
    def pending_orders(self, force: bool = False) -> list[Order]:
        if not force and time.monotonic() - self._pending_ts < 2.0:
            return list(self._pending)
        try:
            data = self.client.private_request(
                "GET", "/api/v5/trade/orders-pending", {"instType": self.inst_type})
        except OKXError as e:
            self.last_error = e.human()
            return list(self._pending)
        out = []
        for r in data or []:
            o = Order(code=str(r.get("instId")), name=str(r.get("instId")),
                      side=str(r.get("side")), price=float(r.get("px") or 0),
                      qty=float(r.get("sz") or 0), gateway=self.name,
                      status=_STATE_MAP.get(str(r.get("state")), STATUS_SUBMITTED),
                      filled_qty=float(r.get("accFillSz") or 0),
                      filled_price=float(r.get("avgPx") or 0) or None,
                      fee=float(r.get("fee") or 0),
                      remark=f"已成交 {r.get('accFillSz') or 0}",
                      raw=dict(r))
            out.append(o)
        self._pending = out
        self._pending_ts = time.monotonic()
        return list(out)

    def sync_pending_to_store(self) -> int:
        """把未成交委托的最新状态写回本地订单表,供「委托记录」显示。

        只更新本地已经存在、且仍是 submitted/partial/unverified 的那些单;
        以 ordId 匹配,不靠时间/价格猜。
        """
        try:
            rows = {r["id"]: r for r in self.st.orders(120)}
        except Exception:
            return 0
        changed = 0
        for p in self.pending_orders(force=True):
            oid = str((p.raw or {}).get("ordId") or "")
            if not oid:
                continue
            for r in rows.values():
                try:
                    raw = json.loads(r.get("raw") or "{}")
                except Exception:
                    raw = {}
                if str(raw.get("ordId") or "") != oid:
                    continue
                if r.get("status") not in (STATUS_SUBMITTED, STATUS_PARTIAL, STATUS_UNKNOWN):
                    continue
                self.st.update_order(r["id"], status=p.status, filled_qty=p.filled_qty,
                                     filled_price=p.filled_price, remark=p.remark)
                changed += 1
                break
        return changed

    def cancel_all(self) -> tuple[bool, str]:
        try:
            rows = self.pending_orders(force=True)
            if not rows:
                return True, "没有未成交委托"
            body = [{"instId": o.code, "ordId": str((o.raw or {}).get("ordId") or "")}
                    for o in rows]
            body = [b for b in body if b["ordId"]]
            if not body:
                return False, "未取到 ordId,请到 OKX 手动撤销"
            data = self.client.private_request("POST", "/api/v5/trade/cancel-batch-orders",
                                               body=body)
            ok = sum(1 for r in data or [] if str(r.get("sCode", "0")) == "0")
            self._pending_ts = 0.0
            return ok > 0, f"已撤销 {ok}/{len(body)} 笔"
        except OKXError as e:
            return False, e.human()

    def sync(self) -> tuple[bool, str]:
        try:
            self._refresh_balance(force=True)
            return True, "余额已同步"
        except OKXError as e:
            return False, e.human()

    def close(self) -> None:
        try:
            self.client.sess.close()
        except Exception:
            pass


def decimals_for(code: str) -> tuple[int, int]:
    """(价格小数位, 数量小数位) —— 界面 spinbox 用。"""
    s = okx_api.spec_of(normalize_inst(code))
    return decimals_of(s.get("tick_sz")), decimals_of(s.get("lot_sz"))
