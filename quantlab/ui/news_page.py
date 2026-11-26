# -*- coding: utf-8 -*-
"""资讯页:分类过滤 + 关键词搜索 + 双击开原文。

数据走 `data/newsfeed.feed(cat, "", limit)`,它把快讯(联网)和本地档案
(news_archive)合并好了,每条含 time / cat / title / source / url / summary。

**档案行没有 url**(news_archive 表压根没有 url 列),所以双击时 url 为空就
明确提示「本地档案没有原文链接」,不编造、不乱开浏览器。
分类用 newsfeed.CATEGORIES 的真实分类(快讯/政策/个股/公告),不硬凑。

关键词在本地过滤(不重新联网),命中的标题用 WARN 色加粗标出。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..data import newsfeed
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, label, set_cell, table
from .theme import TEXT_DIM, TEXT_MUT, WARN

LIMIT = 400


class NewsPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._items: list[dict] = []
        self._filtered: list[dict] = []
        self._build()
        QTimer.singleShot(900, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("资讯", hint="快讯联网抓取 + 本地档案 · 档案行没有原文链接")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("分类", dim=True))
        self.cat = QComboBox()
        for name, key in getattr(newsfeed, "CATEGORIES", [("全部", "all")]):
            self.cat.addItem(name, key)
        self.cat.setMinimumWidth(96)
        self.cat.currentIndexChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.cat)
        self.kw = QLineEdit()
        self.kw.setPlaceholderText("关键词(标题/摘要),回车过滤,命中的标题标黄")
        self.kw.returnPressed.connect(self._apply_filter)
        bar.addWidget(self.kw, 1)
        self.btn = QPushButton("刷新")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        self.hint = label("", dim=True)
        bar.addWidget(self.hint)
        top.body().addLayout(bar)
        root.addWidget(top)

        board = Board("news", cols=[1.0], rows=[1.0])
        card = Card("资讯列表")
        self.tbl = table(["时间", "分类", "标题", "来源"],
                         widths=[136, 60, 560, 190], stretch_col=2)
        self.tbl.setObjectName("NewsList")
        self.tbl.cellDoubleClicked.connect(self._open)
        card.add(self.tbl, 1)
        self.tip = label("双击开原文;本地档案没有链接,会提示而不是瞎打开", dim=True)
        card.add(self.tip)
        board.add(card, 0, 0, min_h=320)
        self.card = card
        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def refresh(self):
        cat = self.cat.currentData() or "all"
        self.btn.setEnabled(False)
        self.hint.setText("取数中…")

        def job():
            # 关键词不交给数据层:拿全量回来本地过滤,搜索就不用反复联网
            return newsfeed.feed(cat, "", LIMIT)

        r = TaskRunner(job)
        r.succeeded.connect(self._ok)
        r.failed.connect(self._err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _ok(self, items):
        self._items = list(items or [])
        self.card.set_hint(f"{self.cat.currentText()} · {len(self._items)} 条")
        if not self._items:
            self.tbl.setRowCount(0)
            self._filtered = []
            self.hint.setText("没有资讯:快讯要联网,档案要先抓过(个股/政策/公告)")
            self.status.emit(f"资讯 {self.cat.currentText()} 为空")
            return
        self._apply_filter()

    def _err(self, msg):
        self.hint.setText(f"取数失败:{str(msg)[:80]}")
        self.status.emit(f"资讯取数失败:{str(msg)[:100]}")

    def _apply_filter(self):
        kw = (self.kw.text() or "").strip()
        low = kw.lower()
        if low:
            rows = [x for x in self._items
                    if low in str(x.get("title") or "").lower()
                    or low in str(x.get("summary") or "").lower()]
        else:
            rows = list(self._items)
        self._filtered = rows
        self.tbl.setRowCount(len(rows))
        for r, it in enumerate(rows):
            title = it.get("title") or ""
            hit = bool(low) and low in title.lower()
            set_cell(self.tbl, r, 0, str(it.get("time") or "")[:16],
                     TEXT_DIM, align="left")
            set_cell(self.tbl, r, 1, it.get("cat") or "", TEXT_MUT, align="left")
            cell = set_cell(self.tbl, r, 2, title, WARN if hit else None,
                            bold=hit, align="left")
            summary = it.get("summary") or ""
            if summary or it.get("url"):
                cell.setToolTip(summary or it.get("url"))
            set_cell(self.tbl, r, 3, it.get("source") or "", TEXT_DIM, align="left")
        with_url = sum(1 for x in rows if (x.get("url") or "").strip())
        extra = f" · 命中 {len(rows)}" if low else ""
        self.hint.setText(f"{len(rows)} 条{extra} · {with_url} 条可开原文")
        self.status.emit(f"资讯 {self.cat.currentText()} {len(rows)} 条")

    def _open(self, row: int, _col: int):
        from .common import row_key
        title = row_key(self.tbl, row, 2)
        it = next((x for x in self._filtered
                   if (x.get("title") or "") == title), None)
        if it is None:
            return
        url = (it.get("url") or "").strip()
        if not url:
            self.tip.setText(f"「{it.get('source') or '本地档案'}」没有原文链接,"
                             "只有标题和摘要 —— 不编造地址")
            self.status.emit("该条没有原文链接")
            return
        from ..safeurl import open_external
        if not open_external(url, self):
            self.tip.setText("这条链接不是安全的网页地址,已拦截")
            return
        self.tip.setText(f"已打开:{url[:80]}")

    def stop(self):
        for t in self._tasks:
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
        self._tasks.clear()
