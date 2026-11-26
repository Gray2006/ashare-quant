# -*- coding: utf-8 -*-
"""实盘页: 展示灰风 OKX 实盘状态(现货+合约+信号+守护)。

数据来自 G:\\DeepSeek\\工作区\\灰风\\wallet\\status_all.py 的输出,
本页只做展示, 不发起任何交易。
"""
from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)

from .theme import ACCENT, TEXT_DIM

WALLET = r"G:\DeepSeek\工作区\灰风\wallet"
PY = sys.executable


class LivePage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("实盘 · 灰风 OKX")
        title.setStyleSheet(f"font-size:16px;font-weight:600;color:{ACCENT};")
        head.addWidget(title)
        head.addStretch(1)
        self.btn = QPushButton("刷新")
        self.btn.clicked.connect(self.refresh)
        head.addWidget(self.btn)
        lay.addLayout(head)

        self.hint = QLabel("数据来自 wallet/status_all.py，只读展示")
        self.hint.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        lay.addWidget(self.hint)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family:Consolas,monospace;font-size:13px;")
        lay.addWidget(self.text, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(30000)
        self.refresh()

    def refresh(self) -> None:
        try:
            p = subprocess.run([PY, WALLET + r"\status_all.py"],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=10,
                               cwd=WALLET)
            out = p.stdout.strip() or p.stderr.strip() or "(无输出)"
        except Exception as e:  # noqa: BLE001
            out = f"读取失败: {e}"
        self.text.setPlainText(out)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
