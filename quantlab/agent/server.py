# -*- coding: utf-8 -*-
"""本机控制接口:让 AI 代理(或脚本)直接操作模拟盘。

设计约束:
- 只绑 127.0.0.1,不对外;必须带 token
- 校验 Host / Origin:防 DNS rebinding 与跨站写请求(2026-11-22 审计 E1)
- token 只认 X-Token 头,不收 ?token= —— 查询参数属于「简单请求」,
  浏览器不会预检,恶意页面可以直接发
- 只接受 application/json:挡住 text/plain 的跨站 form 提交
- 所有下单走同一个 RiskEngine,和界面点按钮的路径完全一致
- 默认只允许模拟盘;真钱通道需要 config 里显式打开 agent.allow_live
- 独立进程也能跑(共享同一个 SQLite),界面开着时两边看到同一份数据

用法:
    python -m quantlab.agent.server --port 8765
    curl -H "X-Token: xxx" http://127.0.0.1:8765/state
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..broker.risk import RiskEngine
from ..broker.sim import DEFAULT_ACCOUNT, SimGateway

_TOKEN: str = ""
_PORT: int = 8765
MAX_AGENT_BODY = 64 * 1024     # 代理接口没有大 body 的正当理由
DEFAULT_TOKEN = "quantlab-local"   # 历史默认值,仅用于识别并强制轮换
MIN_TOKEN_LEN = 24


def ensure_token() -> str:
    """取本机 token;没有、太短、或还是历史默认值就现场换一个并落盘。

    默认 token 写在源码里等于没有鉴权 —— 任何能读仓库的人都能驱动模拟盘。
    """
    from ..config import cfg
    c = cfg()
    tok = str(c.get("agent.token", "") or "").strip()
    if len(tok) >= MIN_TOKEN_LEN and tok != DEFAULT_TOKEN:
        return tok
    tok = secrets.token_urlsafe(32)
    c.set("agent.token", tok, save=True)
    return tok



class AgentService:
    """把「查状态 / 下单 / 管账户」收成纯函数,方便进程内和 HTTP 复用。"""

    def __init__(self, account: str = DEFAULT_ACCOUNT, allow_live: bool = False):
        self.allow_live = allow_live
        self.gw = SimGateway(account=account)
        self.risk = RiskEngine()

    # ---------------------------------------------------------------- 读
    def state(self) -> dict:
        gw = self.gw
        gw.set_mark_prices(self._quotes([p.code for p in gw.positions()]))
        acct = gw.account()
        pos = []
        for p in gw.positions():
            pos.append({"code": p.code, "name": p.name, "qty": p.qty,
                        "avail": p.avail_qty, "cost": round(p.cost, 3),
                        "last": p.last_price, "pnl": round(p.pnl, 2),
                        "pnl_pct": round(p.pnl_pct, 2),
                        "market_value": round(p.market_value, 2)})
        return {"ok": True, "account": acct["account"], "accounts": gw.list_accounts(),
                "cash": round(acct["cash"], 2), "initial": acct["initial"],
                "market_value": round(acct["market_value"], 2),
                "total": round(acct["total"], 2), "pnl": round(acct["pnl"], 2),
                "pnl_pct": round(acct["pnl_pct"], 2), "positions": pos,
                "session": self._session(), "ts": time.time()}

    def quotes(self, codes: list[str]) -> dict:
        return {"ok": True, "quotes": self._quotes(codes), "ts": time.time()}

    def orders(self, limit: int = 50) -> dict:
        from ..data.store import store
        rows = store().orders(limit, account=self.gw.account_name)
        out = []
        for r in rows:
            out.append({"id": r["id"], "time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                             time.localtime(r["ts"])),
                        "code": r["code"], "side": r["side"], "price": r["price"],
                        "qty": r["qty"], "status": r["status"],
                        "filled_price": r["filled_price"], "fee": r["fee"],
                        "remark": r["remark"]})
        return {"ok": True, "account": self.gw.account_name, "orders": out}

    def equity(self) -> dict:
        return {"ok": True, "account": self.gw.account_name,
                "curve": self.gw.equity_curve()}

    # ---------------------------------------------------------------- 自动交易
    def autotrade(self, strategy: str = "momentum20", pool: str = "hs300",
                  n_stocks: int = 60, n_hold: int = 5, per_position_pct: float = 0.18,
                  dry_run: bool = True) -> dict:
        """跑一次策略调仓。dry_run=True 只出计划不下单。"""
        from ..engine.autotrader import AutoTrader
        try:
            at = AutoTrader(self.gw, strategy, pool, n_stocks=n_stocks,
                            n_hold=int(n_hold), per_position_pct=float(per_position_pct),
                            dry_run=bool(dry_run))
            plan = at.run_once()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "dry_run": bool(dry_run), "strategy": plan.strategy,
                "summary": plan.summary(),
                "target": [{"code": c, "name": n} for c, _q, n in plan.target],
                "sells": plan.sells, "buys": plan.buys, "skipped": plan.skipped,
                "note": plan.message,
                "account": self.state()} if not dry_run else {
            "ok": True, "dry_run": True, "strategy": plan.strategy,
            "summary": plan.summary(),
            "target": [{"code": c, "name": n} for c, _q, n in plan.target],
            "sells": plan.sells, "buys": plan.buys, "skipped": plan.skipped,
            "note": plan.message}

    # ---------------------------------------------------------------- 写
    def order(self, code: str, side: str, qty: int, price: float | None = None) -> dict:
        raw = str(side).strip().lower()
        if raw in ("sell", "s", "卖", "卖出"):
            side = "sell"
        elif raw in ("buy", "b", "买", "买入"):
            side = "buy"
        else:
            # 拼错方向绝不能默认为买入
            return {"ok": False, "error": f"方向无效:{side}(只接受 buy/sell)"}
        if not code or str(code).strip() == "":
            return {"ok": False, "error": "缺少代码"}
        try:
            qty = int(qty)
        except Exception:
            return {"ok": False, "error": f"数量无效:{qty}"}
        q = self._quotes([code]).get(code) or {}
        try:
            px = float(price if price is not None else (q.get("price") or 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": f"价格无效:{price}"}
        # JSON 默认接受 NaN/Infinity 字面量,`px <= 0` 对 NaN 恒为 False,
        # 所以这里必须先判有限 —— 否则 NaN 会一路穿过风控(见 risk.py 注释)。
        if not math.isfinite(px):
            return {"ok": False, "error": "价格不是有效数字(NaN/Inf)", "risk": True}
        if px <= 0:
            return {"ok": False, "error": "取不到现价,请显式传 price"}
        decision = self.risk.check_order(
            code, side, px, int(qty), quote=q,
            positions=self.gw.positions(), daily_orders=self._daily_orders(),
            account=self.gw.account(), live=False)
        if not decision.ok:
            return {"ok": False, "error": decision.reason, "risk": True}
        o = self.gw.place_order(code, side, px, int(qty), name=q.get("name") or "")
        from ..engine.models import STATUS_FILLED
        return {"ok": o.status == STATUS_FILLED, "order_id": o.id, "status": o.status,
                "filled_price": o.filled_price, "fee": o.fee, "remark": o.remark,
                "cash_after": round(self.gw.account()["cash"], 2)}

    def switch(self, name: str) -> dict:
        ok, msg = self.gw.switch_account(name)
        return {"ok": ok, "message": msg, "account": self.gw.account_name}

    def new_account(self, name: str, cash: float) -> dict:
        ok, msg = self.gw.create_account(name, cash)
        if ok:
            self.gw.switch_account(name)
        return {"ok": ok, "message": msg}

    def reset(self, cash: float | None = None) -> dict:
        self.gw.reset(cash)
        return {"ok": True, "account": self.gw.account_name,
                "cash": round(self.gw.account()["cash"], 2)}

    # ---------------------------------------------------------------- 内部
    def _quotes(self, codes: list[str]) -> dict:
        codes = [c for c in dict.fromkeys(codes) if c]
        if not codes:
            return {}
        from ..data import em
        try:
            return em.quotes(codes) or {}
        except Exception:
            try:
                from ..data import sina
                return sina.quotes(codes) or {}
            except Exception:
                return {}

    def _daily_orders(self) -> int:
        from ..data.store import store
        return store().orders_today_count(account=self.gw.account_name)

    @staticmethod
    def _session() -> str:
        from ..utils import SESSION_LABEL, session_state
        return SESSION_LABEL.get(session_state(), "")


def _make_handler(svc: AgentService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GrayAgent/1.1"

        def log_message(self, fmt, *args):  # 安静点
            pass

        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if code >= 400:
                # 错误响应会在读完 body 之前返回(比如 413/415);不关连接的话
                # 客户端还在写,Windows 上会直接 RST,看起来像服务崩了。
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---------------------------------------------------- 三层门
        def _host_ok(self) -> bool:
            """Host 必须是本机。挡 DNS rebinding:攻击者域名解析到 127.0.0.1 时,
            浏览器发来的 Host 仍是攻击者域名,不会是我们认的这几个。"""
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return False
            allowed = {f"127.0.0.1:{_PORT}", f"localhost:{_PORT}", f"[::1]:{_PORT}",
                       "127.0.0.1", "localhost", "[::1]"}
            return host in allowed

        def _origin_ok(self) -> bool:
            """Origin 存在就必须同源。浏览器跨站写请求一定带 Origin;
            本机脚本/curl 不带,放行。"""
            origin = (self.headers.get("Origin") or "").strip().lower()
            if not origin:
                return True
            return origin in {f"http://127.0.0.1:{_PORT}", f"http://localhost:{_PORT}"}

        def _auth(self) -> bool:
            """只认 X-Token 头。查询参数 ?token= 已移除 —— 那是简单请求,
            浏览器不预检,等于把 token 暴露给任意网页。"""
            if not _TOKEN:
                return False
            tok = (self.headers.get("X-Token") or "").strip()
            if not tok:
                return False
            return hmac.compare_digest(tok, _TOKEN)

        def _gate(self) -> bool:
            if not self._host_ok():
                self._send({"ok": False, "error": "Host 不被允许"}, 403)
                return False
            if not self._origin_ok():
                self._send({"ok": False, "error": "跨站请求已拒绝"}, 403)
                return False
            return True

        def do_OPTIONS(self) -> None:  # noqa: N802
            # 没有跨域场景;明确拒绝,别让浏览器以为这里支持 CORS
            self._send({"ok": False, "error": "不支持跨域"}, 403)

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._gate():
                return
            if u.path == "/health":
                return self._send({"ok": True, "service": "gray-agent"})
            if not self._auth():
                return self._send({"ok": False, "error": "token 无效"}, 401)
            try:
                if u.path == "/state":
                    return self._send(svc.state())
                if u.path == "/quotes":
                    codes = [c for c in (q.get("codes", [""])[0]).split(",") if c]
                    return self._send(svc.quotes(codes))
                if u.path == "/orders":
                    return self._send(svc.orders(int((q.get("limit") or [50])[0])))
                if u.path == "/equity":
                    return self._send(svc.equity())
                if u.path == "/accounts":
                    return self._send({"ok": True, "accounts": svc.gw.list_accounts(),
                                       "current": svc.gw.account_name})
                if u.path == "/strategy":
                    from ..engine import strategy as st_mod
                    return self._send({"ok": True, "strategies": st_mod.list_strategies()})
                return self._send({"ok": False, "error": f"未知路径 {u.path}"}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if not self._gate():
                return
            if not self._auth():
                return self._send({"ok": False, "error": "token 无效"}, 401)
            # 只收 JSON:跨站的 form/text 提交(简单请求)在这里被挡掉
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send({"ok": False, "error": "只接受 application/json"}, 415)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send({"ok": False, "error": "Content-Length 非法"}, 400)
            if n < 0 or n > MAX_AGENT_BODY:
                # 先把 body 读掉再回 413:直接关连接会让客户端在还在写的时候撞 RST
                # (Windows 上表现为 ConnectionAbortedError),拿不到明确的 413。
                # 读的量封顶 1MB,别让超大请求把内存吃穿。
                try:
                    self.rfile.read(min(n, 1 << 20))
                except Exception:  # noqa: BLE001
                    pass
                return self._send({"ok": False, "error": "请求体过大或长度非法"}, 413)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return self._send({"ok": False, "error": "body 不是合法 JSON"}, 400)
            if not isinstance(body, dict):
                return self._send({"ok": False, "error": "body 必须是 JSON 对象"}, 400)
            try:
                if u.path == "/order":
                    return self._send(svc.order(body.get("code", ""), body.get("side", "buy"),
                                                int(body.get("qty") or 0), body.get("price")))
                if u.path == "/switch":
                    return self._send(svc.switch(body.get("account", "")))
                if u.path == "/account":
                    return self._send(svc.new_account(body.get("name", ""),
                                                      float(body.get("cash") or 0)))
                if u.path == "/reset":
                    return self._send(svc.reset(body.get("cash")))
                if u.path == "/autotrade":
                    return self._send(svc.autotrade(
                        strategy=body.get("strategy") or "momentum20",
                        pool=body.get("pool") or "hs300",
                        n_stocks=int(body.get("n_stocks") or 60),
                        n_hold=int(body.get("n_hold") or 5),
                        per_position_pct=float(body.get("per_position_pct") or 0.18),
                        dry_run=bool(body.get("dry_run", True))))
                return self._send({"ok": False, "error": f"未知路径 {u.path}"}, 404)
            except Exception as e:  # noqa: BLE001
                return self._send({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def make_server(port: int = 8765, token: str = "", host: str = "127.0.0.1",
                account: str = DEFAULT_ACCOUNT) -> ThreadingHTTPServer:
    global _TOKEN, _PORT
    token = (token or "").strip()
    if len(token) < MIN_TOKEN_LEN:
        raise ValueError(f"agent token 至少 {MIN_TOKEN_LEN} 位,拒绝用弱 token 起服务")
    _TOKEN = token
    _PORT = int(port)
    svc = AgentService(account=account)
    httpd = ThreadingHTTPServer((host, port), _make_handler(svc))
    httpd.daemon_threads = True
    return httpd


def start_background(port: int = 8765, token: str = "",
                     account: str = DEFAULT_ACCOUNT) -> ThreadingHTTPServer | None:
    """给界面用:端口占用就返回 None,不打断主程序。"""
    try:
        httpd = make_server(port, token, account=account)
    except OSError:
        return None
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="agent-http")
    t.start()
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="Gray 本机控制接口")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="", help="不传则用配置文件里的,没有就生成")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    a = ap.parse_args()
    token = a.token.strip() or ensure_token()
    httpd = make_server(a.port, token, account=a.account)
    print(f"agent 已在 http://127.0.0.1:{a.port} 启动 (账户={a.account})")
    print(f"  token = {token}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
