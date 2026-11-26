# -*- coding: utf-8 -*-
"""实盘学习页: 展示灰风 OKX 实盘的决策记录、学习统计、参数建议。

数据来自量化库 okx-live 账户 + wallet/learn-notes.md。
"""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)

from .theme import ACCENT, TEXT_DIM

WALLET = r"G:\DeepSeek\工作区\灰风\wallet"
QUANT = r"G:\DeepSeek\工作区\量化"
PY = sys.executable


class LiveLearnPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("实盘学习 · okx-live")
        title.setStyleSheet(f"font-size:16px;font-weight:600;color:{ACCENT};")
        head.addWidget(title)
        head.addStretch(1)
        self.btn = QPushButton("刷新")
        self.btn.clicked.connect(self.refresh)
        head.addWidget(self.btn)
        lay.addLayout(head)

        self.hint = QLabel("决策记录来自量化库 okx-live，教训来自 wallet/learn-notes.md")
        self.hint.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        lay.addWidget(self.hint)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family:Consolas,monospace;font-size:13px;")
        lay.addWidget(self.text, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60000)
        self.refresh()

    def refresh(self) -> None:
        parts = []
        # 1. 量化库统计
        try:
            code = (
                "import sys; sys.path.insert(0, r'%s');"
                "from quantlab.crypto.learn import DecisionLog, daily_report;"
                "dl = DecisionLog('okx-live', 'multifactor');"
                "print(daily_report('okx-live', 'multifactor'))"
            ) % QUANT
            p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=10, cwd=QUANT)
            parts.append(p.stdout.strip() or p.stderr.strip() or "(无数据)")
        except Exception as e:  # noqa: BLE001
            parts.append(f"量化库读取失败: {e}")

        # 2. 教训文件
        try:
            notes = os.path.join(WALLET, "learn-notes.md")
            if os.path.exists(notes):
                txt = open(notes, encoding="utf-8", errors="replace").read()
                parts.append("\n\n===== 实盘教训（最近 3000 字）=====\n" + txt[-3000:])
        except Exception:
            pass

        self.text.setPlainText("\n".join(parts))

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
