"""命令行入口：aq。"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import date
from pathlib import Path

import pandas as pd

from . import __version__, config
from .data import eastmoney, sync
from .data.store import Store
from .engine import backtest, factors, report, screener
from .feedback import client as feedback_client
from .feedback import consent as feedback_consent


def _setup_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _fmt(value, kind: str = "num") -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if kind == "pct":
        return f"{value * 100:.2f}%"
    if kind == "pctp":  # 已经是百分数，不再乘 100
        return f"{value:.2f}%"
    if kind == "num":
        return f"{value:.2f}"
    if kind == "int":
        return f"{int(value)}"
    if kind == "money":
        return f"{value / 1e8:.2f}亿"
    return str(value)


def _print_table(frame: pd.DataFrame, columns: list[tuple[str, str, str]]) -> None:
    if frame.empty:
        print("（无数据）")
        return
    rows = []
    for _, record in frame.iterrows():
        rows.append([_fmt(record.get(name), kind) for name, _, kind in columns])
    headers = [title for _, title, _ in columns]
    widths = [max(_display_width(header), *(_display_width(row[i]) for row in rows))
              for i, header in enumerate(headers)]
    print("  ".join(_pad(header, widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))


def _display_width(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(text))


def _store() -> Store:
    return Store()


def _check_data(store: Store) -> bool:
    coverage = store.coverage()
    if coverage["rows"] == 0:
        print("本地还没有行情数据。先运行：aq sync --limit 300")
        return False
    return True


# ---------------- 命令实现 ----------------

def cmd_init(args) -> int:
    config.ensure_home()
    cfg = config.load()
    if args.start:
        cfg["sync"]["start"] = args.start
    config.save(cfg)
    print(f"配置目录: {config.home()}")
    print(f"配置文件: {config.config_path()}")
    print(f"本地数据库: {config.db_path()}")
    print("下一步: aq sync --limit 300   # 先拉 300 只股票试试")
    return 0


def cmd_sync(args) -> int:
    store = _store()
    start = args.start or config.get("sync.start", "2019-01-01")
    if args.days:
        start = (pd.Timestamp(date.today()) - pd.Timedelta(days=args.days)).strftime("%Y-%m-%d")

    def progress(index: int, total: int, code: str) -> None:
        if index == 1 or index % 50 == 0 or index == total:
            print(f"\r同步 {index}/{total}  {code}", end="", flush=True)

    print(f"同步区间: {start} ~ 今天")
    result = sync.sync_all(
        store,
        start=start,
        full=args.full,
        limit=args.limit,
        with_universe=not args.no_universe,
        progress=progress,
    )
    print()
    if "universe" in result:
        print(f"股票列表: {result['universe']} 只")
    bars = result.get("bars", {})
    print(f"日线: 新增/更新 {bars.get('bars', 0)} 行，失败 {bars.get('failed', 0)} 只，"
          f"跳过 {bars.get('skipped', 0)} 只，用时 {bars.get('seconds', 0)}s")
    for error in bars.get("errors", [])[:5]:
        print(f"  ! {error}")
    print(f"基准指数: {result.get('benchmarks', 0)} 行")
    coverage = store.coverage()
    print(f"本地覆盖: {coverage['codes_with_bars']} 只 / {coverage['trading_days']} 个交易日 "
          f"({coverage['first_date']} ~ {coverage['last_date']})")
    return 0


def cmd_quote(args) -> int:
    codes = [code for code in args.codes if code]
    frame = sync.realtime_quotes(codes)
    if frame.empty:
        print("没有拿到行情。检查网络或代码是否正确。")
        return 1
    columns = [
        ("code", "代码", "str"), ("name", "名称", "str"), ("price", "现价", "num"),
        ("pct_chg", "涨跌幅", "pct"), ("turnover", "换手", "num"),
        ("pe_ttm", "PE", "num"), ("pb", "PB", "num"),
        ("amount", "成交额", "money"), ("market_cap", "总市值", "money"),
    ]
    _print_table(frame, columns)
    return 0


def _screen_frame(store: Store, live: bool) -> pd.DataFrame:
    panel = store.panel(start=(pd.Timestamp(date.today()) - pd.Timedelta(days=400)).strftime("%Y-%m-%d"))
    if not panel:
        return pd.DataFrame()
    latest = {}
    for field, frame in panel.items():
        series = frame.ffill().iloc[-1]
        latest[field] = series
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
            keep = ["code", "name", "pe_ttm", "pb", "market_cap", "float_cap",
                    "volume_ratio", "amplitude", "industry", "region"]
            snapshot = snapshot[[c for c in keep if c in snapshot.columns]]
            frame = frame.drop(columns=[c for c in ("name", "industry") if c in frame.columns])
            frame = frame.merge(snapshot, on="code", how="left")
        except Exception as exc:  # noqa: BLE001
            print(f"在线快照获取失败，改用本地字段: {exc}")
    return frame


def cmd_screen(args) -> int:
    store = _store()
    if not _check_data(store):
        return 1
    frame = _screen_frame(store, live=args.live)
    if frame.empty:
        print("本地没有可用数据。")
        return 1
    try:
        selected = screener.screen(
            frame, args.expr, limit=args.limit,
            sort_by=args.sort, ascending=args.asc,
        )
    except screener.ScreenError as exc:
        print(f"表达式错误: {exc}")
        return 2
    columns = [
        ("code", "代码", "str"), ("name", "名称", "str"),
        ("close", "收盘", "num"), ("pct_chg", "涨跌幅", "pctp"),
        ("turnover", "换手", "num"), ("amount", "成交额", "money"),
    ]
    if "pe_ttm" in selected.columns:
        columns.append(("pe_ttm", "PE", "num"))
    if args.expr:
        for name in factors.FACTORS:
            if name in args.expr and name in selected.columns:
                columns.append((name, name, "num"))
    _print_table(selected, columns)
    print(f"\n命中 {len(selected)} 只。筛选条件: {args.expr}")
    return 0


def cmd_factors(args) -> int:
    items = factors.list_factors()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    frame = pd.DataFrame(items)
    _print_table(frame, [
        ("name", "因子", "str"), ("label", "名称", "str"),
        ("category", "类别", "str"), ("direction", "方向", "int"),
        ("lookback", "回看", "int"), ("description", "说明", "str"),
    ])
    print("\n方向 1 = 数值越大越好，-1 = 越小越好。")
    return 0


def _run_backtest(args) -> tuple[backtest.BacktestResult, dict]:
    store = _store()
    if not _check_data(store):
        raise SystemExit(1)
    start = args.start or "2020-01-01"
    end = args.end or date.today().isoformat()
    panel = store.panel(start=start, end=end)
    if "close" not in panel:
        raise SystemExit("本地数据为空，先运行 aq sync")
    scores = backtest.factor_scores(panel, args.factor)
    stocks = store.stocks()
    names = dict(zip(stocks["code"], stocks["name"])) if not stocks.empty else {}
    benchmark_frame = store.index_panel(args.benchmark, start=start, end=end)
    benchmark = benchmark_frame["close"] if not benchmark_frame.empty else None
    cfg = backtest.BacktestConfig(
        top_n=args.top,
        rebalance=args.rebalance,
        start=start,
        end=end,
        fee_bps=args.fee,
        stamp_bps=args.stamp,
        slippage_bps=args.slippage,
        benchmark=args.benchmark,
    )
    result = backtest.run_backtest(panel, scores, cfg, names=names, benchmark=benchmark)
    meta = {
        "title": f"{factors.FACTORS[args.factor].label} 因子选股",
        "factor": args.factor,
        "factor_label": factors.FACTORS[args.factor].label,
        "top_n": cfg.top_n,
        "rebalance": cfg.rebalance,
        "fee_bps": cfg.fee_bps,
        "stamp_bps": cfg.stamp_bps,
        "slippage_bps": cfg.slippage_bps,
        "universe": int(panel["close"].shape[1]),
    }
    return result, meta


def _print_backtest(result: backtest.BacktestResult) -> None:
    stats = result.metrics
    print(f"区间 {result.equity.index[0].date()} ~ {result.equity.index[-1].date()}  "
          f"交易日 {len(result.equity)}  "
          f"期末净值 {stats.get('final_value', 0) / result.config.initial_capital:.3f}")
    print(f"累计 {_fmt(stats.get('total_return'), 'pct'):>8}   "
          f"年化 {_fmt(stats.get('annual_return'), 'pct'):>8}   "
          f"夏普 {_fmt(stats.get('sharpe')):>6}   "
          f"回撤 {_fmt(stats.get('max_drawdown'), 'pct'):>8}   "
          f"换手 {_fmt(stats.get('turnover_annual'), 'num')}x")
    if "benchmark_return" in stats:
        print(f"基准 {_fmt(stats.get('benchmark_return'), 'pct'):>8}   "
              f"超额 {_fmt(stats.get('excess_return'), 'pct'):>8}   "
              f"Beta {_fmt(stats.get('beta')):>6}   "
              f"Alpha {_fmt(stats.get('alpha_annual'), 'pct'):>8}")
    print(f"成交 {len(result.trades)} 笔，期末持仓 {len(result.holdings)} 只")


def cmd_backtest(args) -> int:
    result, meta = _run_backtest(args)
    _print_backtest(result)
    if args.trades:
        path = Path(args.trades)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result.trades).to_csv(path, index=False, encoding="utf-8-sig")
        print(f"成交明细: {path}")
    if not args.no_report:
        out = Path(args.out or "reports") / f"{args.factor}-{date.today().isoformat()}"
        md_path, svg_path = report.write_report(result, meta, out)
        print(f"报告: {md_path}")
        print(f"曲线: {svg_path}")
    return 0


def cmd_feedback(args) -> int:
    if args.action == "status":
        status = feedback_consent.status()
        print(f"数据回流: {'开启' if status['enabled'] else '关闭'}")
        if status["enabled"]:
            print(f"接收地址: {status['endpoint']}")
            print(f"同意版本: {status['consent_version']}  时间: {status['consent_at']}")
        else:
            print("默认关闭。开启前请先看 `aq feedback policy`。")
        return 0
    if args.action == "policy":
        print(feedback_consent.CONSENT_TEXT)
        return 0
    if args.action == "enable":
        if not args.endpoint:
            print("需要提供接收地址：aq feedback enable https://example.com/v1/feedback")
            return 2
        print(feedback_consent.CONSENT_TEXT)
        print("\n同意以上条款并开启？本条命令即表示同意。")
        status = feedback_consent.enable(args.endpoint)
        print(f"已开启: {status['endpoint']}")
        return 0
    if args.action == "disable":
        feedback_consent.disable()
        print("已关闭数据回流，不再发送。")
        return 0
    if args.action in ("preview", "send"):
        result, meta = _run_backtest(args)
        if args.action == "preview":
            print(feedback_client.preview(result, meta))
            path = feedback_client.local_only(feedback_client.build_payload(result, meta))
            print(f"\n（预览已另存: {path}）")
            return 0
        try:
            response = feedback_client.submit(result, meta)
        except feedback_client.FeedbackError as exc:
            print(f"未发送: {exc}")
            return 1
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0
    print("未知动作")
    return 2


def cmd_status(args) -> int:
    store = _store()
    coverage = store.coverage()
    print(f"ashare-quant {__version__}")
    print(f"配置目录: {config.home()}")
    print(f"数据库  : {config.db_path()}")
    print(f"股票列表: {len(store.stocks())} 只")
    print(f"日线覆盖: {coverage['codes_with_bars']} 只 / "
          f"{coverage['trading_days']} 个交易日 / {coverage['rows']} 行")
    print(f"最新日期: {coverage['last_date'] or '无'}")
    status = feedback_consent.status()
    print(f"数据回流: {'开启 -> ' + status['endpoint'] if status['enabled'] else '关闭'}")
    return 0


def cmd_serve(args) -> int:
    from .web.server import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_doctor(args) -> int:
    print(f"ashare-quant {__version__}")
    print(f"Python {sys.version.split()[0]}")
    print(f"配置目录: {config.home()}")
    print(f"数据库: {config.db_path()}")
    store = _store()
    coverage = store.coverage()
    print(f"本地数据: {coverage['codes_with_bars']} 只 / {coverage['trading_days']} 个交易日 / "
          f"{coverage['rows']} 行")
    if coverage["last_date"]:
        print(f"最新日期: {coverage['last_date']}")
    status = feedback_consent.status()
    print(f"数据回流: {'开启 -> ' + status['endpoint'] if status['enabled'] else '关闭'}")
    if args.network:
        try:
            frame = eastmoney.index_quotes(["000300"])
            if frame.empty:
                print("网络: 接口返回为空")
                return 1
            print(f"网络: 正常（沪深300 {frame.iloc[0]['price']}）")
        except Exception as exc:  # noqa: BLE001
            print(f"网络: 不通（{exc}）")
            return 1
    return 0


# ---------------- 参数解析 ----------------

def _add_backtest_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--factor", default="momentum_20", choices=list(factors.FACTORS),
                        help="因子名称，默认 momentum_20")
    parser.add_argument("--top", type=int, default=20, help="持仓数量，默认 20")
    parser.add_argument("--rebalance", default="M", help="调仓频率：W/M/Q 或整数交易日")
    parser.add_argument("--start", default="2020-01-01", help="开始日期")
    parser.add_argument("--end", default=None, help="结束日期")
    parser.add_argument("--benchmark", default="000300", help="基准指数，默认 000300")
    parser.add_argument("--fee", type=float, default=3.0, help="佣金 bp，默认 3")
    parser.add_argument("--stamp", type=float, default=10.0, help="印花税 bp，默认 10")
    parser.add_argument("--slippage", type=float, default=5.0, help="滑点 bp，默认 5")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aq",
        description="A股行情分析、因子选股与回测。不含自动交易、加密货币、同花顺接入。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              aq init
              aq sync --limit 300
              aq quote 600519 000001
              aq screen "pct_chg > 2 and between(turnover, 1, 10)" --limit 20
              aq backtest --factor momentum_20 --top 20 --rebalance M
              aq serve
        """),
    )
    parser.add_argument("--version", action="version", version=f"ashare-quant {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化配置目录")
    p.add_argument("--start", default=None, help="历史数据起始日期")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="查看本地数据覆盖与回流状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sync", help="同步股票列表与日线（公开接口，限速）")
    p.add_argument("--start", default=None, help="起始日期，默认读配置")
    p.add_argument("--days", type=int, default=None, help="只同步最近 N 个自然日")
    p.add_argument("--limit", type=int, default=None, help="只同步前 N 只股票，便于试用")
    p.add_argument("--full", action="store_true", help="忽略本地进度，全量重拉")
    p.add_argument("--no-universe", action="store_true", help="不更新股票列表")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("quote", help="实时行情快照")
    p.add_argument("codes", nargs="+", help="股票代码，如 600519 000001")
    p.set_defaults(func=cmd_quote)

    p = sub.add_parser("screen", help="按表达式选股（本地数据，可选在线快照）")
    p.add_argument("expr", help="筛选表达式，例如 \"pct_chg > 2 and pe_ttm < 30\"")
    p.add_argument("--limit", type=int, default=30, help="最多显示多少只")
    p.add_argument("--sort", default=None, help="排序字段")
    p.add_argument("--asc", action="store_true", help="升序")
    p.add_argument("--live", action="store_true", help="拉取在线快照补 PE/PB/市值/行业")
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("factors", help="列出内置因子")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_factors)

    p = sub.add_parser("backtest", help="因子回测（T+1、费用、涨跌停、整手）")
    _add_backtest_args(p)
    p.add_argument("--out", default=None, help="报告输出目录")
    p.add_argument("--no-report", action="store_true", help="不写报告")
    p.add_argument("--trades", default=None, help="成交明细 CSV 路径")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("feedback", help="数据回流（默认关闭，只发聚合指标）")
    p.add_argument("action", choices=["status", "policy", "preview", "enable", "disable", "send"])
    p.add_argument("endpoint", nargs="?", default=None, help="enable 时的接收地址")
    _add_backtest_args(p)
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("serve", help="启动本地仪表盘")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认只本机")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("doctor", help="自检：环境、数据、网络")
    p.add_argument("--network", action="store_true", help="顺便测试行情接口")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 1
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"出错: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
