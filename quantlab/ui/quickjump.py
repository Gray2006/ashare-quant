# -*- coding: utf-8 -*-
"""页面快跳面板(Ctrl+K)。

21 个页面光靠翻侧栏还是慢,这里直接打名字跳:输入「回」「bt」「mac」都能命中。
匹配是「子序列 + 中文包含」:输入的字符按顺序出现在
「页面名 分组 说明 key」里就算命中,所以中文片段、英文 key、首字母缩写都有效。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QDialog, QLineEdit, QListWidget, QListWidgetItem,
                              QVBoxLayout)

from .theme import ACCENT, BG2, BORDER, TEXT, TEXT_DIM, TEXT_MUT


def _score(query: str, name: str, group: str, hint: str, key: str = "") -> int:
    """越小越靠前;返回 -1 表示不匹配。

    key 也进匹配域:页面 key 是 backtest / macro 这种英文,
    打 bt、mac 这类缩写能靠子序列命中。
    """
    q = query.strip().lower()
    if not q:
        return 0
    hay = f"{name} {group} {hint} {key}".lower()
    if name.lower().startswith(q):
        return 0
    if key.startswith(q):
        return 0
    if q in name.lower():
        return 1
    # 子序列:q 的每个字符按顺序出现
    pos = 0
    for ch in q:
        pos = hay.find(ch, pos)
        if pos < 0:
            return -1
        pos += 1
    return 2 + (len(hay) - len(q)) // 100


class QuickJump(QDialog):
    """输入即过滤,回车直达。accept 后用 .key 取选中的页面 key。"""

    def __init__(self, nav: list[tuple[str, str, str]], group_of: dict[str, str],
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setObjectName("QuickJump")
        self.key: str | None = None
        self._nav = nav
        self._group_of = group_of

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        self.edit = QLineEdit()
        self.edit.setObjectName("QuickJumpEdit")
        self.edit.setPlaceholderText("跳到哪个页面…  回车直达 · Esc 取消")
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._filter)
        lay.addWidget(self.edit)

        self.lst = QListWidget()
        self.lst.setObjectName("QuickJumpList")
        self.lst.itemActivated.connect(lambda _i: self._pick())
        self.lst.itemClicked.connect(lambda _i: self._pick())
        lay.addWidget(self.lst, 1)

        self.resize(460, 380)
        self.setStyleSheet(f"""
        QDialog#QuickJump {{
            background: {BG2}; border: 1px solid {BORDER}; border-radius: 14px;
        }}
        QLineEdit#QuickJumpEdit {{
            background: #0d141d; border: 1px solid {BORDER}; border-radius: 9px;
            padding: 9px 11px; font-size: 14px;
        }}
        QLineEdit#QuickJumpEdit:focus {{ border-color: {ACCENT}; }}
        QListWidget#QuickJumpList {{
            background: transparent; border: none; outline: none;
        }}
        QListWidget#QuickJumpList::item {{
            padding: 8px 10px; border-radius: 8px; color: {TEXT_DIM};
        }}
        QListWidget#QuickJumpList::item:selected {{
            background: #1d2f47; color: {TEXT};
        }}
        """)
        self._filter("")

    # ------------------------------------------------------------ 过滤
    def _filter(self, text: str) -> None:
        self.lst.clear()
        rows = []
        for _name, key, hint in self._nav:
            g = self._group_of.get(key, "")
            s = _score(text, _name, g, hint, key)
            if s >= 0:
                rows.append((s, _name, key, hint, g))
        rows.sort(key=lambda r: (r[0], r[1]))
        for _s, name, key, hint, g in rows:
            item = QListWidgetItem(f"{g} / {name}")
            # 第一行给出说明,缩进一点,颜色压暗,不抢主标题
            item.setData(Qt.UserRole, key)
            item.setToolTip(hint)
            item.setForeground(QColor(TEXT_DIM))
            f = QFont()
            f.setPointSizeF(f.pointSizeF() + 0.5)
            item.setFont(f)
            self.lst.addItem(item)
        if self.lst.count():
            self.lst.setCurrentRow(0)

    # ------------------------------------------------------------ 交互
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit and event.type() == QEvent.KeyPress:
            k = event.key()
            if k in (Qt.Key_Down, Qt.Key_Up):
                row = self.lst.currentRow()
                n = self.lst.count()
                if n:
                    step = 1 if k == Qt.Key_Down else -1
                    self.lst.setCurrentRow(max(0, min(n - 1, row + step)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                self._pick()
                return True
            if k == Qt.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)

    def _pick(self) -> None:
        item = self.lst.currentItem()
        if item is None:
            return
        self.key = item.data(Qt.UserRole)
        self.accept()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.edit.setFocus()
