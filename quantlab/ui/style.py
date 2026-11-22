# -*- coding: utf-8 -*-
"""自绘控件细节:勾选框、单选点、下拉/微调箭头。

QSS 表达不了矢量图标 —— 只靠 QSS 时:
  · 勾选态是一个纯色方块,没有对勾;
  · 下拉箭头写成 border 三角形,Qt 会画成一根白短横;
  · 微调上下按钮会留下两个小方框。
所以这几处交给 QProxyStyle 画,其余仍由 QSS 负责。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QStyle, QStyleFactory, QProxyStyle

from . import theme as T

_disabled = "#48535f"
_indicator_size = 16          # 勾选框边长(device-independent px)


class GrayStyle(QProxyStyle):
    """Fusion 的细节替换:深色主题下的自绘勾选框与箭头。"""

    def __init__(self):
        super().__init__(QStyleFactory.create("Fusion"))

    # ---------------------------------------------------------------- 尺寸
    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric in (QStyle.PM_IndicatorWidth, QStyle.PM_IndicatorHeight):
            return _indicator_size
        if metric in (QStyle.PM_ExclusiveIndicatorWidth,
                      QStyle.PM_ExclusiveIndicatorHeight):
            return _indicator_size - 1
        if metric == QStyle.PM_ComboBoxFrameWidth:
            return 0
        return super().pixelMetric(metric, option, widget)

    # ---------------------------------------------------------------- 绘制
    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element in (QStyle.PE_IndicatorCheckBox, QStyle.PE_IndicatorItemViewItemCheck):
            self._draw_box(option, painter, radio=False)
            return
        if element == QStyle.PE_IndicatorRadioButton:
            self._draw_box(option, painter, radio=True)
            return
        if element in (QStyle.PE_IndicatorSpinUp, QStyle.PE_IndicatorArrowUp):
            self._draw_chevron(option, painter, "up")
            return
        if element in (QStyle.PE_IndicatorSpinDown, QStyle.PE_IndicatorArrowDown):
            self._draw_chevron(option, painter, "down")
            return
        if element == QStyle.PE_IndicatorArrowLeft:
            self._draw_chevron(option, painter, "left")
            return
        if element == QStyle.PE_IndicatorArrowRight:
            self._draw_chevron(option, painter, "right")
            return
        super().drawPrimitive(element, option, painter, widget)

    # ---------------------------------------------------------------- 实现
    def _draw_box(self, option, painter: QPainter, radio: bool) -> None:
        r = QRectF(option.rect)
        if r.width() < 6 or r.height() < 6:
            return
        side = min(r.width(), r.height())
        box = QRectF(r.center().x() - side / 2.0, r.center().y() - side / 2.0,
                     side, side).adjusted(0.7, 0.7, -0.7, -0.7)
        enabled = bool(option.state & QStyle.State_Enabled)
        checked = bool(option.state & QStyle.State_On)
        partial = bool(option.state & QStyle.State_NoChange)
        hover = bool(option.state & QStyle.State_MouseOver)

        if not enabled:
            fill, edge, mark = "#0f151d", "#1b2531", _disabled
        elif checked or partial:
            fill, edge, mark = T.ACCENT, T.ACCENT2, "#ffffff"
        else:
            fill = "#0d141d"
            edge = T.ACCENT if hover else "#39495c"
            mark = T.TEXT

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            if radio:
                painter.setPen(QPen(QColor(edge), 1.4))
                painter.setBrush(QColor("#0d141d"))
                painter.drawEllipse(box)
                if checked:
                    d = side * 0.26
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(T.ACCENT if enabled else _disabled))
                    painter.drawEllipse(box.adjusted(d, d, -d, -d))
            else:
                painter.setPen(QPen(QColor(edge), 1.4))
                painter.setBrush(QColor(fill))
                painter.drawRoundedRect(box, 4.2, 4.2)
                w = box.width()
                if checked:
                    path = QPainterPath()
                    path.moveTo(box.left() + w * 0.22, box.top() + w * 0.53)
                    path.lineTo(box.left() + w * 0.43, box.top() + w * 0.73)
                    path.lineTo(box.left() + w * 0.79, box.top() + w * 0.29)
                    painter.setPen(QPen(QColor(mark), max(1.5, w * 0.13),
                                        Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPath(path)
                elif partial:
                    painter.setPen(QPen(QColor(mark), max(1.5, w * 0.13),
                                        Qt.SolidLine, Qt.RoundCap))
                    painter.drawLine(QPointF(box.left() + w * 0.28, box.center().y()),
                                     QPointF(box.left() + w * 0.72, box.center().y()))
        finally:
            painter.restore()

    def _draw_chevron(self, option, painter: QPainter, direction: str) -> None:
        r = QRectF(option.rect)
        if r.width() < 4 or r.height() < 4:
            return
        enabled = bool(option.state & QStyle.State_Enabled)
        hover = bool(option.state & QStyle.State_MouseOver)
        if not enabled:
            col = QColor(_disabled)
        elif hover:
            col = QColor(T.TEXT)
        else:
            col = QColor(T.TEXT_DIM)

        wide = min(r.width() * 0.55, 9.0)
        tall = wide * 0.58
        cx, cy = r.center().x(), r.center().y()
        if direction == "down":
            pts = [QPointF(cx - wide / 2, cy - tall / 2),
                   QPointF(cx, cy + tall / 2),
                   QPointF(cx + wide / 2, cy - tall / 2)]
        elif direction == "up":
            pts = [QPointF(cx - wide / 2, cy + tall / 2),
                   QPointF(cx, cy - tall / 2),
                   QPointF(cx + wide / 2, cy + tall / 2)]
        elif direction == "left":
            pts = [QPointF(cx + tall / 2, cy - wide / 2),
                   QPointF(cx - tall / 2, cy),
                   QPointF(cx + tall / 2, cy + wide / 2)]
        else:
            pts = [QPointF(cx - tall / 2, cy - wide / 2),
                   QPointF(cx + tall / 2, cy),
                   QPointF(cx - tall / 2, cy + wide / 2)]

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(col, 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(QPolygonF(pts))
        finally:
            painter.restore()
