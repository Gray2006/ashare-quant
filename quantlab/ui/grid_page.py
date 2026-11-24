# -*- coding: utf-8 -*-
"""多图联动:四张 K 线并排,十字光标在四图之间同步。

同步的是**下标**(第几根),不是日期 —— 同一标的同一周期下两者等价;
周期不同时下标对不上,所以页面上明确写了「同周期才严格可比」。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ..data.kline import get_kline
from ..engine.hub import TaskRunner
from .charts import KLineWidget
from .common import Card, label

PERIODS = [("日线", 101), ("周线", 102), ("60 分钟", 60), ("30 分钟", 30)]
DEFAULT_CODES = ["600519", "000001", "300750", "601318"]


class GridPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._cells: list[dict] = []
        self._syncing = False
        self._build()
        QTimer.singleShot(800, self.refresh_all)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("周期", dim=True, size=11))
        self.period = QComboBox()
        for name, klt in PERIODS:
            self.period.addItem(name, klt)
        bar.addWidget(self.period)
        self.btn = QPushButton("重算全部")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh_all)
        bar.addWidget(self.btn)
        self.hint = label("四图十字线联动 · 同步的是第几根,同周期才严格可比",
                          dim=True, size=11)
        bar.addWidget(self.hint, 1)
        root.addLayout(bar)

        grid = QGridLayout()
        grid.setSpacing(8)
        for idx in range(4):
            card = Card(f"图 {idx + 1}")
            edit = QLineEdit(DEFAULT_CODES[idx])
            edit.setMaximumWidth(110)
            edit.setPlaceholderText("6 位代码")
            edit.returnPressed.connect(self.refresh_all)
            card.body().addWidget(edit)
            ch = KLineWidget()
            ch.cursor_moved.connect(self._on_cursor)
            card.add(ch, 1)
            grid.addWidget(card, idx // 2, idx % 2)
            self._cells.append({"card": card, "edit": edit, "chart": ch,
                                "code": DEFAULT_CODES[idx]})
        root.addLayout(grid, 1)

    def _on_cursor(self, i: int):
        """一张图动了光标,把其余三张也挪过去(来源自己不用再挪)。"""
        if self._syncing:
            return
        src = self.sender()
        self._syncing = True
        try:
            for c in self._cells:
                if c["chart"] is src:
                    continue
                c["chart"].sync_cursor(i)
        finally:
            self._syncing = False

    def refresh_all(self):
        klt = self.period.currentData() or 101
        codes = []
        for c in self._cells:
            raw = (c["edit"].text() or "").strip()
            code = "".join(ch for ch in raw if ch.isdigit())[:6]
            codes.append(code or c["code"])
        self.btn.setEnabled(False)
        self.hint.setText("取数中…")

        def job():
            out = []
            for code in codes:
                try:
                    bars, src = get_kline(code, klt, 300)
                except Exception as e:  # noqa: BLE001
                    bars, src = [], str(e)[:40]
                out.append((code, src, bars))
            return out

        r = TaskRunner(job)
        r.succeeded.connect(self._ok)
        r.failed.connect(self._err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _ok(self, payload):
        from ..engine.indicators import bars_to_df
        got = 0
        for cell, (code, src, bars) in zip(self._cells, payload):
            cell["code"] = code
            if not bars:
                cell["card"].set_hint(f"{code} 无数据 · {src}")
                continue
            got += 1
            cell["card"].set_hint(f"{code} · {len(bars)} 根 · {src}")
            cell["chart"].plot(bars_to_df(bars), unit="手")
        self.hint.setText(f"四图已更新({got}/4 有数据)· 同周期下标严格可比")
        self.status.emit(f"多图联动 {got}/4 完成")

    def _err(self, msg):
        self.hint.setText(f"失败:{str(msg)[:70]}")

    def stop(self):
        for t in self._tasks:
            if t.isRunning():
                t.wait(800)
        self._tasks.clear()
