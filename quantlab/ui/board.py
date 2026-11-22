# -*- coding: utf-8 -*-
"""Board:二维分栏容器,卡片四边都能拖。

QSplitter 只能沿一个方向分栏(要么左右,要么上下);Board 同时维护
列宽和行高,所以任意一张卡片的左/右/上/下边界都是可拖的分栏线。

· 布局按比例存进 config.json 的 ui.layout.<key>,窗口缩放自动跟随
· 双击分栏线 → 这块区域恢复默认比例
· 双击卡片标题栏 → 卡片铺满所在区域,再双击还原
· 比例是相对值,换分辨率、最大化窗口都不会跑偏

用法:

    b = Board("quote", cols=[0.46, 0.54], rows=[1.0])
    b.add(card, 0, 0)                      # 第 0 行第 0 列
    b.add(card2, 0, 1, min_w=520)          # 同一行第 1 列,给个最小宽度
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..config import cfg
from .theme import ACCENT

MIN_COL = 90.0      # 任何一列的兜底最小像素
MIN_ROW = 60.0


def _norm(vals: list[float]) -> list[float]:
    vals = [max(0.0, float(v)) for v in vals]
    total = sum(vals)
    if total <= 0:
        n = max(1, len(vals))
        return [1.0 / n] * n
    return [v / total for v in vals]


def _fit(sizes: list[float], needs: list[float], extent: float) -> list[float]:
    """把按比例算出来的尺寸收进 extent,同时保证每格不小于 needs。

    比例正常时结果与 sizes 完全一致;某格被最小值顶大时,只从还有裕量的
    格子里让,谁都不许压到 minimum 以下 —— 这样卡片永远不会互相盖住。
    """
    n = len(sizes)
    if n == 0:
        return []
    need_total = sum(needs)
    if need_total >= extent:
        # 空间不够,按需求等比缩,保证顺序不重叠
        k = extent / need_total if need_total > 0 else 0.0
        return [max(0.0, nd * k) for nd in needs]
    slack = extent - need_total
    extras = [max(0.0, s - nd) for s, nd in zip(sizes, needs)]
    extra_total = sum(extras)
    if extra_total <= 0:
        extras = list(sizes)
        extra_total = sum(extras) or 1.0
    return [nd + slack * e / extra_total for nd, e in zip(needs, extras)]


class _Item:
    __slots__ = ("w", "row", "col", "rspan", "cspan", "min_w", "min_h")

    def __init__(self, w: QWidget, row: int, col: int, rspan: int, cspan: int,
                 min_w: float, min_h: float):
        self.w = w
        self.row = row
        self.col = col
        self.rspan = rspan
        self.cspan = cspan
        self.min_w = float(min_w)
        self.min_h = float(min_h)


class _Grip(QWidget):
    """分栏线手柄:只占缝隙那条带,不压住卡片内容。"""

    def __init__(self, board: "Board", axis: str, index: int):
        super().__init__(board)
        self._board = board
        self.axis = axis            # "v" 竖线(左右拖) / "h" 横线(上下拖)
        self.index = index          # 第 index 条内部界线
        self._drag = None
        self._hover = False
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.SplitHCursor if axis == "v" else Qt.SplitVCursor)
        self.setToolTip("拖动调整大小 · 双击恢复默认")

    # ---------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        if not (self._hover or self._drag):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = QColor(ACCENT)
        col.setAlpha(170 if self._drag else 90)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        if self.axis == "v":
            w = 3 if self._drag else 2
            p.drawRoundedRect(QRect(self.width() // 2 - w // 2 + 1, 2, w, self.height() - 4),
                              w / 2, w / 2)
        else:
            h = 3 if self._drag else 2
            p.drawRoundedRect(QRect(2, self.height() // 2 - h // 2 + 1, self.width() - 4, h),
                              h / 2, h / 2)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    # ---------------------------------------------------------- 拖拽
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        self._drag = (event.globalPosition().toPoint(),
                      list(self._board._cols), list(self._board._rows))
        self.grabMouse()
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._drag:
            return
        start, cols0, rows0 = self._drag
        now = event.globalPosition().toPoint()
        if self.axis == "v":
            self._board._drag_col(self.index, now.x() - start.x(), cols0)
        else:
            self._board._drag_row(self.index, now.y() - start.y(), rows0)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._drag:
            return
        self._drag = None
        try:
            self.releaseMouse()
        except Exception:
            pass
        self._board._persist()
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._board.reset()


class Board(QWidget):
    """二维分栏容器。见模块说明。"""

    layout_changed = Signal()
    GAP = 10

    def __init__(self, key: str = "", cols: list[float] | None = None,
                 rows: list[float] | None = None, gap: int | None = None,
                 parent=None):
        super().__init__(parent)
        self._key = key or ""
        self._gap = int(self.GAP if gap is None else gap)
        self._cols = _norm(list(cols or [1.0]))
        self._rows = _norm(list(rows or [1.0]))
        self._default = (list(self._cols), list(self._rows))
        self._items: list[_Item] = []
        self._grips: list[_Grip] = []
        self._max: _Item | None = None
        self._max_state: tuple | None = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 容器本身别带大 minimum:嵌套 Board 会顶住行高,把下一块挤到重叠
        self.setMinimumSize(80, 40)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._load()

    # ---------------------------------------------------------- 结构
    def add(self, w: QWidget, row: int = 0, col: int = 0, rowspan: int = 1,
            colspan: int = 1, min_w: float = 180, min_h: float = 110) -> QWidget:
        w.setParent(self)
        item = _Item(w, row, col, max(1, rowspan), max(1, colspan), min_w, min_h)
        # 同一格塞两张卡 = 后者整张盖住前者,界面"看不清"多半是这个原因。
        # 不抛异常(旧布局可能触发),但要在 stderr 里喊一声。
        for other in self._items:
            if (other.row == item.row and other.col == item.col
                    and other.rspan == item.rspan and other.cspan == item.cspan):
                import sys
                print(f"[Board:{self._key}] 第 {row} 行第 {col} 列被塞了两张卡:"
                      f"{type(other.w).__name__} 与 {type(w).__name__} —— 后者会盖住前者",
                      file=sys.stderr)
                break
        self._items.append(item)
        sig = getattr(w, "title_double_clicked", None)
        if sig is not None:
            try:
                sig.connect(lambda it=item: self.toggle_max(it))
            except Exception:
                pass
        w.show()
        self._sync_grips()
        self._relayout()
        return w

    def items(self) -> list[QWidget]:
        return [it.w for it in self._items]

    def grips(self) -> list["_Grip"]:
        return list(self._grips)

    def item_of(self, w: QWidget):
        for it in self._items:
            if it.w is w:
                return it
        return None

    @property
    def key(self) -> str:
        return self._key

    @property
    def cols(self) -> list[float]:
        return list(self._cols)

    @cols.setter
    def cols(self, vals: list[float]) -> None:
        self._cols = _norm(list(vals))
        self._sync_grips()
        self._relayout()

    @property
    def rows(self) -> list[float]:
        return list(self._rows)

    @rows.setter
    def rows(self, vals: list[float]) -> None:
        self._rows = _norm(list(vals))
        self._sync_grips()
        self._relayout()

    # ---------------------------------------------------------- 几何
    def _extent(self) -> tuple[float, float]:
        nc, nr = len(self._cols), len(self._rows)
        return (max(0.0, float(self.width() - self._gap * (nc - 1))),
                max(0.0, float(self.height() - self._gap * (nr - 1))))

    def _geometry_map(self) -> tuple[list[float], list[float], list[float], list[float]]:
        cw, ch = self._extent()
        ws = _fit([f * cw for f in self._cols], self._col_min_px(), cw)
        hs = _fit([f * ch for f in self._rows], self._row_min_px(), ch)
        xs: list[float] = []
        x = 0.0
        for w in ws:
            xs.append(x)
            x += w + self._gap
        ys: list[float] = []
        y = 0.0
        for h in hs:
            ys.append(y)
            y += h + self._gap
        return xs, ys, ws, hs

    def _relayout(self) -> None:
        xs, ys, ws, hs = self._geometry_map()
        nc, nr = len(self._cols), len(self._rows)
        for it in self._items:
            if it.w.isHidden():
                continue
            if self._max is it:
                it.w.setGeometry(QRect(0, 0, self.width(), self.height()))
                continue
            c0 = min(max(0, it.col), nc - 1)
            r0 = min(max(0, it.row), nr - 1)
            c1 = min(nc, c0 + it.cspan)
            r1 = min(nr, r0 + it.rspan)
            w = sum(ws[c0:c1]) + self._gap * (c1 - c0 - 1)
            h = sum(hs[r0:r1]) + self._gap * (r1 - r0 - 1)
            it.w.setGeometry(QRect(int(xs[c0]), int(ys[r0]),
                                   max(1, int(w)), max(1, int(h))))
        # 嵌套 Board 不靠 resizeEvent 兜底(页面隐藏时 Qt 不发),这里直接递归排
        for it in self._items:
            if not it.w.isHidden() and isinstance(it.w, Board):
                it.w._relayout()
        self._place_grips()

    def _place_grips(self) -> None:
        xs, ys, _, _ = self._geometry_map()
        vi = hi = 0
        for g in self._grips:
            if g.isHidden():
                continue
            if g.axis == "v":
                x = xs[g.index] - self._gap
                g.setGeometry(QRect(int(x) - 1, 0, self._gap + 2, self.height()))
                vi += 1
            else:
                y = ys[g.index] - self._gap
                g.setGeometry(QRect(0, int(y) - 1, self.width(), self._gap + 2))
                hi += 1
            g.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._relayout()

    # ---------------------------------------------------------- 手柄
    def _sync_grips(self) -> None:
        want_v = len(self._cols) - 1
        want_h = len(self._rows) - 1
        have_v = sum(1 for g in self._grips if g.axis == "v")
        have_h = sum(1 for g in self._grips if g.axis == "h")
        if have_v == want_v and have_h == want_h:
            return
        for g in self._grips:
            g.hide()
            g.deleteLater()
        self._grips = []
        for i in range(1, want_v + 1):
            g = _Grip(self, "v", i)
            g.show()
            self._grips.append(g)
        for i in range(1, want_h + 1):
            g = _Grip(self, "h", i)
            g.show()
            self._grips.append(g)
        self._place_grips()

    # ---------------------------------------------------------- 约束
    def _col_min_px(self) -> list[float]:
        mins = [MIN_COL] * len(self._cols)
        for it in self._items:
            if it.w.isHidden():
                continue
            span = max(1, it.cspan)
            hint = 0
            if isinstance(it.w, Board):
                hint = it.w.min_extent()[0]
            elif it.w.layout() is not None:
                hint = it.w.minimumSizeHint().width()
            need = max(it.min_w, 0, hint) / span
            for c in range(it.col, min(len(self._cols), it.col + span)):
                mins[c] = max(mins[c], need)
        return mins

    def _row_min_px(self) -> list[float]:
        mins = [MIN_ROW] * len(self._rows)
        for it in self._items:
            if it.w.isHidden():
                continue
            span = max(1, it.rspan)
            hint = 0
            if isinstance(it.w, Board):
                # 嵌套 Board 的 minimumSizeHint 是 (80,40),不能代表里面卡片
                # 真正需要的高度 —— 用它的 min_extent,否则外层永远觉得装得下
                hint = it.w.min_extent()[1]
            elif it.w.layout() is not None:
                hint = it.w.minimumSizeHint().height()
            need = max(it.min_h, 0, hint) / span
            for r in range(it.row, min(len(self._rows), it.row + span)):
                mins[r] = max(mins[r], need)
        return mins

    def min_extent(self) -> tuple[float, float]:
        """内容真正需要的最小宽高(含缝隙)。

        窗口比它矮时,卡片会被等比压缩、控件互相盖住 —— 外层套
        QScrollArea 的页面用它设 minimumHeight,让页面滚动而不是压扁。
        """
        cw, ch = self._extent()
        need_w = sum(self._col_min_px()) + self._gap * (len(self._cols) - 1)
        need_h = sum(self._row_min_px()) + self._gap * (len(self._rows) - 1)
        return (max(need_w, 0.0), max(need_h, 0.0))

    def _drag_col(self, index: int, dx: float, base: list[float]) -> None:
        c0, c1 = index - 1, index
        cw, _ = self._extent()
        if cw <= 1 or self._max is not None:
            return
        mins = self._col_min_px()
        if sum(mins) >= cw:
            return
        d = dx / cw
        lo = mins[c0] / cw - base[c0]
        hi = base[c1] - mins[c1] / cw
        if lo > hi:
            return
        d = max(lo, min(hi, d))
        if abs(d) < 1e-5:
            return
        cols = list(base)
        cols[c0] += d
        cols[c1] -= d
        self._cols = _norm(cols)
        self._relayout()
        self.layout_changed.emit()

    def _drag_row(self, index: int, dy: float, base: list[float]) -> None:
        r0, r1 = index - 1, index
        _, ch = self._extent()
        if ch <= 1 or self._max is not None:
            return
        mins = self._row_min_px()
        if sum(mins) >= ch:
            return
        d = dy / ch
        lo = mins[r0] / ch - base[r0]
        hi = base[r1] - mins[r1] / ch
        if lo > hi:
            return
        d = max(lo, min(hi, d))
        if abs(d) < 1e-5:
            return
        rows = list(base)
        rows[r0] += d
        rows[r1] -= d
        self._rows = _norm(rows)
        self._relayout()
        self.layout_changed.emit()

    # ---------------------------------------------------------- 最大化
    def toggle_max(self, item: _Item) -> None:
        if self._max is item:
            self._restore_max()
            return
        if self._max is not None:
            self._restore_max()
        self._max_state = (list(self._cols), list(self._rows),
                           [(it, it.w.isHidden()) for it in self._items])
        self._max = item
        for it in self._items:
            if it is not item:
                it.w.hide()
        self._relayout()

    def _restore_max(self) -> None:
        if self._max_state:
            cols, rows, vis = self._max_state
            self._cols, self._rows = list(cols), list(rows)
            for it, hidden in vis:
                it.w.setHidden(hidden)
        self._max = None
        self._max_state = None
        self._relayout()

    # ---------------------------------------------------------- 存盘
    def reset(self) -> None:
        """恢复默认比例并清掉存盘。"""
        if self._max is not None:
            self._restore_max()
        self._cols = list(self._default[0])
        self._rows = list(self._default[1])
        if self._key:
            try:
                lay = cfg().get("ui.layout") or {}
                if isinstance(lay, dict) and self._key in lay:
                    lay.pop(self._key, None)
                    cfg().set("ui.layout", lay, save=True)
            except Exception:
                pass
        self._relayout()
        self.layout_changed.emit()

    def _persist(self) -> None:
        if not self._key:
            return
        try:
            lay = cfg().get("ui.layout") or {}
            if not isinstance(lay, dict):
                lay = {}
            lay[self._key] = {
                "cols": [round(v, 4) for v in self._cols],
                "rows": [round(v, 4) for v in self._rows],
            }
            cfg().set("ui.layout", lay, save=True)
        except Exception:
            pass

    def _load(self) -> None:
        if not self._key:
            return
        # key 里带点(quote.right),不能走点路径查,直接取整个 dict
        try:
            lay = cfg().get("ui.layout")
        except Exception:
            return
        saved = lay.get(self._key) if isinstance(lay, dict) else None
        if not isinstance(saved, dict):
            return
        cols, rows = saved.get("cols"), saved.get("rows")
        if (isinstance(cols, list) and isinstance(rows, list)
                and len(cols) == len(self._cols) and len(rows) == len(self._rows)
                and all(isinstance(x, (int, float)) and 0 < x <= 1 for x in cols + rows)):
            self._cols = _norm([float(x) for x in cols])
            self._rows = _norm([float(x) for x in rows])

    # ---------------------------------------------------------- 提示
    def sizeHint(self):  # noqa: N802
        from PySide6.QtCore import QSize
        return QSize(720, 480)
