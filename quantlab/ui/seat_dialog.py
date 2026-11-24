# -*- coding: utf-8 -*-
"""席位画像弹窗:双击一个营业部,看它怎么买、拿多久、跟买有没有用。

画像计算要碰几十只票的日线,几秒钟,所以走后台线程,窗口先开再填。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QTextBrowser, QVBoxLayout)

from ..engine.hub import TaskRunner
from ..engine import seat_profile as sp
from .theme import BORDER, TEXT, TEXT_DIM, TEXT_MUT


class SeatDialog(QDialog):
    def __init__(self, seat: str, parent=None):
        super().__init__(parent)
        self.seat = seat
        self._task: TaskRunner | None = None
        self.setWindowTitle(f"席位画像 · {seat}")
        self.resize(880, 720)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        self.hint = QLabel("计算中…(要读几十只票的日线,几秒钟)")
        self.hint.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        head.addWidget(self.hint, 1)
        self.btn = QPushButton("重新计算")
        self.btn.clicked.connect(self.refresh)
        head.addWidget(self.btn)
        lay.addLayout(head)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setStyleSheet(
            f"QTextBrowser{{background:#0e141b;border:1px solid {BORDER};"
            f"border-radius:6px;color:{TEXT};padding:10px;}}")
        self.view.setFont(QFont("Microsoft YaHei UI", 10))
        lay.addWidget(self.view, 1)

        self.refresh()

    def refresh(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self.btn.setEnabled(False)
        self.hint.setText("计算中…")
        seat = self.seat

        def job():
            return sp.profile(seat)

        t = TaskRunner(job, name="seat")
        t.succeeded.connect(self._ok)
        t.failed.connect(self._fail)
        t.finished.connect(lambda: self.btn.setEnabled(True))
        self._task = t
        t.start()

    def _ok(self, p: dict) -> None:
        md = sp.render_markdown(p)
        if p.get("ok"):
            self.hint.setText(
                f"{p['n']} 次净买入 · {p['codes']} 只票 · "
                f"风格 {' / '.join(p['tags'])} · 5日 {p['ret5']:+.2%}"
                + (f" · 超额 {p['excess5']:+.2%}" if p.get("excess5") is not None else ""))
        else:
            self.hint.setText(p.get("msg", "没有数据"))
        try:
            self.view.document().setMarkdown(md)
        except Exception:  # noqa: BLE001
            self.view.setPlainText(md)

    def _fail(self, msg: str) -> None:
        self.hint.setText(f"计算失败:{str(msg)[:90]}")

    def stop(self) -> None:
        if self._task is not None:
            try:
                if self._task.isRunning():
                    self._task.wait(1200)
            except Exception:  # noqa: BLE001
                pass
