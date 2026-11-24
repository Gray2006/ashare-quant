# -*- coding: utf-8 -*-
"""负面清单弹窗:今天哪些票该躲。"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..engine.hub import TaskRunner
from ..engine import hotmoney as hm
from ..engine import hotmoney_filter as hf
from .common import Card, set_cell, table
from .theme import DOWN, TEXT_DIM, TEXT_MUT, UP


class HotFilterDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("游资负面清单 · 该躲的票")
        self.resize(860, 520)
        self._task = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        self.hint = QLabel("计算中…")
        self.hint.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        bar.addWidget(self.hint, 1)
        self.btn = QPushButton("重算")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        lay.addLayout(bar)

        card = Card("负面清单", hint="大额净买入 + 60 日区间高位 = 给别人抬轿")
        self.tbl = table(["代码", "名称", "60日位置", "近3日净买入(亿)", "理由"],
                         [80, 110, 80, 120, 380], stretch_col=4)
        self.tbl.setToolTip("双击跳到行情页看这只票")
        self.tbl.doubleClicked.connect(lambda *_: self._jump())
        card.add(self.tbl, 1)
        lay.addWidget(card, 1)

        self.refresh()

    def _jump(self) -> None:
        row = self.tbl.currentRow()
        if row < 0:
            return
        item = self.tbl.item(row, 0)
        code = (item.text() or "").strip() if item is not None else ""
        if len(code) != 6 or not code.isdigit():
            return
        win = self.parent().window() if self.parent() else None
        fn = getattr(win, "_goto_stock", None)
        if callable(fn):
            self.accept()
            fn(code)

    def refresh(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self.btn.setEnabled(False)
        self.hint.setText("计算中…")

        def job():
            day = hm.latest_day()
            if not day:
                return day, []
            return day, hf.screen(day)

        t = TaskRunner(job, name="hot-filter")
        t.succeeded.connect(self._ok)
        t.failed.connect(lambda m: self.hint.setText(f"计算失败:{str(m)[:80]}"))
        t.finished.connect(lambda: self.btn.setEnabled(True))
        self._task = t
        t.start()

    def _ok(self, payload) -> None:
        day, rows = payload
        if not day:
            self.hint.setText("库里还没有龙虎榜数据,先去龙虎榜页点「同步」")
            return
        self.hint.setText(f"基准日 {day} · 该躲 {len(rows)} 只"
                          f"(大额净买入 ≥{hf.NET_THRESHOLD / 1e8:.1f} 亿 且 "
                          f"位置 ≥{hf.POS_THRESHOLD:.0%})")
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            pos = r.get("pos")
            set_cell(self.tbl, i, 0, r["code"], TEXT_DIM, align="left")
            set_cell(self.tbl, i, 1, r.get("name", ""), align="left")
            set_cell(self.tbl, i, 2, "-" if pos is None else f"{pos:.0%}",
                     UP if (pos or 0) >= 0.9 else TEXT_DIM)
            set_cell(self.tbl, i, 3, f"{r['net'] / 1e8:+.2f}",
                     DOWN if r["net"] < 0 else UP)
            set_cell(self.tbl, i, 4, "; ".join(r["reasons"]), TEXT_MUT, align="left")

    def stop(self) -> None:
        if self._task is not None:
            try:
                if self._task.isRunning():
                    self._task.wait(1200)
            except Exception:  # noqa: BLE001
                pass
