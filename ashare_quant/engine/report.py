"""回测报告：Markdown + SVG 净值曲线，不依赖绘图库。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .backtest import BacktestResult


def _fmt_pct(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value * 100:.{digits}f}%"


def _fmt_num(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.{digits}f}"


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    if high <= low:
        return [low]
    step = (high - low) / count
    return [low + step * i for i in range(count + 1)]


def svg_equity(result: BacktestResult, width: int = 820, height: int = 300) -> str:
    equity = result.equity.dropna()
    if equity.empty:
        return "<svg></svg>"
    margin_left, margin_right, margin_top, margin_bottom = 64, 16, 16, 34
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom

    bench = result.benchmark
    if bench is not None:
        bench = bench.reindex(equity.index).ffill()
    values = [equity]
    if bench is not None and not bench.dropna().empty:
        values.append(bench)
    low = min(float(series.min()) for series in values)
    high = max(float(series.max()) for series in values)
    pad = (high - low) * 0.05 or max(abs(high), 1.0) * 0.01
    low, high = low - pad, high + pad

    def point(index: int, value: float) -> tuple[float, float]:
        x = margin_left + plot_w * index / max(len(equity) - 1, 1)
        y = margin_top + plot_h * (1 - (value - low) / (high - low))
        return x, y

    def path(series: pd.Series) -> str:
        points = [point(i, float(v)) for i, v in enumerate(series.to_numpy())]
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    strategy_path = path(equity)
    area_path = (
        f"{margin_left},{margin_top + plot_h} "
        + strategy_path
        + f" {margin_left + plot_w},{margin_top + plot_h}"
    )

    grid = []
    for tick in _nice_ticks(low, high):
        _, y = point(0, tick)
        grid.append(
            f'<line x1="{margin_left}" y1="{y:.1f}" x2="{margin_left + plot_w}" '
            f'y2="{y:.1f}" stroke="#2a2f3a" stroke-width="1"/>'
            f'<text x="{margin_left - 10}" y="{y + 4:.1f}" fill="#8b949e" '
            f'font-size="11" text-anchor="end">{tick / 10000:.0f}万</text>'
        )

    labels = []
    for i in (0, len(equity) // 2, len(equity) - 1):
        x, _ = point(i, float(equity.iloc[i]))
        labels.append(
            f'<text x="{x:.1f}" y="{height - 12}" fill="#8b949e" '
            f'font-size="11" text-anchor="middle">{equity.index[i].strftime("%Y-%m")}</text>'
        )

    bench_line = ""
    if bench is not None and not bench.dropna().empty:
        bench_line = (
            f'<polyline points="{path(bench)}" fill="none" stroke="#f0a35e" '
            'stroke-width="1.4" stroke-dasharray="5 4" opacity="0.9"/>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" fill="#0d1117"/>
  {''.join(grid)}
  <polygon points="{area_path}" fill="#3fb950" opacity="0.10"/>
  <polyline points="{strategy_path}" fill="none" stroke="#3fb950" stroke-width="2"/>
  {bench_line}
  {''.join(labels)}
  <text x="{margin_left}" y="12" fill="#8b949e" font-size="11">策略净值</text>
  <text x="{margin_left + 70}" y="12" fill="#f0a35e" font-size="11">基准</text>
</svg>"""


def markdown_report(result: BacktestResult, meta: dict) -> str:
    stats = result.metrics
    start = result.equity.index[0].strftime("%Y-%m-%d")
    end = result.equity.index[-1].strftime("%Y-%m-%d")
    lines = [
        f"# 回测报告：{meta.get('title', '因子选股')}",
        "",
        f"- 区间：{start} ~ {end}",
        f"- 因子：{meta.get('factor_label', '-')}（{meta.get('factor', '-')}）",
        f"- 调仓：{meta.get('rebalance', '-')}，持仓 {meta.get('top_n', '-')} 只，等权",
        f"- 成本：佣金 {meta.get('fee_bps')}bp、印花税 {meta.get('stamp_bps')}bp、滑点 {meta.get('slippage_bps')}bp",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 策略 |",
        "| --- | ---: |",
        f"| 累计收益 | {_fmt_pct(stats.get('total_return'))} |",
        f"| 年化收益 | {_fmt_pct(stats.get('annual_return'))} |",
        f"| 年化波动 | {_fmt_pct(stats.get('annual_volatility'))} |",
        f"| 夏普 | {_fmt_num(stats.get('sharpe'))} |",
        f"| 最大回撤 | {_fmt_pct(stats.get('max_drawdown'))} |",
        f"| 卡玛 | {_fmt_num(stats.get('calmar'))} |",
        f"| 日胜率 | {_fmt_pct(stats.get('win_rate'))} |",
        f"| 年化换手 | {_fmt_num(stats.get('turnover_annual'), 1)} 倍 |",
    ]
    if "benchmark_return" in stats:
        lines += [
            f"| 基准收益 | {_fmt_pct(stats.get('benchmark_return'))} |",
            f"| 超额收益 | {_fmt_pct(stats.get('excess_return'))} |",
            f"| Beta | {_fmt_num(stats.get('beta'))} |",
            f"| 年化 Alpha | {_fmt_pct(stats.get('alpha_annual'))} |",
            f"| 信息比率 | {_fmt_num(stats.get('information_ratio'))} |",
        ]

    lines += ["", "## 分年收益", "", "| 年份 | 收益 |", "| --- | ---: |"]
    for year, value in result.yearly.items():
        lines.append(f"| {year} | {_fmt_pct(value)} |")

    lines += ["", "## 净值曲线", "", f"![净值曲线]({meta.get('chart', 'equity.svg')})", ""]
    if result.holdings:
        lines += ["## 期末持仓", "", "| 代码 | 股数 |", "| --- | ---: |"]
        for code, shares in sorted(result.holdings.items()):
            lines.append(f"| {code} | {int(shares)} |")
        lines.append("")

    lines += [
        "## 说明",
        "",
        "- 回测为历史模拟，不代表未来收益，也不构成任何投资建议。",
        "- 未考虑停牌长期、涨跌停连续、分红送转细节、冲击成本随规模变化等情形。",
        "- 结果对参数、样本区间和交易成本敏感，请自行做样本外与敏感性检验。",
        "",
    ]
    return "\n".join(lines)


def write_report(result: BacktestResult, meta: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_path = out_dir / "equity.svg"
    svg_path.write_text(svg_equity(result), encoding="utf-8")
    meta = dict(meta)
    meta.setdefault("chart", "equity.svg")
    md_path = out_dir / "report.md"
    md_path.write_text(markdown_report(result, meta), encoding="utf-8")
    return md_path, svg_path
