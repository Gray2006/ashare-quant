"""本地仪表盘：只用标准库，绑定 127.0.0.1，不对外提供服务。

接口只读本地数据；回测在内存里跑，不写文件、不下单。
"""
from __future__ import annotations

import json
import math
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .. import __version__, config
from ..data import eastmoney
from ..data.store import Store
from ..engine import analysis, backtest, factors, screener
from ..feedback import consent as feedback_consent

WEB_DIR = Path(__file__).resolve().parent


def _clean(value):
    """把 NaN / numpy 类型转成合法 JSON。"""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 6)
    if hasattr(value, "item"):
        return _clean(value.item())
    return value


def _store() -> Store:
    return Store()


def overview() -> dict:
    store = _store()
    coverage = store.coverage()
    latest = coverage.get("last_date")
    movers = []
    if latest:
        rows = store.conn.execute(
            """SELECT d.code, s.name, d.close, d.pct_chg, d.amount, d.turnover
               FROM daily d LEFT JOIN stocks s ON s.code = d.code
               WHERE d.date = ? AND d.close IS NOT NULL
               ORDER BY d.pct_chg DESC""",
            (latest,),
        ).fetchall()
        movers = [
            {"code": r[0], "name": r[1] or "", "close": r[2], "pct_chg": r[3],
             "amount": r[4], "turnover": r[5]}
            for r in rows
        ]
    indices = []
    for code, name in (("000001", "上证指数"), ("000300", "沪深300"),
                       ("000905", "中证500"), ("000852", "中证1000"),
                       ("399006", "创业板指")):
        frame = store.index_panel(code)
        if frame.empty:
            continue
        last = frame.iloc[-1]
        indices.append({
            "code": code, "name": name,
            "close": float(last["close"]),
            "pct_chg": float(last["pct_chg"]) if pd.notna(last.get("pct_chg")) else None,
            "date": frame.index[-1].strftime("%Y-%m-%d"),
        })
    status = feedback_consent.status()
    return {
        "version": __version__,
        "coverage": coverage,
        "indices": indices,
        "gainers": movers[:8],
        "losers": list(reversed(movers[-8:])) if movers else [],
        "feedback": {"enabled": status["enabled"]},
        "universe": int(len(store.stocks())),
    }


def _screen_frame(store: Store, live: bool) -> pd.DataFrame:
    start = (pd.Timestamp(date.today()) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    panel = store.panel(start=start)
    if not panel:
        return pd.DataFrame()
    latest = {field: frame.ffill().iloc[-1] for field, frame in panel.items()}
    frame = pd.DataFrame(latest)
    frame.index.name = "code"
    frame = frame.reset_index()
    stocks = store.stocks()
    if not stocks.empty:
        frame = frame.merge(stocks[["code", "name", "industry"]], on="code", how="left")
    frame = screener.attach_factors(frame, panel)
    if live:
        try:
            snapshot = eastmoney.list_stocks()
        except Exception:  # noqa: BLE001
            from ..data import sina
            snapshot = sina.list_stocks()
        keep = ["code", "pe_ttm", "pb", "market_cap", "float_cap",
                "volume_ratio", "amplitude", "industry"]
        snapshot = snapshot[[c for c in keep if c in snapshot.columns]]
        frame = frame.drop(columns=[c for c in ("industry",) if c in frame.columns])
        frame = frame.merge(snapshot, on="code", how="left")
    return frame


def run_screen(expr: str, limit: int = 50, live: bool = False) -> dict:
    store = _store()
    frame = _screen_frame(store, live)
    if frame.empty:
        return {"error": "本地没有数据，先执行 aq sync"}
    try:
        selected = screener.screen(frame, expr, limit=limit, sort_by="pct_chg", ascending=False)
    except screener.ScreenError as exc:
        return {"error": f"表达式错误: {exc}"}
    columns = ["code", "name", "close", "pct_chg", "turnover", "amount",
               "pe_ttm", "market_cap", "industry"]
    columns += [name for name in factors.FACTORS if name in selected.columns]
    columns = [c for c in dict.fromkeys(columns) if c in selected.columns]
    records = selected[columns].to_dict("records")
    return {"count": len(selected), "rows": _clean(records), "columns": columns}


def run_backtest(payload: dict) -> dict:
    factor = str(payload.get("factor") or "momentum_20")
    if factor not in factors.FACTORS:
        return {"error": f"未知因子: {factor}"}
    top = int(payload.get("top") or 20)
    top = max(1, min(top, 100))
    rebalance = payload.get("rebalance") or "M"
    start = str(payload.get("start") or "2020-01-01")
    end = str(payload.get("end") or date.today().isoformat())
    benchmark = str(payload.get("benchmark") or "000300")

    store = _store()
    panel = store.panel(start=start, end=end)
    if "close" not in panel or panel["close"].empty:
        return {"error": "本地没有该区间的数据，先执行 aq sync"}
    scores = backtest.factor_scores(panel, factor)
    stocks = store.stocks()
    names = dict(zip(stocks["code"], stocks["name"])) if not stocks.empty else {}
    index_frame = store.index_panel(benchmark, start=start, end=end)
    bench = index_frame["close"] if not index_frame.empty else None
    cfg = backtest.BacktestConfig(top_n=top, rebalance=rebalance, start=start,
                                  end=end, benchmark=benchmark)
    try:
        result = backtest.run_backtest(panel, scores, cfg, names=names, benchmark=bench)
    except ValueError as exc:
        return {"error": str(exc)}

    step = max(1, len(result.equity) // 400)
    curve = []
    bench_series = result.benchmark
    for i in range(0, len(result.equity), step):
        point = {
            "date": result.equity.index[i].strftime("%Y-%m-%d"),
            "equity": float(result.equity.iloc[i]),
        }
        if bench_series is not None and i < len(bench_series):
            point["benchmark"] = float(bench_series.iloc[i])
        curve.append(point)
    holdings = [{"code": code, "name": names.get(code, ""), "shares": int(shares)}
                for code, shares in sorted(result.holdings.items())]
    return {
        "meta": {
            "factor": factor,
            "factor_label": factors.FACTORS[factor].label,
            "top": top,
            "rebalance": rebalance,
            "start": result.equity.index[0].strftime("%Y-%m-%d"),
            "end": result.equity.index[-1].strftime("%Y-%m-%d"),
            "trades": len(result.trades),
            "universe": int(panel["close"].shape[1]),
        },
        "metrics": _clean(result.metrics),
        "yearly": {str(k): float(v) for k, v in result.yearly.items()},
        "curve": curve,
        "holdings": holdings,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"ashare-quant/{__version__}"

    def log_message(self, fmt, *args):  # 安静一点
        return

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(_clean(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/overview":
                self._json(overview())
                return
            if path == "/api/factors":
                self._json(factors.list_factors())
                return
            if path == "/api/stock":
                query = parse_qs(parsed.query)
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    self._json({"error": "缺少 code"}, 400)
                    return
                try:
                    days = int((query.get("days") or ["250"])[0])
                except ValueError:
                    days = 250
                self._json(analysis.analyze(_store(), code, days=days))
                return
            if path == "/api/quote":
                query = parse_qs(parsed.query)
                codes = [c for chunk in query.get("codes", []) for c in chunk.split(",") if c]
                from ..data import sync
                self._json(sync.realtime_quotes(codes).to_dict("records"))
                return
            self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path not in ("/api/screen", "/api/backtest"):
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                self._json({"error": "请求体过大"}, 413)
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                self._json({"error": "请求体必须是 JSON 对象"}, 400)
                return
            if self.path == "/api/screen":
                expr = str(payload.get("expr") or "").strip()
                if not expr:
                    self._json({"error": "缺少 expr"}, 400)
                    return
                self._json(run_screen(expr, int(payload.get("limit") or 50),
                                      bool(payload.get("live"))))
            else:
                self._json(run_backtest(payload))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": f"参数错误: {exc}"}, 400)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"本地仪表盘: {url}")
    print("只监听本机，按 Ctrl+C 退出。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()
