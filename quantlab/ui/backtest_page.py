# -*- coding: utf-8 -*-
"""回测页:策略 + 股票池 + 参数 → 净值曲线 / 成绩单 / 交易明细 / 因果检查。"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
from PySide6.QtCore import QDate, Signal
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDoubleSpinBox, QFileDialog,
                               QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from ..config import APP_DIR
from ..engine import audit, backtest as bt, strategy as st_mod, universe
from ..engine.hub import TaskRunner
from .charts import EquityWidget
from .board import Board
from .common import Card, label, pnl_color, set_cell, table
from .theme import TEXT_DIM, UP, WARN

POOLS = [("全市场", "all"), ("沪深300", "hs300"), ("中证500", "zz500"),
         ("中证1000", "zz1000"), ("上证50", "sz50"), ("成交额前列", "amount"),
         ("自选股", "watchlist")]


class BacktestPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._result = None
        self._metrics = None
        self._bench = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("backtest", cols=[1.0], rows=[0.24, 0.12, 0.64])
        root.addWidget(board)

        cfg_card = Card("回测设置")
        r1 = QHBoxLayout()
        self.stg = QComboBox()
        self._reload_strategies()
        self.pool = QComboBox()
        for name, key in POOLS:
            self.pool.addItem(name, key)
        self.n_stocks = QSpinBox()
        self.n_stocks.setRange(10, 300)
        self.n_stocks.setValue(30)
        self.start = QDateEdit(QDate(2023, 1, 1))
        self.start.setCalendarPopup(True)
        self.start.setDisplayFormat("yyyy-MM-dd")
        self.end = QDateEdit(QDate.currentDate())
        self.end.setCalendarPopup(True)
        self.end.setDisplayFormat("yyyy-MM-dd")
        self.cash = QDoubleSpinBox()
        self.cash.setRange(10000, 1e9)
        self.cash.setValue(1_000_000)
        self.cash.setSingleStep(100000)
        self.cash.setDecimals(0)
        self.max_pos = QSpinBox()
        self.max_pos.setRange(1, 50)
        self.max_pos.setValue(8)
        self.pos_pct = QDoubleSpinBox()
        self.pos_pct.setRange(1, 100)
        self.pos_pct.setValue(11)
        self.pos_pct.setSuffix("%")

        for lab, w in (("策略", self.stg), ("股票池", self.pool), ("只数", self.n_stocks),
                       ("起始", self.start), ("结束", self.end), ("资金", self.cash),
                       ("最大持仓", self.max_pos), ("单票仓位", self.pos_pct)):
            r1.addWidget(label(lab, dim=True))
            r1.addWidget(w)
            r1.addSpacing(6)
        cfg_card._lay.addLayout(r1)

        r2 = QHBoxLayout()
        self.btn_reload = QPushButton("刷新策略")
        self.btn_reload.setToolTip("新建的自定义策略会出现在下拉里")
        self.btn_reload.clicked.connect(self._reload_strategies)
        self.btn_run = QPushButton("运行回测")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run_backtest)
        self.btn_check = QPushButton("未来函数检查")
        self.btn_check.clicked.connect(self.run_causality)
        self.btn_export = QPushButton("导出报告")
        self.btn_export.clicked.connect(self.export_report)
        self.btn_export.setEnabled(False)
        self.btn_ic = QPushButton("算因子 IC")
        self.btn_ic.setToolTip("用当前股票池和区间,算每个因子的 IC/IR")
        self.btn_ic.clicked.connect(self.run_factor_ic)
        self.btn_evolve = QPushButton("搜索权重")
        self.btn_evolve.setToolTip("用 MAP-Elites 进化搜索多因子权重")
        self.btn_evolve.clicked.connect(self.run_evolve)
        self.btn_batch = QPushButton("批量对比")
        self.btn_batch.setToolTip("同一股票池/区间,跑全部策略(可选参数扫描),按夏普排序")
        self.btn_batch.clicked.connect(self.run_batch)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        r2.addWidget(self.btn_reload)
        r2.addWidget(self.btn_run)
        r2.addWidget(self.btn_batch)
        r2.addWidget(self.btn_check)
        r2.addWidget(self.btn_export)
        r2.addWidget(self.btn_ic)
        r2.addWidget(self.btn_evolve)
        r2.addSpacing(10)
        r2.addWidget(self.progress, 1)
        self.run_info = QLabel("")
        self.run_info.setStyleSheet(f"color:{TEXT_DIM};")
        r2.addWidget(self.run_info)
        cfg_card._lay.addLayout(r2)
        board.add(cfg_card, 0, 0, min_h=112)

        # 指标卡
        stats = Board("backtest.stats", cols=[0.125] * 8, rows=[1.0])
        self.cards = {}
        for i, (key, title) in enumerate(
                (("total", "总收益"), ("annual", "年化"), ("mdd", "最大回撤"),
                 ("sharpe", "夏普(盯市)"), ("alpha", "Alpha(年化)"),
                 ("beta", "Beta"), ("trades", "交易笔数"), ("win", "胜率"))):
            c = Card()
            c.setObjectName("CardFlat")
            v = QLabel("-")
            v.setObjectName("BigNum")
            t = QLabel(title)
            t.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
            # 卡片比两行字高:上下各留一段弹性,内容才会居中而不是贴顶
            c._lay.addStretch(1)
            c._lay.addWidget(v)
            c._lay.addWidget(t)
            c._lay.addStretch(1)
            self.cards[key] = v
            stats.add(c, 0, i, min_w=112, min_h=68)
        board.add(stats, 1, 0, min_h=74)

        body = Board("backtest.body", cols=[0.62, 0.38], rows=[1.0])
        left = Card("净值曲线")
        self.equity = EquityWidget()
        left.add(self.equity, 1)
        body.add(left, 0, 0, min_w=420, min_h=200)

        self.tabs = QTabWidget()
        self.trade_tbl = table(["代码", "名称", "开仓日", "平仓日", "开仓价", "平仓价",
                                "数量", "盈亏", "收益率", "原因"],
                               widths=[62, 76, 84, 84, 68, 68, 66, 82, 68, 90], stretch_col=1)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet("font-family:Consolas,'Microsoft YaHei';font-size:12px;")
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        l1.setContentsMargins(6, 6, 6, 6)
        l1.addWidget(self.trade_tbl)
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        l2.setContentsMargins(6, 6, 6, 6)
        l2.addWidget(self.report)
        self.tabs.addTab(w1, "交易明细")
        self.tabs.addTab(w2, "成绩单")

        self.cmp_tbl = table(["策略", "总收益%", "年化%", "最大回撤%", "夏普(盯市)",
                              "夏普(平仓)", "Alpha", "Beta", "交易", "胜率%", "DSR"],
                             widths=[150, 82, 74, 92, 96, 96, 70, 62, 60, 66, 68],
                             stretch_col=0)
        w3 = QWidget()
        l3 = QVBoxLayout(w3)
        l3.setContentsMargins(6, 6, 6, 6)
        self.cmp_hint = QLabel("点「批量对比」:同一区间与股票池,跑全部策略 + 参数扫描,按夏普排序。"
                               "试了多少组,DSR 就会相应打折。")
        self.cmp_hint.setWordWrap(True)
        self.cmp_hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        l3.addWidget(self.cmp_hint)
        l3.addWidget(self.cmp_tbl)
        self.tabs.addTab(w3, "对比")

        self.ic_tbl = table(["因子", "RankIC", "t", "扣费/期", "中性化/期", "判定"],
                            widths=[140, 74, 58, 84, 88, 132], stretch_col=0)
        w4 = QWidget()
        l4 = QVBoxLayout(w4)
        l4.setContentsMargins(6, 6, 6, 6)
        self.ic_hint = QLabel(
            "点「算因子 IC」:同时给出 Rank IC、扣费后单期收益、以及 **beta 中性化后**的收益。\n"
            "只有「扣费/期」和「中性化/期」两列都为正且 t≥2 才算真 alpha —— "
            "实测 45 因子等权 rank 在 2024-08~2026-11 原始 +0.570%/期(t=2.23),"
            "中性化后 -0.436%(t=-1.55),那点超额全是 beta。")
        self.ic_hint.setWordWrap(True)
        self.ic_hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        l4.addWidget(self.ic_hint)
        l4.addWidget(self.ic_tbl)
        self.tabs.addTab(w4, "因子评测")
        body.add(self.tabs, 0, 1, min_w=360, min_h=200)
        board.add(body, 2, 0, min_h=240)

    def _reload_strategies(self) -> None:
        cur = self.stg.currentData() if self.stg.count() else None
        self.stg.clear()
        from ..engine import rules as rules_mod
        for s2 in st_mod.list_strategies():
            self.stg.addItem(f"{s2['name']} · {s2['desc']}", s2["name"])
        try:
            for r in rules_mod.list_rules():
                self.stg.addItem(f"自有·{r['name']}", f"rule:{r['name']}")
        except Exception:
            pass
        if cur:
            i = self.stg.findData(cur)
            if i >= 0:
                self.stg.setCurrentIndex(i)

    # ------------------------------------------------------------ 运行
    def _run(self, fn, on_ok, name=""):
        if name:
            for t in list(self._tasks):
                if getattr(t, "task_name", name) == name and t.isRunning():
                    return
        t = TaskRunner(fn, name=name, parent=self)
        t.task_name = name
        t.succeeded.connect(on_ok)
        t.failed.connect(self._on_fail)
        t.progressed.connect(lambda i, n, _: self._set_progress(i, n))
        self._tasks.append(t)
        t.finished.connect(lambda: self._tasks.remove(t) if t in self._tasks else None)
        t.start()

    def _on_fail(self, msg: str) -> None:
        self.btn_run.setEnabled(True)
        self.status.emit(msg.splitlines()[0][:200])
        self.run_info.setText("失败:" + msg.splitlines()[0][:80])

    def _set_progress(self, i: int, n: int) -> None:
        if n > 0:
            self.progress.setValue(int(i / n * 100))

    def run_backtest(self) -> None:
        sname = self.stg.currentData()
        pool = self.pool.currentData()
        n = self.n_stocks.value()
        start = self.start.date().toString("yyyy-MM-dd")
        end = self.end.date().toString("yyyy-MM-dd")
        cash = float(self.cash.value())
        max_pos = self.max_pos.value()
        pct = self.pos_pct.value() / 100.0

        self.btn_run.setEnabled(False)
        self.run_info.setText("准备数据…")
        self.progress.setValue(2)
        t0 = time.time()

        def job(prog=None):
            rows, src = universe.build(pool, limit=n)
            if not rows:
                raise RuntimeError("股票池为空(数据源限流?),稍后重试")
            codes = [universe.normalize_code(r["code"]) for r in rows][:n]
            names = {universe.normalize_code(r["code"]): r.get("name", "") for r in rows}
            data = bt.load_universe_data(codes, start, end, count=1400,
                                        progress=lambda i, tot: None)
            if not data:
                raise RuntimeError("没有任何股票的日线可用")
            strategy = st_mod.build(sname)
            strategy.params["n_long"] = max(1, min(max_pos, len(data) // 2))
            engine = bt.Backtester(strategy, bt.BTParams(
                start=start, end=end, initial_cash=cash,
                max_positions=max_pos, per_position_pct=pct),
                data, names)
            res = engine.run()
            bench = bt.load_benchmark("000300", start, end)
            m = audit.summarize(res, bench, n_trials=1, capital=cash)
            return {"res": res, "metrics": m, "bench": bench, "src": src,
                    "codes": codes, "strategy": strategy, "data": data}

        def on_ok(payload):
            self._result = payload["res"]
            self._metrics = payload["metrics"]
            self._bench = payload["bench"]
            self._strategy = payload["strategy"]
            self._data = payload.get("data") or {}
            self._show(payload)
            try:
                from .. import feedback
                m = payload.get("metrics") or {}
                keep = ("total_return_pct", "annual_return_pct",
                        "max_drawdown_pct", "sharpe_mtm", "sharpe_realized",
                        "sortino", "calmar", "win_rate_pct", "profit_factor",
                        "exposure_pct", "days", "trades", "beta",
                        "alpha_annual", "information_ratio")
                metrics = {k: m.get(k) for k in keep
                           if isinstance(m.get(k), (int, float))}
                strat = payload.get("strategy")
                params = {k: v for k, v in (getattr(strat, "params", {}) or {}).items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)}
                feedback.push(
                    "backtest", "a",
                    {"metrics": metrics, "params": params,
                     "universe_n": len(payload.get("codes") or [])},
                    strategy=str(getattr(strat, "name", "") or "backtest"))
            except Exception:
                pass
            self.btn_run.setEnabled(True)
            self.btn_export.setEnabled(True)
            self.progress.setValue(100)
            self.run_info.setText(f"完成 · {len(payload['codes'])} 只 · "
                                  f"{time.time() - t0:.1f}s")

        self._run(job, on_ok, "backtest")

    def _show(self, p: dict) -> None:
        m = p["metrics"]
        self.cards["total"].setText(f"{m['total_return_pct']:+.2f}%")
        self.cards["total"].setStyleSheet(f"color:{pnl_color(m['total_return_pct'])};")
        self.cards["annual"].setText(f"{m['annual_return_pct']:+.2f}%")
        self.cards["annual"].setStyleSheet(f"color:{pnl_color(m['annual_return_pct'])};")
        self.cards["mdd"].setText(f"{m['max_drawdown_pct']:.2f}%")
        self.cards["mdd"].setStyleSheet("color:#ef4444;")
        self.cards["sharpe"].setText(f"{m['sharpe_mtm']:.2f}")
        self.cards["sharpe"].setStyleSheet(
            f"color:{pnl_color(m['sharpe_mtm'])};")
        self.cards["alpha"].setText(f"{m['alpha_annual']:.2f}" if m.get("alpha_annual") is not None else "-")
        self.cards["alpha"].setStyleSheet(f"color:{pnl_color(m.get('alpha_annual'))};")
        self.cards["beta"].setText(f"{m['beta']:.2f}" if m.get("beta") is not None else "-")
        self.cards["trades"].setText(str(m["trades"]))
        self.cards["win"].setText(f"{m['win_rate_pct']:.0f}%")

        self.equity.plot_equity(p["res"].equity, p["bench"])
        self._fill_trades(p["res"])
        self.report.setPlainText(audit.to_markdown(m, f"{p['strategy'].name} 回测成绩单"))

    def _fill_trades(self, res) -> None:
        td = res.trade_df()
        self.trade_tbl.setRowCount(len(td))
        for i, r in enumerate(td.to_dict("records")):
            set_cell(self.trade_tbl, i, 0, str(r["code"]), align="left")
            set_cell(self.trade_tbl, i, 1, str(r.get("name") or ""), align="left")
            set_cell(self.trade_tbl, i, 2, str(r["open_date"])[:10])
            set_cell(self.trade_tbl, i, 3, str(r["close_date"])[:10])
            set_cell(self.trade_tbl, i, 4, f"{r['open_price']:.2f}")
            set_cell(self.trade_tbl, i, 5, f"{r['close_price']:.2f}")
            set_cell(self.trade_tbl, i, 6, str(r["qty"]))
            set_cell(self.trade_tbl, i, 7, f"{r['profit']:,.0f}", pnl_color(r["profit"]), bold=True)
            set_cell(self.trade_tbl, i, 8, f"{r['profit_pct']:+.2f}%", pnl_color(r["profit"]))
            set_cell(self.trade_tbl, i, 9, str(r.get("reason") or ""), align="left")

    def run_batch(self) -> None:
        """同一数据上跑多组参数/多策略,横向比较。"""
        pool = self.pool.currentData()
        n = self.n_stocks.value()
        start = self.start.date().toString("yyyy-MM-dd")
        end = self.end.date().toString("yyyy-MM-dd")
        cash = float(self.cash.value())
        max_pos = self.max_pos.value()
        pct = self.pos_pct.value() / 100.0
        cur = self.stg.currentData()

        combos: list[tuple[str, dict]] = []
        for s in st_mod.list_strategies():
            combos.append((s["name"], {}))
        for k in (5, 8, 12, 15):
            combos.append((cur, {"n_long": k}))
        combos = list(dict.fromkeys([(a, tuple(sorted(b.items()))) for a, b in combos]))
        combos = [(a, dict(b)) for a, b in combos]

        self.btn_batch.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.run_info.setText(f"批量对比:{len(combos)} 组…")
        self.progress.setValue(0)
        t0 = time.time()
        self.status.emit(f"批量对比开始:{len(combos)} 组,同一份数据")

        def job():
            rows, src = universe.build(pool, limit=n)
            if not rows:
                raise RuntimeError("股票池为空(数据源限流?)")
            codes = [universe.normalize_code(r["code"]) for r in rows][:n]
            names = {universe.normalize_code(r["code"]): r.get("name", "") for r in rows}
            data = bt.load_universe_data(codes, start, end, count=1400)
            if not data:
                raise RuntimeError("没有可用日线")
            bench = bt.load_benchmark("000300", start, end)
            out = []
            trials = len(combos)
            for i, (sname, params) in enumerate(combos):
                try:
                    st = st_mod.build(sname, **params)
                    st.params["n_long"] = int(params.get("n_long",
                                                         max(1, min(max_pos, len(data) // 2))))
                    engine = bt.Backtester(st, bt.BTParams(
                        start=start, end=end, initial_cash=cash,
                        max_positions=max_pos, per_position_pct=pct), data, names)
                    res = engine.run()
                    m = audit.summarize(res, bench, n_trials=trials, capital=cash, do_mc=False)
                    label = sname if not params else f"{sname} n_long={params.get('n_long')}"
                    out.append({"label": label, **m})
                except Exception as e:  # noqa: BLE001
                    out.append({"label": f"{sname}{params or ''}", "error": str(e)[:60]})
            return {"rows": out, "codes": codes, "src": src, "n": len(data)}

        def on_ok(payload):
            rows = payload["rows"]
            ok_rows = [r for r in rows if not r.get("error")]
            ok_rows.sort(key=lambda r: (r.get("sharpe_mtm") or -9), reverse=True)
            self.cmp_tbl.setRowCount(len(ok_rows))
            for i, r in enumerate(ok_rows):
                set_cell(self.cmp_tbl, i, 0, r["label"], align="left")
                set_cell(self.cmp_tbl, i, 1, f"{r.get('total_return_pct', 0):+.2f}",
                         pnl_color(r.get("total_return_pct")))
                set_cell(self.cmp_tbl, i, 2, f"{r.get('annual_return_pct', 0):+.2f}",
                         pnl_color(r.get("annual_return_pct")))
                set_cell(self.cmp_tbl, i, 3, f"{r.get('max_drawdown_pct', 0):.2f}")
                set_cell(self.cmp_tbl, i, 4, f"{r.get('sharpe_mtm', 0):.2f}",
                         pnl_color(r.get("sharpe_mtm")), bold=(i == 0))
                set_cell(self.cmp_tbl, i, 5, f"{r.get('sharpe_realized', 0):.2f}")
                set_cell(self.cmp_tbl, i, 6,
                         f"{r.get('alpha_annual'):.3f}" if r.get("alpha_annual") is not None else "-")
                set_cell(self.cmp_tbl, i, 7,
                         f"{r.get('beta'):.2f}" if r.get("beta") is not None else "-")
                set_cell(self.cmp_tbl, i, 8, str(r.get("trades")))
                set_cell(self.cmp_tbl, i, 9, f"{r.get('win_rate_pct', 0):.0f}")
                dsr = (r.get("_dsr") or {}).get("DSR")
                set_cell(self.cmp_tbl, i, 10, f"{dsr:.3f}" if dsr is not None else "-",
                         None if dsr is None else ("#22c55e" if dsr >= 0.95 else "#f59e0b"))
            self.tabs.setCurrentIndex(2)
            best = ok_rows[0] if ok_rows else None
            tip = (f"共 {len(rows)} 组,有效 {len(ok_rows)} 组 · {payload['n']} 只 · "
                   f"{payload['src']} · {time.time() - t0:.1f}s")
            if best:
                tip += f" · 最优 {best['label']} 夏普 {best.get('sharpe_mtm'):.2f}"
            dsr_best = ((best or {}).get("_dsr") or {}).get("DSR")
            if dsr_best is not None:
                tip += f",DSR {dsr_best:.3f}" + ("(显著)" if dsr_best >= 0.95
                                                else "(试错折扣后不显著)")
            self.cmp_hint.setText(tip)
            self.run_info.setText("批量对比完成")
            self.btn_batch.setEnabled(True)
            self.btn_run.setEnabled(True)
            self.progress.setValue(100)
            self.status.emit(tip[:200])

        self._run(job, on_ok, "batch")

    def run_causality(self) -> None:
        if not getattr(self, "_strategy", None) or not self._result:
            self.status.emit("先跑一次回测")
            return
        strategy = self._strategy
        codes = getattr(self, "_codes", None)
        data = getattr(self, "_data", None)
        if not data:
            self.status.emit("因果检查需要回测数据,请重新运行回测")
            return

        def job():
            return audit.check_causality(strategy, {c: data[c] for c in list(data)[:5]}, points=6)

        def on_ok(cc: dict):
            if cc.get("verdict") == "no_leak":
                txt = (f"通过:比对 {cc['compared']} 个时点,最大差异 {cc['max_diff']:.2e}\n"
                       f"同一时点用全量数据与截断数据算因子,结果一致 → 没有未来函数。")
            elif cc.get("verdict") == "inconclusive":
                txt = (f"无法定论:只比对到 {cc['compared']} 个有效点(需要 ≥10)。\n"
                       f"通常是数据太短或因子 NaN 太多。")
            else:
                txt = f"未通过:{cc.get('leak')}\n说明策略用到了未来数据。"
            self.report.appendPlainText("\n\n## 未来函数检查\n" + txt)
            self.status.emit(txt.splitlines()[0])

        self._run(job, on_ok, "causality")

    def run_factor_ic(self) -> None:
        pool = self.pool.currentData()
        n = self.n_stocks.value()
        start = self.start.date().toString("yyyy-MM-dd")
        end = self.end.date().toString("yyyy-MM-dd")
        self.btn_ic.setEnabled(False)
        self.ic_hint.setText("算因子 IC 中…")

        def job():
            from ..engine import factor_analysis as fa
            codes = universe.build(pool, n)
            data = bt.load_universe_data(codes, start, end, count=400)
            keys = ["momentum20", "reversal5", "lowvol20", "volprice5", "matrend",
                    "slope20", "cmf20", "ksft2", "beta20", "resi20"]
            rep = fa.report(data, keys, horizon=5)
            # 每个因子单独做「扣费收益 + beta 中性化」双栏评测
            raw, neut = {}, {}
            for k in keys:
                if k not in data and not any(k == kk for kk in keys):
                    continue
                sc = {}
                for code, df in data.items():
                    try:
                        v = fa.fct.factor_series(df, k)
                    except Exception:  # noqa: BLE001
                        continue
                    idx = df["datetime"] if "datetime" in df else df.index
                    sc[code] = pd.Series(np.asarray(v, dtype="float64"),
                                         index=pd.DatetimeIndex(idx))
                if not sc:
                    continue
                mat = pd.DataFrame(sc).sort_index().rank(axis=1, pct=True)
                r1 = fa.net_return(data, mat, horizon=5, step=5)
                # 用同一打分做中性化:beta 只对组合层有意义,单因子这里用残差版
                r2 = fa.net_return(data, fa.beta_neutralize(mat, data), horizon=5, step=5)
                raw[k] = r1 if r1 else {}
                neut[k] = r2 if r2 else {}
            return {"rep": rep, "n": len(data), "raw": raw, "neut": neut}

        def on_ok(d):
            self.btn_ic.setEnabled(True)
            rows = d["rep"]["factors"]
            self.ic_tbl.setRowCount(len(rows))
            for i, r in enumerate(rows):
                k = r["key"]
                ic = float(r.get("ic") or 0.0)
                set_cell(self.ic_tbl, i, 0, f"{r['name']}({k})", align="left")
                set_cell(self.ic_tbl, i, 1, f"{ic:+.3f}", pnl_color(ic))
                set_cell(self.ic_tbl, i, 2, f"{r['t']:+.2f}",
                         None if abs(r["t"]) >= 2 else TEXT_DIM)
                r1 = d["raw"].get(k) or {}
                r2 = d["neut"].get(k) or {}
                e1, e2 = r1.get("excess_mean"), r2.get("excess_mean")
                t1, t2 = r1.get("excess_t"), r2.get("excess_t")
                if e1 is None:
                    set_cell(self.ic_tbl, i, 3, "—", TEXT_DIM)
                else:
                    set_cell(self.ic_tbl, i, 3, f"{e1*100:+.3f}%",
                             pnl_color(e1) if (t1 or 0) >= 2 else TEXT_DIM)
                if e2 is None:
                    set_cell(self.ic_tbl, i, 4, "—", TEXT_DIM)
                else:
                    set_cell(self.ic_tbl, i, 4, f"{e2*100:+.3f}%",
                             pnl_color(e2) if (t2 or 0) >= 2 else TEXT_DIM)
                # 判定:中性化后仍显著才算真 alpha
                if e1 is not None and (t1 or 0) >= 2 and e2 is not None and (t2 or 0) >= 2:
                    verdict, vc = "真 alpha", UP
                elif e1 is not None and (t1 or 0) >= 2:
                    verdict, vc = "beta 伪装", WARN
                else:
                    verdict, vc = "跑不赢等权", TEXT_DIM
                set_cell(self.ic_tbl, i, 5, verdict, vc)
            from ..engine import factor_analysis as fa
            red = d["rep"].get("redundant") or []
            tip = (f"{len(rows)} 个因子 · 池子 {d['n']} 只 · 未来 5 日 · 扣费 "
                   f"{fa.COST_ROUND_TRIP*100:.2f}%/次")
            if red:
                worst = red[0]
                tip += f" · 冗余:{worst['a']}≈{worst['b']} {worst['corr']:+.2f}"
            self.ic_hint.setText(tip)

        def on_err(e):
            self.btn_ic.setEnabled(True)
            self.ic_hint.setText(f"算因子 IC 失败:{e}")

        t = TaskRunner(job, name="factor_ic", parent=self)
        t.task_name = "factor_ic"
        t.succeeded.connect(on_ok)
        t.failed.connect(on_err)
        t.finished.connect(lambda: self.btn_ic.setEnabled(True))
        self._tasks = [x for x in self._tasks if x.isRunning()]
        self._tasks.append(t)
        t.start()

    def run_evolve(self) -> None:
        pool = self.pool.currentData()
        n = self.n_stocks.value()
        start = self.start.date().toString("yyyy-MM-dd")
        end = self.end.date().toString("yyyy-MM-dd")
        keys = ["momentum20", "reversal5", "lowvol20", "volprice5", "matrend"]
        self.btn_evolve.setEnabled(False)
        self.run_info.setText("搜索因子权重中…")

        def job():
            from ..engine import evolve_search as es
            codes = universe.build(pool, n)
            data = bt.load_universe_data(codes, start, end, count=400)
            return es.search(data, keys, n_long=self.max_pos.value(),
                             generations=20, pop=16)

        def on_ok(res):
            self.btn_evolve.setEnabled(True)
            w = res.get("weights") or []
            ks = res.get("keys") or []
            self.run_info.setText(
                f"权重搜索:夏普 {res.get('fitness', 0):.2f} · "
                + " / ".join(f"{k}:{x:.2f}" for k, x in zip(ks, w)))
            self.report.appendPlainText(
                "\n\n## 因子权重搜索(MAP-Elites)\n"
                + f"fitness(夏普): {res.get('fitness', 0):.2f}\n"
                + "\n".join(f"- {k}: {x:.3f}" for k, x in zip(ks, w)))

        def on_err(e):
            self.btn_evolve.setEnabled(True)
            self.run_info.setText(f"搜索失败:{e}")

        t = TaskRunner(job, name="evolve", parent=self)
        t.task_name = "evolve"
        t.succeeded.connect(on_ok)
        t.failed.connect(on_err)
        t.finished.connect(lambda: self.btn_evolve.setEnabled(True))
        self._tasks = [x for x in self._tasks if x.isRunning()]
        self._tasks.append(t)
        t.start()

    def export_report(self) -> None:
        if not self._metrics:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出报告",
            str(APP_DIR / f"backtest_{time.strftime('%Y%m%d_%H%M')}.md"), "Markdown (*.md)")
        if not path:
            return
        md = audit.to_markdown(self._metrics, "回测报告")
        Path(path).write_text(md, encoding="utf-8")
        self.status.emit(f"已导出 {path}")
