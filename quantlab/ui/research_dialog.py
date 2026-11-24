# -*- coding: utf-8 -*-
"""策略研究窗口:拿一只股票的历史走势,自动枚举形态模板找参数。

研究出来的是可读的条件表达式,能直接存成策略接自动交易/回测。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from ..engine import research as research_mod
from ..engine.hub import TaskRunner
from ..utils import normalize_code
from .common import label, pnl_color, set_cell, table
from .theme import BORDER, DOWN, TEXT, TEXT_DIM, UP


class ResearchDialog(QDialog):
    """自主研究:选一只票,跑一遍模板×参数网格。"""

    def __init__(self, gateway=None, parent=None):
        super().__init__(parent)
        self.gw = gateway
        self.setWindowTitle("自主研究 · 按历史走势找策略")
        self.setMinimumSize(940, 640)
        self.setSizeGripEnabled(True)
        self._cands: list = []
        self._df = None
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
        top.addWidget(QLabel("候选数"))
        self.top_n = QSpinBox()
        self.top_n.setRange(3, 30)
        self.top_n.setValue(8)
        top.addWidget(self.top_n)
        self.btn_run = QPushButton("开始研究")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run)
        top.addWidget(self.btn_run)
        top.addStretch(1)
        self.btn_save = QPushButton("存为策略")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_selected)
        top.addWidget(self.btn_save)
        root.addLayout(top)

        self.info = label("填代码后点「开始研究」。会跑 60+ 组形态×参数,约 1~3 秒", dim=True)
        root.addWidget(self.info)

        self.tbl = table(["策略", "训练收益", "夏普", "回撤", "笔数", "胜率", "验证收益", "评分"],
                         widths=[210, 84, 66, 70, 54, 60, 84, 66], stretch_col=0)
        self.tbl.itemSelectionChanged.connect(self._on_select)
        self.tbl.doubleClicked.connect(lambda *_: self._preview())
        root.addWidget(self.tbl, 2)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(120)
        self.detail.setPlaceholderText("选中一行查看买卖条件")
        root.addWidget(self.detail)

        bottom = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("保存用的策略名(默认:代码·模板名)")
        bottom.addWidget(self.name_edit, 1)
        bottom.addStretch(1)
        self.close_btn = QPushButton("关闭")
        self.close_btn.clicked.connect(self.accept)
        bottom.addWidget(self.close_btn)
        root.addLayout(bottom)

        hint = label("训练段 70% 用来挑、验证段 30% 用来淘汰;验证亏损的直接沉底,不参与排名", dim=True)
        root.addWidget(hint)

    # ---------------------------------------------------------------- 执行
    def run(self) -> None:
        code = normalize_code(self.code.text())
        if not code:
            QMessageBox.warning(self, "缺代码", "填一个股票代码")
            return
        self.btn_run.setEnabled(False)
        self.info.setText(f"研究 {code} 中…")

        def job():
            return research_mod.research(code, days=self.days.value(),
                                         top=self.top_n.value())

        def ok(res):
            cands, df, meta = res
            self._cands, self._df, self._meta = cands, df, meta
            self._fill(cands, meta)
            self.btn_run.setEnabled(True)
            self.btn_save.setEnabled(bool(cands))

        def err(e):
            self.info.setText(f"研究失败:{e}")
            self.btn_run.setEnabled(True)

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_run.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _fill(self, cands: list, meta: dict) -> None:
        self.tbl.setRowCount(len(cands))
        for i, c in enumerate(cands):
            set_cell(self.tbl, i, 0, c.name, align="left")
            set_cell(self.tbl, i, 1, f"{c.ret*100:+.1f}%", pnl_color(c.ret))
            set_cell(self.tbl, i, 2, f"{c.sharpe:.2f}", pnl_color(c.sharpe))
            set_cell(self.tbl, i, 3, f"{c.max_dd*100:.1f}%", DOWN if c.max_dd < -0.25 else TEXT)
            set_cell(self.tbl, i, 4, str(c.trades))
            set_cell(self.tbl, i, 5, f"{c.win_rate*100:.0f}%")
            set_cell(self.tbl, i, 6, f"{c.v_ret*100:+.1f}%", pnl_color(c.v_ret))
            set_cell(self.tbl, i, 7, f"{c.score:.2f}", bold=(i == 0))
        self.info.setText(
            f"{meta['code']} · {meta['start']} ~ {meta['end']} 共 {meta['bars']} 根 · "
            f"切分点 {meta['split']} · 试了 {meta['tested']} 组 · 取前 {len(cands)}")
        if cands:
            self.tbl.selectRow(0)
            self._on_select()

    def _selected(self):
        rows = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        if not rows:
            return None
        i = rows[0].row()
        return self._cands[i] if 0 <= i < len(self._cands) else None

    def _on_select(self) -> None:
        c = self._selected()
        if not c:
            return
        self.detail.setPlainText(
            f"{c.name}\n买入:{c.buy}\n卖出:{c.sell or '(不设卖点,只靠买点消失)'}\n"
            f"训练:收益 {c.ret*100:+.1f}% 夏普 {c.sharpe:.2f} 回撤 {c.max_dd*100:.1f}% "
            f"{c.trades} 笔 胜率 {c.win_rate*100:.0f}%\n"
            f"验证:收益 {c.v_ret*100:+.1f}% 夏普 {c.v_sharpe:.2f}")
        if not self.name_edit.text():
            self.name_edit.setText(f"{self._meta.get('code', '')}·{c.name}")

    def _preview(self) -> None:
        self._on_select()

    def save_selected(self) -> None:
        c = self._selected()
        if not c:
            self.status_hint("先选中一条")
            return
        name = self.name_edit.text().strip() or f"{self._meta.get('code','')}·{c.name}"
        try:
            research_mod.save_candidate(self._meta.get("code", ""), c, name=name)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()

    def status_hint(self, text: str) -> None:
        self.info.setText(text)

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
