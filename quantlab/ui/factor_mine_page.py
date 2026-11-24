# -*- coding: utf-8 -*-
"""策略因子挖掘页:A股 / 加密两个板块,自动挖掘 + 组合回测 + 因子取用。

每天首次启动时后台自动跑一轮(见 main_window),结果落盘到
USER_DIR/factor_mine/{a,crypto}.json;打开本页直接显示,不用等。
"""
from __future__ import annotations

import json
import threading
import time

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QHBoxLayout, QPushButton, QTabWidget, QVBoxLayout,
                               QWidget)

from ..config import USER_DIR
from ..engine import factor_mine
from ..engine.hub import TaskRunner
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import ACCENT, DOWN, TEXT_DIM, TEXT_MUT


class _Curve(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=False, y=True, alpha=0.12)
        self.plot.getAxis("left").setTextPen(TEXT_DIM)
        self.plot.getAxis("bottom").setTextPen(TEXT_DIM)
        self.plot.hideButtons()
        self.plot.setLabel("left", "净值", color=TEXT_MUT)
        lay.addWidget(self.plot)

    def show_curve(self, curve: list):
        self.plot.clear()
        if not curve:
            return
        ys = [float(v) for _d, v in curve]
        self.plot.plot(range(len(ys)), ys, pen=pg.mkPen(ACCENT, width=1.8),
                       fillLevel=1.0, brush=pg.mkBrush(76, 141, 255, 40))
        self.plot.addLine(y=1.0, pen=pg.mkPen("#3d4856", width=1))
        self.plot.setXRange(0, max(1, len(ys) - 1), padding=0)


class _LibraryDialog(QDialog):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("因子库")
        self.resize(760, 440)
        lay = QVBoxLayout(self)
        self.tbl = table(["表达式", "备注", "IC", "ICIR", "加入时间"],
                         widths=[320, 160, 60, 60, 110], stretch_col=0)
        lay.addWidget(self.tbl, 1)
        bar = QHBoxLayout()
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        b_copy = QPushButton("复制表达式")
        b_copy.clicked.connect(self._copy)
        b_del = QPushButton("删除")
        b_del.clicked.connect(self._del)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bar.addWidget(b_copy)
        bar.addWidget(b_del)
        bar.addWidget(b_close)
        lay.addLayout(bar)
        self._rows = []
        self.reload()

    def reload(self):
        self._rows = factor_mine.load_library()
        self.tbl.setRowCount(0)
        for i, r in enumerate(self._rows):
            self.tbl.insertRow(i)
            set_cell(self.tbl, i, 0, r.get("expr") or "", align="left")
            set_cell(self.tbl, i, 1, r.get("label") or "", align="left")
            st = r.get("stats") or {}
            set_cell(self.tbl, i, 2, f'{st.get("ic", 0):+.3f}')
            set_cell(self.tbl, i, 3, f'{st.get("icir", 0):+.2f}')
            set_cell(self.tbl, i, 4, r.get("created") or "", align="left")
        self.hint.setText(f"共 {len(self._rows)} 条")

    def _picked(self):
        row = self.tbl.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _copy(self):
        r = self._picked()
        if r:
            QGuiApplication.clipboard().setText(r.get("expr") or "")

    def _del(self):
        r = self._picked()
        if r and factor_mine.remove_factor(r.get("expr") or ""):
            self.reload()
            self.changed.emit()


class _MarketPanel(QWidget):
    """一个市场的完整面板:候选表 + 组合回测 + 取用按钮。"""

    status = Signal(str)
    round_signal = Signal(dict)

    def __init__(self, market: str, title: str, parent=None):
        super().__init__(parent)
        self.market = market
        self.title = title
        self._tasks: list[TaskRunner] = []
        self._rows: list[dict] = []
        self._res: dict = {}
        self._stop_event = threading.Event()
        self.round_signal.connect(self._round_update)
        self._build()
        self.load_saved()

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(label("股票池", dim=True))
        self.u = QComboBox()
        for n in (200, 300, 600):
            self.u.addItem(f"{n} 只", n)
        self.u.setCurrentIndex(1)
        self.u.setEnabled(self.market == "a")
        head.addWidget(self.u)
        head.addWidget(label("历史", dim=True))
        self.days = QComboBox()
        for n in (120, 250, 400):
            self.days.addItem(f"{n} 日", n)
        self.days.setCurrentIndex(1)
        head.addWidget(self.days)
        head.addWidget(label("预测期", dim=True))
        self.h = QComboBox()
        for n in (1, 3, 5, 10, 20):
            self.h.addItem(f"{n} 日", n)
        self.h.setCurrentIndex(2)
        head.addWidget(self.h)
        head.addWidget(label("模式", dim=True))
        self.mode = QComboBox()
        self.mode.addItem("模板迭代", "tpl")
        self.mode.addItem("遗传(GP)", "gp")
        head.addWidget(self.mode)
        head.addWidget(label("轮数", dim=True))
        self.rounds = QComboBox()
        for n in (3, 5, 8, 12, 20):
            self.rounds.addItem(f"{n} 轮", n)
        self.rounds.setCurrentIndex(2)
        head.addWidget(self.rounds)
        self.btn = QPushButton("持续挖掘")
        self.btn.clicked.connect(self.run)
        head.addWidget(self.btn)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_mining)
        head.addWidget(self.btn_stop)
        self.loop = QCheckBox("循环")
        self.loop.setToolTip("一轮跑完等 10 秒再挖,直到点停止")
        head.addWidget(self.loop)
        root.addLayout(head)
        self.hint = label("", dim=True)
        root.addWidget(self.hint)

        body = QHBoxLayout()
        body.setSpacing(8)

        left = Card("候选因子", hint="按 |ICIR| 排序 · 多选后回测组合")
        self.tbl = table(["#", "表达式", "IC", "ICIR", "覆盖", "换手", "多空"],
                         widths=[30, 220, 54, 54, 50, 50, 58], stretch_col=1)
        self.tbl.setSelectionMode(self.tbl.SelectionMode.ExtendedSelection)
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        left.add(self.tbl, 1)
        lb = QHBoxLayout()
        self.count = label("", dim=True)
        lb.addWidget(self.count, 1)
        b_bt = QPushButton("回测选中组合")
        b_bt.setObjectName("Primary")
        b_bt.clicked.connect(self.backtest)
        b_save = QPushButton("保存因子")
        b_save.clicked.connect(self.save_selected)
        b_copy = QPushButton("复制组合表达式")
        b_copy.clicked.connect(self.copy_combo)
        b_json = QPushButton("导出 JSON")
        b_json.clicked.connect(self.export_json)
        b_lib = QPushButton("因子库")
        b_lib.clicked.connect(self.open_lib)
        for b in (b_bt, b_save, b_copy, b_json, b_lib):
            lb.addWidget(b)
        left.body().addLayout(lb)
        body.addWidget(left, 58)

        right = Card("组合回测", hint="日频 · 等权 Top5 · 每期调仓")
        stats = QHBoxLayout()
        stats.setSpacing(6)
        self.s_ret = StatCard("总收益", "-")
        self.s_ann = StatCard("年化", "-")
        self.s_sharpe = StatCard("夏普", "-")
        self.s_mdd = StatCard("最大回撤", "-")
        for w in (self.s_ret, self.s_ann, self.s_sharpe, self.s_mdd):
            stats.addWidget(w, 1)
        right.body().addLayout(stats)
        self.curve = _Curve()
        right.add(self.curve, 1)
        self.bt_hint = label("选 1~5 个因子,点「回测选中组合」", dim=True)
        right.body().addWidget(self.bt_hint)
        body.addWidget(right, 42)
        root.addLayout(body, 1)

    # ------------------------------------------------------------------ 数据
    def load_saved(self):
        doc = factor_mine.load_result(self.market)
        if not doc:
            self.hint.setText("今天还没自动挖掘,点「重新挖掘」")
            return
        self._fill(doc.get("result") or {})
        self.hint.setText(f"自动挖掘 {doc.get('date', '')} {doc.get('saved', '')}")

    def _fill(self, res: dict, keep_hint: bool = False):
        self._res = res
        self._rows = res.get("rows") or []
        self.tbl.setRowCount(0)
        for i, r in enumerate(self._rows):
            self.tbl.insertRow(i)
            set_cell(self.tbl, i, 0, str(i + 1), align="left")
            mark = "★ " if r.get("selected") else ""
            set_cell(self.tbl, i, 1, mark + (r.get("expr") or ""), align="left")
            set_cell(self.tbl, i, 2, f'{r.get("ic", 0):+.3f}',
                     pnl_color(r.get("ic")))
            set_cell(self.tbl, i, 3, f'{r.get("icir", 0):+.2f}',
                     pnl_color(r.get("icir")))
            set_cell(self.tbl, i, 4, f'{r.get("coverage", 0) * 100:.0f}%')
            set_cell(self.tbl, i, 5, f'{r.get("turnover", 0):.2f}')
            set_cell(self.tbl, i, 6, f'{r.get("spread", 0) * 100:+.2f}%',
                     pnl_color(r.get("spread")))
            item = self.tbl.item(i, 1)
            if item is not None:
                tip = (f'{r.get("label")} · {r.get("expr")}\n'
                       f'训练 IC {r.get("ic")} / ICIR {r.get("icir")}\n'
                       f'测试 IC {r.get("test_ic", "-")} / '
                       f'ICIR {r.get("test_icir", "-")} · '
                       f'覆盖 {r.get("coverage")}')
                if r.get("depth"):
                    tip += f' · 树深 {r.get("depth")}'
                item.setToolTip(tip)
        sel = sum(1 for r in self._rows if r.get("selected"))
        self.count.setText(f"候选 {len(self._rows)} · 入选 {sel}")
        if self._rows:
            self.tbl.selectRow(0)

    def run(self):
        if self._tasks:
            self.hint.setText("还在跑,等这一轮")
            return
        days = int(self.days.currentData())
        stocks = int(self.u.currentData())
        h = int(self.h.currentData())
        rounds = int(self.rounds.currentData())
        mode = str(self.mode.currentData())
        self._stop_event.clear()
        self.btn.setEnabled(False)
        self.btn_stop.setEnabled(True)
        unit = "代" if mode == "gp" else "轮"
        self.hint.setText(f"持续挖掘:{self.title} × {days} 日 × {rounds} {unit} …")
        self.status.emit(f"因子挖掘:{self.title}")
        if mode == "gp":
            from ..engine import factor_gp as gp
            job = TaskRunner(lambda: gp.run(
                days=days, max_stocks=stocks, horizon=h, market=self.market,
                generations=rounds, population=40, time_budget=300,
                progress=lambda d: self.round_signal.emit(d),
                should_stop=self._stop_event.is_set))
        else:
            job = TaskRunner(lambda: factor_mine.iter_mine(
                days=days, max_stocks=stocks, horizon=h, market=self.market,
                rounds=rounds,
                progress=lambda d: self.round_signal.emit(d),
                should_stop=self._stop_event.is_set))
        job.succeeded.connect(self._ok)
        job.failed.connect(lambda m: self.hint.setText(f"失败:{m.splitlines()[0][:90]}"))
        job.finished.connect(lambda: self._done(job))
        self._tasks.append(job)
        job.start()

    def _done(self, job):
        if job in self._tasks:
            self._tasks.remove(job)
        self.btn.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _round_update(self, d: dict):
        rows = d.get("rows") or []
        sel = set(d.get("selected_exprs") or [])
        if rows:
            res = {"rows": [{**r, "selected": r.get("expr") in sel}
                            for r in rows]}
            self._fill(res, keep_hint=True)
        if "generation" in d:
            self.hint.setText(f"第 {d.get('generation')}/{d.get('total')} 代 · "
                              f"已评估 {d.get('evaluated')} 个表达式")
        else:
            self.hint.setText(f"第 {d.get('round')}/{d.get('total')} 轮 · "
                              f"候选 {d.get('pool')} · 新增 {d.get('new')}")

    def stop_mining(self):
        self._stop_event.set()
        self.loop.setChecked(False)
        self.hint.setText("正在停止…")

    def _ok(self, d: dict):
        d = d if isinstance(d, dict) else {}
        if not d.get("ok"):
            self.hint.setText(str(d.get("error") or "没有结果"))
            return
        factor_mine.save_result(self.market, d)
        self._fill(d)
        self.hint.setText(f"{d.get('n_codes')} 只 × {d.get('days')} 日 · "
                          f"{d.get('n_candidates')} 个候选 · "
                          f"{d.get('rounds')} 轮 · "
                          f"{d.get('cost_seconds')}s")
        if self.loop.isChecked() and not self._stop_event.is_set():
            QTimer.singleShot(10000, self.run)

    def _picked_rows(self) -> list[dict]:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()})
        out = [self._rows[i] for i in rows if 0 <= i < len(self._rows)]
        if not out:
            out = [r for r in self._rows if r.get("selected")][:3]
        return out[:6]

    def _on_pick(self):
        rows = self._picked_rows()
        if rows:
            self.bt_hint.setText("已选:" + " + ".join(
                (r.get("label") or r.get("expr") or "") for r in rows))

    # ------------------------------------------------------------------ 回测
    def backtest(self):
        rows = self._picked_rows()
        if not rows:
            self.bt_hint.setText("先选因子")
            return
        if self._tasks:
            return
        days = int(self.days.currentData())
        h = int(self.h.currentData())
        self.bt_hint.setText("回测中…")
        job = TaskRunner(lambda: self._run_backtest(rows, days, h))
        job.succeeded.connect(self._bt_ok)
        job.failed.connect(lambda m: self.bt_hint.setText(f"回测失败:{m[:90]}"))
        job.finished.connect(lambda: self._done(job))
        self._tasks.append(job)
        job.start()

    def _run_backtest(self, rows, days, h):
        panel = factor_mine._panel(days, 300, self.market)
        return factor_mine.composite_backtest(panel, rows, horizon=h, top_n=5)

    def _bt_ok(self, d: dict):
        d = d if isinstance(d, dict) else {}
        if not d.get("ok"):
            self.bt_hint.setText(str(d.get("error") or "回测失败"))
            return
        m = d.get("metrics") or {}
        self.curve.show_curve(d.get("curve") or [])
        self.s_ret.set(f'{m.get("total_return_pct", 0):+.1f}%',
                       pnl_color(m.get("total_return_pct")))
        self.s_ann.set(f'{m.get("annual_return_pct", 0):+.1f}%',
                       pnl_color(m.get("annual_return_pct")))
        self.s_sharpe.set(f'{m.get("sharpe", 0):+.2f}',
                          pnl_color(m.get("sharpe")))
        self.s_mdd.set(f'{m.get("max_drawdown_pct", 0):.1f}%', DOWN)
        self.bt_hint.setText(
            f'{m.get("n_days")} 天 · 换手 {m.get("turnover_avg")} · '
            f'成本 {m.get("cost")} · 最新持有:{"、".join((d.get("holdings") or [])[:5])}')

    # ------------------------------------------------------------------ 取用
    def _combo_expr(self, rows: list[dict]) -> str:
        parts = []
        for r in rows:
            expr = str(r.get("expr") or "")
            if not expr:
                continue
            term = f"rank({expr})"
            sign = float(r.get("ic") or 1) >= 0
            parts.append(("+" if sign else "-") + term)
        if not parts:
            return ""
        text = "".join(parts)
        return text[1:] if text[0] == "+" else text

    def copy_combo(self):
        expr = self._combo_expr(self._picked_rows())
        if expr:
            QGuiApplication.clipboard().setText(expr)
            self.bt_hint.setText("组合表达式已复制(可粘到选股/自动交易)")

    def save_selected(self):
        rows = self._picked_rows()
        n = 0
        for r in rows:
            if factor_mine.save_factor(r.get("expr") or "", r.get("label") or "", r):
                n += 1
        self.bt_hint.setText(f"已保存 {n} 个到因子库")

    def export_json(self):
        rows = self._picked_rows()
        if not rows:
            return
        name = f"factor_combo_{self.market}_{time.strftime('%Y%m%d')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出因子组合", str(USER_DIR / name), "JSON (*.json)")
        if not path:
            return
        doc = {"market": self.market, "horizon": int(self.h.currentData()),
               "created": time.strftime("%Y-%m-%d %H:%M"),
               "combo_expr": self._combo_expr(rows),
               "factors": [{k: r.get(k) for k in
                            ("expr", "label", "ic", "icir", "coverage",
                             "turnover", "spread")} for r in rows]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            self.bt_hint.setText(f"已导出:{path}")
        except OSError as e:
            self.bt_hint.setText(f"导出失败:{e}")

    def open_lib(self):
        _LibraryDialog(self).exec()

    def stop(self):
        for t in list(self._tasks):
            t.wait(6000)
        self._tasks.clear()


class FactorMinePage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("策略因子挖掘",
                   hint="每天首次启动自动跑 · 可回测、可复制、可导出")
        bar = QHBoxLayout()
        self.auto_lbl = label("自动挖掘:今天还没跑", dim=True)
        bar.addWidget(self.auto_lbl, 1)
        b_all = QPushButton("全部重跑")
        b_all.clicked.connect(self.run_all)
        bar.addWidget(b_all)
        b_lib = QPushButton("因子库")
        b_lib.clicked.connect(lambda: _LibraryDialog(self).exec())
        bar.addWidget(b_lib)
        top.body().addLayout(bar)
        root.addWidget(top)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.panels = {"a": _MarketPanel("a", "A股", self)}
        self.panels["a"].status.connect(self.status)
        self.tabs.addTab(self.panels["a"], "A股")
        root.addWidget(self.tabs, 1)
        self.refresh_auto()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh_auto()

    def refresh_auto(self):
        doc = factor_mine.load_result("a")
        if not doc:
            self.auto_lbl.setText("自动挖掘 · A股: 未跑")
            return
        res = doc.get("result") or {}
        sel = len(res.get("selected") or [])
        self.auto_lbl.setText(f"自动挖掘 · A股 {doc.get('saved', '')} · "
                              f"{len(res.get('rows') or [])} 候选 / {sel} 入选")
        self.panels["a"].load_saved()

    def run_all(self):
        """依次重跑两个市场(一个在跑时跳过另一个)。"""
        panel = self.panels["a"]
        if not panel._tasks:
            panel.run()

    def stop(self):
        for p in self.panels.values():
            p.stop()
