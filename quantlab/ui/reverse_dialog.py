# -*- coding: utf-8 -*-
"""反向推算窗口:从历史走势倒推「哪些条件之后涨得多」。"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout)

from ..engine import reverse as reverse_mod
from ..engine.hub import TaskRunner
from ..utils import normalize_code
from .common import label, pnl_color, set_cell, table
from .theme import TEXT, TEXT_DIM, UP


class ReverseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("反向推算 · 从走势倒推策略")
        self.setMinimumSize(940, 640)
        self.setSizeGripEnabled(True)
        self._findings: list = []
        self._meta: dict = {}
        self._tasks: list[TaskRunner] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("股票"))
        self.code = QLineEdit("600519")
        self.code.setFixedWidth(100)
        top.addWidget(self.code)
        top.addWidget(QLabel("回看天数"))
        self.days = QSpinBox()
        self.days.setRange(200, 1200)
        self.days.setValue(400)
        self.days.setSingleStep(50)
        top.addWidget(self.days)
        top.addWidget(QLabel("看未来"))
        self.horizon = QSpinBox()
        self.horizon.setRange(1, 20)
        self.horizon.setValue(5)
        self.horizon.setSuffix(" 日")
        top.addWidget(self.horizon)
        top.addWidget(QLabel("条数"))
        self.top_n = QSpinBox()
        self.top_n.setRange(3, 30)
        self.top_n.setValue(10)
        top.addWidget(self.top_n)
        self.btn_run = QPushButton("开始推算")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run)
        top.addWidget(self.btn_run)
        top.addStretch(1)
        self.btn_save = QPushButton("存为策略")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_selected)
        top.addWidget(self.btn_save)
        root.addLayout(top)

        self.info = label("填代码后点「开始推算」:统计每个条件之后 N 日的实际涨跌", dim=True)
        root.addWidget(self.info)

        self.tbl = table(["条件", "样本", "胜率", "平均涨幅", "提升度", "基准"],
                         widths=[300, 60, 66, 84, 70, 70], stretch_col=0)
        self.tbl.itemSelectionChanged.connect(self._on_select)
        root.addWidget(self.tbl, 2)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(110)
        root.addWidget(self.detail)

        bottom = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("保存用的策略名(默认:反推·条件名)")
        bottom.addWidget(self.name_edit, 1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bottom.addWidget(b_close)
        root.addLayout(bottom)
        root.addWidget(label("提升度 = 条件胜率 ÷ 全样本胜率;>1.3 才算明显。"
                             "存成策略后买点就是该条件,卖点用回撤 5% 兜底", dim=True))

    def run(self) -> None:
        code = normalize_code(self.code.text())
        if not code:
            QMessageBox.warning(self, "缺代码", "填一个股票代码")
            return
        self.btn_run.setEnabled(False)
        self.info.setText(f"推算 {code} 中…")

        def job():
            return reverse_mod.reverse_model(code, days=self.days.value(),
                                             horizon=self.horizon.value(),
                                             top=self.top_n.value())

        def ok(res):
            fs, _df, meta = res
            self._findings, self._meta = fs, meta
            self._fill(fs, meta)
            self.btn_run.setEnabled(True)
            self.btn_save.setEnabled(bool(fs))

        def err(e):
            self.info.setText(f"推算失败:{e}")
            self.btn_run.setEnabled(True)

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_run.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _fill(self, fs: list, meta: dict) -> None:
        self.tbl.setRowCount(len(fs))
        for i, f in enumerate(fs):
            set_cell(self.tbl, i, 0, f.name, align="left")
            set_cell(self.tbl, i, 1, str(f.samples))
            set_cell(self.tbl, i, 2, f"{f.win_rate*100:.0f}%",
                     pnl_color(f.win_rate - f.base_rate))
            set_cell(self.tbl, i, 3, f"{f.avg_ret*100:+.1f}%", pnl_color(f.avg_ret))
            set_cell(self.tbl, i, 4, f"{f.lift:.2f}", UP if f.lift >= 1.3 else TEXT,
                     bold=(f.lift >= 1.5))
            set_cell(self.tbl, i, 5, f"{f.base_rate*100:.0f}%", TEXT_DIM)
        self.info.setText(
            f"{meta['code']} · {meta['start']} ~ {meta['end']} {meta['bars']} 根 · "
            f"看未来 {meta['horizon']} 日 · 全样本上涨率 {meta['base_rate']*100:.0f}% "
            f"平均 {meta['base_avg']*100:+.1f}%")
        if fs:
            self.tbl.selectRow(0)
            self._on_select()

    def _selected(self):
        rows = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        if not rows:
            return None
        i = rows[0].row()
        return self._findings[i] if 0 <= i < len(self._findings) else None

    def _on_select(self) -> None:
        f = self._selected()
        if not f:
            return
        self.detail.setPlainText(
            f"{f.name}\n条件:{f.cond}\n"
            f"命中 {f.samples} 次:上涨 {f.up_after} 次 / 下跌 {f.down_after} 次\n"
            f"胜率 {f.win_rate*100:.1f}%(基准 {f.base_rate*100:.1f}%)· "
            f"平均 {f.avg_ret*100:+.2f}%(基准 {f.avg_base*100:+.2f}%)· 提升度 {f.lift:.2f}")
        if not self.name_edit.text():
            self.name_edit.setText(f"反推·{f.name}")

    def save_selected(self) -> None:
        f = self._selected()
        if not f:
            return
        rule = reverse_mod.suggest_rule(f)
        if self.name_edit.text().strip():
            rule["name"] = self.name_edit.text().strip()
        try:
            from ..engine import rules as rules_mod
            rules_mod.save_rule(rule)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
