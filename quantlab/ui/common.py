# -*- coding: utf-8 -*-
"""通用界面组件:卡片、指标块、表格辅助。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QSizePolicy, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .theme import ACCENT, BORDER, DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP


class CardHead(QFrame):
    """卡片标题栏:双击把卡片在区域里铺满 / 还原。"""

    double_clicked = Signal()

    def __init__(self, title: str, hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("CardHead")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 11, 12, 9)
        lay.setSpacing(8)
        # 标题前的一小段强调色:整屏卡片长得一样,靠它做视觉锚点
        self.bar = QFrame()
        self.bar.setFixedSize(3, 12)
        self.bar.setStyleSheet(f"background:{ACCENT};border-radius:1px;")
        lay.addWidget(self.bar)
        self.title = QLabel(title)
        self.title.setObjectName("CardTitle")
        lay.addWidget(self.title)
        self.hint = QLabel(hint)
        self.hint.setObjectName("CardHint")
        lay.addWidget(self.hint)
        lay.addStretch(1)
        self.tip = QLabel("⠿")
        self.tip.setStyleSheet(f"color:#3a4757;font-size:11px;")
        lay.addWidget(self.tip)
        self.setToolTip("拖动四周的分栏线调整大小 · 双击标题栏铺满 / 还原")
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.double_clicked.emit()
        event.accept()

    def set_hint(self, text: str) -> None:
        self.hint.setText(text)


class Card(QFrame):
    """带标题的基础卡片。内容往 self._lay 里加。"""

    title_double_clicked = Signal()

    def __init__(self, title: str = "", parent=None, spacing: int = 8,
                 hint: str = "", pad: int = 14):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self._lay = QVBoxLayout()
        self._lay.setContentsMargins(16, 12, 16, pad)
        self._lay.setSpacing(spacing)
        self.title_label = None
        self.head = None
        if title or hint:
            self.head = CardHead(title, hint)
            self.head.double_clicked.connect(self.title_double_clicked)
            self.title_label = self.head.title
            self._outer.addWidget(self.head)
        else:
            self._lay.setContentsMargins(16, pad, 16, pad)
        self._outer.addLayout(self._lay, 1)

    def body(self) -> QVBoxLayout:
        return self._lay

    def add(self, w: QWidget, stretch: int = 0):
        self._lay.addWidget(w, stretch)
        return w

    def set_hint(self, text: str) -> None:
        if self.head is not None:
            self.head.set_hint(text)


class StatCard(QFrame):
    """一个数字 + 说明。"""

    def __init__(self, label: str, value: str = "-", hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("CardFlat")
        self.setAttribute(Qt.WA_Hover, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 11, 14, 11)
        lay.setSpacing(3)
        self.lbl = QLabel(label)
        self.lbl.setObjectName("CardTitle")
        self.val = QLabel(value)
        self.val.setObjectName("BigNum")
        self.hint = QLabel(hint)
        self.hint.setObjectName("CardHint")
        lay.addWidget(self.lbl)
        lay.addWidget(self.val)
        lay.addWidget(self.hint)

    def set(self, value: str, color: str | None = None, hint: str | None = None):
        self.val.setText(value)
        # 数字块宽度是固定的(一行塞四块),4 个汉字按 21px 排会被切掉半截
        # —— 长结论自动降字号,再挂 tooltip 兜全文
        n = len(str(value))
        size = 21 if n <= 2 else 18 if n == 3 else 16 if n == 4 else 13
        self.val.setStyleSheet(f"color:{color or TEXT};font-size:{size}px;")
        self.val.setToolTip(str(value))
        if hint is not None:
            self.hint.setText(hint)
            self.hint.setToolTip(str(hint))


class SmartTable(QTableWidget):
    """列宽跟着可视宽度走的表格(全部列都是 Interactive,横滚靠 Qt 自己)。

    三档策略:
      · 放得下    → 按声明宽度排,富余按各列比例摊(不再全塞给最后一列)
      · 放不下    → 等比收缩,但不低于 MIN_COL
      · 收到底了  → 保持 MIN_COL,横向滚动条出现,右端列能滚到

    踩过的坑:**绝对不能用 QHeaderView.Stretch / setStretchLastSection**。
    只要有一列是 Stretch,表头长度就等于视口宽度而不是列宽之和,
    横向滚动时表头和数据就错位(表现为「标签栏对不齐」),
    而且被挤掉的列既没有滚动条也看不到(表现为「严重挡数据」)。
    所以「吃富余」的列改成自己算:其余列排完,剩下的宽度给 stretch_col。
    """

    MIN_COL = 68       # 「+0.07%」「黄河旋风」这类至少要这么宽才不打省略号
    MAX_AUTO = 260     # 没声明宽度的表,按内容量宽度时给个上限
    AUTO_ROWS = 60     # 量内容宽度只看前几十行,几千行的表不会卡

    def __init__(self, headers: list[str], parent=None):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(29)
        self.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        hh = self.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self._want: list[int] = []
        self._stretch_col: int | None = None
        self._auto = False          # 没声明宽度 → 按内容量一次再自适应
        self._auto_sig: tuple = ()
        self._floors_cache: list[int] = []
        self._floor_sig: tuple = ()
        self._col_left: dict[int, bool] = {}   # 每列表头是否左对齐(粘性判定)
        self._fitting = False
        self._fit_pending = False
        # 数据是「先排好版、后填内容」的:单元格填进来时下限要重算,
        # 但每次 setItem 都重排太贵,这里攒 120ms 再排一次
        self._data_timer = QTimer(self)
        self._data_timer.setSingleShot(True)
        self._data_timer.setInterval(120)
        self._data_timer.timeout.connect(self._on_data_settled)
        # 监听 model 的 dataChanged 而不是 widget 的 itemChanged:
        # 页面填数据时常 blockSignals(True),那样 itemChanged 不触发,
        # 表头对齐和列宽下限就会停在占位数据算出来的结果上。
        self.model().dataChanged.connect(self._on_data_changed)
        # 用户拖列宽 → 把新宽度记成声明宽度,否则下次数据刷新重排会把它改回去
        hh.sectionResized.connect(self._on_section_resized)

        # 空表提示:一张空表格只剩表头,看着像坏了
        self._empty_text = "暂无数据"
        self._empty = QLabel(self._empty_text, self.viewport())
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._empty.setStyleSheet(f"color:{TEXT_MUT};font-size:12px;background:transparent;")
        self._empty.hide()
        self.model().rowsInserted.connect(self._on_rows_changed)
        self.model().rowsRemoved.connect(self._on_rows_changed)
        self.model().modelReset.connect(self._on_rows_changed)

    def _on_rows_changed(self, *_args) -> None:
        """行数变了就同步空表提示。

        必须用绑定方法而不是 lambda:lambda 不会随控件销毁自动断开,
        页面重建后旧表的 model 信号还会打到已删除的 C++ 对象上。
        """
        self._sync_empty()

    def set_empty_text(self, text: str) -> None:
        self._empty_text = text
        self._empty.setText(text)

    def _sync_empty(self) -> None:
        show = self.rowCount() == 0
        if show:
            self._empty.setGeometry(self.viewport().rect())
        self._empty.setVisible(show)
        if show:
            self._empty.raise_()

    def _on_data_changed(self, *_args) -> None:
        self._floor_sig = ()
        self._auto_sig = ()
        if not self._data_timer.isActive():
            self._data_timer.start()

    def _on_section_resized(self, col: int, _old: int, new: int) -> None:
        if self._fitting or col < 0 or col >= len(self._want):
            return
        if new > 0 and new != self._want[col]:
            self._want[col] = int(new)

    def _on_item_changed(self, _item) -> None:
        self._on_data_changed()

    def _on_data_settled(self) -> None:
        if self._fitting:
            return
        self._fit()

    # ------------------------------------------------------------ 列宽
    def set_cols(self, widths: list[int], stretch_col: int | None = None) -> None:
        """声明各列想要的宽度。之后由 _fit() 按实际可视宽度收放。"""
        self._want = [int(w) for w in widths]
        self._stretch_col = stretch_col
        self._auto = False
        n = min(len(self._want), self.columnCount())
        self._fitting = True
        try:
            for i in range(n):
                self.setColumnWidth(i, self._want[i])
        finally:
            self._fitting = False
        self._fit()

    def set_col_width(self, col: int, width: int) -> None:
        """改某一列的声明宽度(之后再自适应也跟着变)。"""
        if 0 <= col < len(self._want):
            self._want[col] = int(width)
        self._auto = False
        self.setColumnWidth(col, int(width))

    def auto_cols(self) -> None:
        """没声明宽度时用:按内容量一次列宽,之后跟窗口自适应。"""
        self._auto = True
        self._stretch_col = None
        self._auto_sig = ()
        self._fit()

    # ------------------------------------------------------------ 尺寸
    def _floors(self) -> list[int]:
        """每列的最小可读宽度。

        以前只有一条统一的 MIN_COL:表头「成交量(手)」塞进 56px 必然变
        省略号,数字列也会被压到只剩几个字符。现在下限按内容算:
          表头文字宽 + 左右 padding,再和「前几十行数据的文字宽」取较大值,
          上限 MAX_AUTO 防止长文本列把整张表撑爆。
        """
        n = self.columnCount()
        if n == 0:
            return []
        hh = self.horizontalHeader()
        # 签名必须带上数据内容:有些页面填数据时 blockSignals,
        # 只按「行数 + 表头」缓存会一直用空表算出来的下限(列宽永远 56)。
        sample = []
        for r in range(min(self.rowCount(), 3)):
            for c in range(n):
                it = self.item(r, c)
                sample.append(it.text() if it is not None else "")
        sig = (self.rowCount(), n,
               tuple(self.horizontalHeaderItem(i).text() if self.horizontalHeaderItem(i) else ""
                     for i in range(n)),
               tuple(sample))
        if sig == self._floor_sig and len(self._floors_cache) == n:
            return list(self._floors_cache)
        hfont = hh.font()
        hm = QFontMetrics(hfont)
        rows = min(self.rowCount(), self.AUTO_ROWS)
        floors: list[int] = []
        for c in range(n):
            item = self.horizontalHeaderItem(c)
            text = item.text() if item else ""
            need = hm.horizontalAdvance(text) + 20          # QSS: padding 8+8 + 4 余量
            # 数据宽度(只看前几十行,几千行的表不能全扫)
            cf = QFontMetrics(self.font())
            data_need = 0
            for r in range(rows):
                it = self.item(r, c)
                if it is None:
                    continue
                w = cf.horizontalAdvance(it.text()) + 18    # QSS: item padding 8+8
                if w > data_need:
                    data_need = w
            if data_need:
                # 数据比表头长时以数据为准(时间「11-08 19:14:25」、
                # 「+8.31%」这类不该变省略号),上限 MAX_AUTO 防止长文本列撑爆
                need = max(need, min(data_need, self.MAX_AUTO))
            floors.append(max(self.MIN_COL, need))
        self._floors_cache = floors
        self._floor_sig = sig
        return list(floors)

    def _vscroll_w(self) -> int:
        """纵向滚动条要出现时先扣它的宽度,否则刚排满又多出一条横滚。"""
        vsb = self.verticalScrollBar()
        if self.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff:
            return 0
        if vsb.isVisible():
            return vsb.width()
        rows = self.rowCount()
        if rows <= 0:
            return 0
        need = rows * self.verticalHeader().defaultSectionSize() + 2
        if need > self.viewport().height():
            return vsb.sizeHint().width()
        return 0

    def _content_widths(self) -> list[int]:
        """按表头 + 前若干行内容估列宽(只量一次,签名不变就不重复量)。"""
        n = self.columnCount()
        hh = self.horizontalHeader()
        sig = (self.rowCount(), n,
               tuple(self.horizontalHeaderItem(i).text() if self.horizontalHeaderItem(i) else ""
                     for i in range(n)))
        if sig == self._auto_sig and len(self._want) == n:
            return list(self._want)
        rows = min(self.rowCount(), self.AUTO_ROWS)
        want = []
        for c in range(n):
            w = max(self.sizeHintForColumn(c), self.sizeHintForColumnHeader(c))
            want.append(min(self.MAX_AUTO, max(self.MIN_COL, w + 12)))
        self._want = want
        self._auto_sig = sig
        return list(want)

    def _fit(self) -> None:
        if self._fitting:
            return
        n = self.columnCount()
        if n == 0:
            return
        vw = self.viewport().width()
        if vw <= 0:
            return
        if self._auto:
            self._content_widths()
        if not self._want:
            return
        # 每次重排都校一遍表头对齐:数据可能是后填的,对齐方式跟表头默认值不同
        self._sync_header_align()
        vw -= self._vscroll_w()
        if vw <= self.MIN_COL:
            vw = self.viewport().width()
        n = min(len(self._want), n)
        floors = self._floors()[:n]
        self._fitting = True
        try:
            st = self._stretch_col
            if st is not None and 0 <= st < n:
                # 拉伸列自己算:其余列按比例收完,剩下的全给它(不低于它的下限)
                fixed = [i for i in range(n) if i != st]
                sizes_f = self._scale([self._want[i] for i in fixed],
                                      max(0, vw - floors[st]))
                used = 0
                sizes = [0] * n
                for i, w in zip(fixed, sizes_f):
                    sizes[i] = max(floors[i], w)
                    used += sizes[i]
                sizes[st] = max(floors[st], vw - used)
                # 几像素的溢出(取整误差)从宽列里抹掉,别留一条 3px 的滚动条
                if 0 < sum(sizes) - vw < 20:
                    sizes = self._trim(sizes, vw, floors)
                for i, w in enumerate(sizes):
                    if self.columnWidth(i) != w:
                        self.setColumnWidth(i, w)
                return
            total = sum(self._want[:n])
            if total <= vw:
                extra = vw - total
                sizes = [self._want[i] + int(extra * self._want[i] / total)
                         for i in range(n)]
            else:
                sizes = self._scale(self._want[:n], vw)
            # 不低于可读下限:宁可出横向滚动条,也不让表头/数字变省略号
            sizes = [max(floors[i], sizes[i]) for i in range(n)]
            sizes = self._trim(sizes, vw, floors)
            for i, w in enumerate(sizes):
                if self.columnWidth(i) != w:
                    self.setColumnWidth(i, w)
        finally:
            self._fitting = False

    @staticmethod
    def _trim(sizes: list[int], avail: int, floors: list[int] | None = None) -> list[int]:
        """只超一点点(<20px)时从最宽的列里各减 1,别为几像素留条滚动条。

        超过 20px 就说明真的放不下,滚动条该出来就出来。
        减不到列自己的下限(floors)就停手,不能靠压字省空间。
        """
        over = sum(sizes) - avail
        if over <= 0 or over >= 20:
            return sizes
        out = list(sizes)
        while over > 0:
            order = sorted(range(len(out)), key=lambda i: -out[i])
            moved = False
            for i in order:
                low = floors[i] if floors and i < len(floors) else SmartTable.MIN_COL
                if out[i] > low and over > 0:
                    out[i] -= 1
                    over -= 1
                    moved = True
            if not moved:
                break
        return out

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()
        self._schedule_fit()
        self._sync_empty()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._fit()
        self._schedule_fit()
        self._sync_empty()

    # 占位值:数据还没到时的 "-" / "--"。它们的对齐方式不代表这一列的真实意图,
    # 统计表头对齐时必须跳过,否则表头会跟着占位值右对齐、真数据到了又变左对齐。
    _PLACEHOLDERS = {"", "-", "--", "—", "…", "N/A", "n/a"}

    def _sync_header_align(self) -> None:
        """表头文字对齐跟着该列数据走,且判定是「粘性」的。

        表头默认一律左对齐,而数字列的数据是右对齐的 —— 视觉上就是
        「表头文字压在数字左边、跟数据对不齐」。按每列数据的主导对齐方式
        设置表头,数字列表头也右对齐,文字列表头保持左对齐。

        粘性:数据刷新时单元格会先被写成 "-" 占位再填真值,如果每次都按
        当前内容重判,表头就会在左右之间来回跳。所以只在看到真数据时更新
        判定,占位值一律沿用上一次的结论。
        """
        rows = min(self.rowCount(), 30)
        if not hasattr(self, "_col_left"):
            # __init__ 里 setHorizontalHeaderLabels 会先于属性初始化被调用
            self._col_left = {}
        for c in range(self.columnCount()):
            item = self.horizontalHeaderItem(c)
            if item is None:
                continue
            left = right = 0
            for r in range(rows):
                it = self.item(r, c)
                if it is None or it.text().strip() in self._PLACEHOLDERS:
                    continue
                a = int(it.textAlignment())
                if a & int(Qt.AlignRight):
                    right += 1
                elif a & int(Qt.AlignLeft):
                    left += 1
            if right or left:
                # 有真值就按多数更新判定(占位值已排除,不会来回跳)。
                # 全占位时沿用上一次结论,避免表头在左右之间闪。
                self._col_left[c] = left >= right
            align = Qt.AlignLeft if self._col_left.get(c, True) else Qt.AlignRight
            want = int(align) | int(Qt.AlignVCenter)
            if int(item.textAlignment()) != want:
                item.setTextAlignment(want)

    def setHorizontalHeaderLabels(self, labels) -> None:  # noqa: N802
        super().setHorizontalHeaderLabels(labels)
        # 表头文案变了 → 宽度下限和文字对齐都要重算
        self._floor_sig = ()
        self._sync_header_align()
        self._schedule_fit()

    def _schedule_fit(self) -> None:
        """再排一次:纵向滚动条出现/消失会改可视宽度,一次排不准。"""
        if getattr(self, "_fit_pending", False):
            return
        self._fit_pending = True

        def run() -> None:
            self._fit_pending = False
            self._sync_header_align()
            self._fit()

        QTimer.singleShot(0, self, run)

    def setRowCount(self, rows: int) -> None:  # noqa: N802
        super().setRowCount(rows)
        # 行数变了 → 内容宽度和纵向滚动条都可能变,重排
        if self._auto:
            self._auto_sig = ()
        self._schedule_fit()
        self._sync_empty()

    @staticmethod
    def _scale(want: list[int], avail: int) -> list[int]:
        """等比收缩,但最多缩到 80%、且不低于 MIN_COL。

        这里踩过一次坑:以前「压到最小还差几像素」时会把所有列统一压成
        MIN_COL(56px),结果 8 列全 56px、数字全变省略号 —— 这才是真正的
        「严重挡数据」。现在的做法是:
          · 缩到 80% 还能装下 → 缩,不留横向滚动条
          · 缩到 80% 仍装不下 → 就按 80% 排,让横向滚动条出现,
            列宽保持可读,右端列滚一下就能看到
        """
        total = sum(want) or 1
        if avail >= total:
            return list(want)
        k = max(0.80, max(0.0, avail / total))
        return [max(SmartTable.MIN_COL, int(w * k)) for w in want]


def table(headers: list[str], widths: list[int] | None = None,
          stretch_col: int | None = None) -> SmartTable:
    """建一张表。

    · 给了 widths:按声明宽度自适应(富余按比例摊,不够就收缩,再不够出横滚)
    · 没给 widths:按内容量一次宽度,之后同样自适应
    · 显式给 stretch_col 但没给 widths:该列吃富余
    三种都不使用 Qt 的 Stretch 模式 —— 那会让表头与数据错位、且断掉横向滚动。
    """
    t = SmartTable(headers)
    if widths:
        t.set_cols(widths, stretch_col)
    else:
        t._stretch_col = stretch_col
        t.auto_cols()
    return t


def set_cell(t: QTableWidget, row: int, col: int, text: str,
             color: str | None = None, bold: bool = False, align: str = "right"):
    """写单元格。值没变就完全跳过——高频刷新时这一条能省掉大量重绘。

    文字比列宽长时自动挂 tooltip 显示全文:表格里难免有长文本列
    (新闻标题、策略条件、日志原文),加宽到能装下会把别的列挤走,
    省略号 + 悬停看全文才是正解。
    """
    item = t.item(row, col)
    if item is not None and item.text() == text and not bold:
        if color is None or (item.foreground().color().name() == color.lower()):
            return item
    if item is None:
        item = QTableWidgetItem()
        t.setItem(row, col, item)
    changed = item.text() != text
    if changed:
        item.setText(text)
    if color:
        item.setForeground(QColor(color))
    if bold and not item.font().bold():
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    item.setTextAlignment((Qt.AlignLeft if align == "left" else Qt.AlignRight) | Qt.AlignVCenter)
    if changed:
        # 列宽会变,这里只按「当前列宽」判一次;多挂一个 tooltip 不影响正确性
        try:
            need = QFontMetrics(item.font()).horizontalAdvance(text) + 18
            item.setToolTip(text if need > t.columnWidth(col) else "")
        except Exception:
            pass
    return item


def pnl_color(v: float | None) -> str:
    if v is None or v == 0:
        return FLAT
    return UP if v > 0 else DOWN


def hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{BORDER};")
    return f


def hbox(*widgets, spacing: int = 8, stretch_last: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    return w


def label(text: str, dim: bool = False, size: int | None = None, bold: bool = False) -> QLabel:
    l = QLabel(text)
    if dim:
        l.setObjectName("Dim")
        l.setStyleSheet(f"color:{TEXT_DIM};")
    if size:
        l.setStyleSheet(f"font-size:{size}px;")
    if bold:
        f = l.font()
        f.setBold(True)
        l.setFont(f)
    return l
