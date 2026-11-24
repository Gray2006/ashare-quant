# -*- coding: utf-8 -*-
"""公司评估页:左边评分/雷达/导航,右边是排版过的 HTML 报告。

报告正文由 ui/report_view.py 生成(带锚点),这里只负责:
  · 分数环 + 五维雷达 + 风险计数;
  · 左侧目录按钮,点一下 `scrollToAnchor` 跳到对应章节,不用一路往下滚;
  · 导出 Markdown(engine/company_eval.py 的 text 字段)。
"""
from __future__ import annotations

import math
import os
import re

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QPolygonF
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import company_eval
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, label
from .report_view import build_html
from .theme import ACCENT, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

GRADE_COLOR = {"稳健": UP, "良好": ACCENT, "中性": TEXT,
               "偏弱": WARN, "高风险": DOWN}

NAV = (("结论", "sec-1"), ("评分", "sec-2"), ("财务", "sec-3"),
       ("估值", "sec-4"), ("资金", "sec-5"), ("舆论", "sec-6"),
       ("产品", "sec-7"), ("股东", "sec-8"), ("风险", "sec-9"),
       ("口径", "sec-10"))


class _Ring(QWidget):
    """分数环:圆弧 + 中间总分/评级。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = None
        self._grade = ""
        self.setMinimumHeight(150)

    def set_score(self, total, grade: str):
        self._total, self._grade = total, grade or ""
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        side = min(w, h) - 18
        rect = QRectF((w - side) / 2, (h - side) / 2, side, side)
        p.setPen(QPen(QColor("#232a35"), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 0, 360 * 16)
        if self._total is None:
            return
        color = GRADE_COLOR.get(self._grade, ACCENT)
        ratio = max(0.0, min(1.0, (self._total or 0) / 100))
        p.setPen(QPen(QColor(color), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, -int(360 * 16 * ratio))
        p.setPen(QPen(QColor(TEXT)))
        f = QFont(self.font().family(), 22)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, h / 2 - 24, w, 34), Qt.AlignCenter, str(self._total))
        p.setFont(QFont(self.font().family(), 10))
        p.setPen(QPen(QColor(color)))
        p.drawText(QRectF(0, h / 2 + 10, w, 20), Qt.AlignCenter, self._grade)


class _Radar(QWidget):
    """五维雷达图(参考 Simply Wall St 的 snowflake:一眼看出哪一维强/弱)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[tuple[str, float]] = []
        self.setMinimumHeight(185)

    def set_items(self, items: list[tuple[str, float]]):
        self._items = items or []
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2 + 4
        r = min(w, h) / 2 - 26
        if r <= 10 or not self._items:
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "暂无评分")
            return
        n = len(self._items)
        for ring in (1.0, 0.75, 0.5, 0.25):
            p.setPen(QPen(QColor("#2a3039"), 1))
            p.setBrush(Qt.NoBrush)
            pts = [QPointF(cx + r * ring * math.cos(-math.pi / 2 + 2 * math.pi * i / n),
                           cy + r * ring * math.sin(-math.pi / 2 + 2 * math.pi * i / n))
                   for i in range(n)]
            p.drawPolygon(QPolygonF(pts))
        poly = []
        for i, (_name, ratio) in enumerate(self._items):
            a = -math.pi / 2 + 2 * math.pi * i / n
            rr = r * max(0.0, min(1.0, ratio))
            poly.append(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)))
        p.setPen(QPen(QColor(ACCENT), 1.6))
        p.setBrush(QColor(56, 189, 248, 48))
        p.drawPolygon(QPolygonF(poly))
        p.setFont(QFont(self.font().family(), max(7, self.font().pointSize() - 1)))
        for i, (name, ratio) in enumerate(self._items):
            a = -math.pi / 2 + 2 * math.pi * i / n
            x = cx + (r + 16) * math.cos(a)
            y = cy + (r + 16) * math.sin(a)
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(x - 32, y - 8, 64, 16), Qt.AlignCenter,
                       f"{name} {ratio * 100:.0f}")


class CompanyEvalPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._data: dict = {}
        self._markdown = ""
        self._build()
        QTimer.singleShot(1000, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("公司评估", hint="财务 · 资金流 · 舆论 · 产品 · 风险,规则打分")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("代码", dim=True))
        self.code = QComboBox()
        self.code.setEditable(True)
        self.code.setMinimumWidth(150)
        seen = []
        for c in list(cfg().watchlist or [])[:40]:
            c = str(c).strip()
            if c.isdigit() and len(c) == 6 and c not in seen:
                seen.append(c)
                self.code.addItem(c, c)
        if "600519" not in seen:
            self.code.addItem("600519", "600519")
        self.code.lineEdit().setPlaceholderText("输入 6 位代码,回车")
        self.code.lineEdit().returnPressed.connect(self.refresh)
        bar.addWidget(self.code)
        self.btn = QPushButton("生成报告")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        self.btn_save = QPushButton("导出 Markdown")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        bar.addWidget(self.btn_save)
        self.btn_pdf = QPushButton("导出 PDF")
        self.btn_pdf.setEnabled(False)
        self.btn_pdf.clicked.connect(self._pdf)
        bar.addWidget(self.btn_pdf)
        self.btn_copy = QPushButton("复制要点")
        self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_points)
        bar.addWidget(self.btn_copy)
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        top.body().addLayout(bar)
        root.addWidget(top)

        board = Board("company_eval", cols=[0.29, 0.71], rows=[1.0])

        left = Card("评分", hint="规则打分,不是收益预测")
        self.ring = _Ring()
        left.body().addWidget(self.ring)
        self.radar = _Radar()
        left.body().addWidget(self.radar)
        self.risk_lbl = QLabel("")
        self.risk_lbl.setWordWrap(True)
        self.risk_lbl.setStyleSheet(f"color:{TEXT_MUT};font-size:12px;")
        left.body().addWidget(self.risk_lbl)
        left.body().addWidget(label("目录", dim=True))
        nav = QGridLayout()
        nav.setSpacing(5)
        self.nav_buttons: list[QPushButton] = []
        for i, (text, anchor) in enumerate(NAV):
            b = QPushButton(text)
            b.setObjectName("Flat")
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(28)
            b.setStyleSheet(
                f"QPushButton{{color:{TEXT_DIM};background:#18222e;"
                f"border:1px solid #243040;border-radius:6px;"
                f"padding:5px 4px;font-size:12px;}}"
                f"QPushButton:hover{{color:{TEXT};background:#1e2a38;"
                f"border-color:{ACCENT};}}")
            b.clicked.connect(lambda _=False, a=anchor: self.report.scrollToAnchor(a))
            self.nav_buttons.append(b)
            nav.addWidget(b, i // 2, i % 2)
        left.body().addLayout(nav)
        left.body().addStretch(1)
        board.add(left, 0, 0)

        right = Card("评估报告", hint="按章节整理 · 可导出")
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(True)
        self.report.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.report.setStyleSheet(
            f"QTextBrowser{{background:transparent;color:{TEXT};border:none;"
            f"padding:2px 10px 2px 2px;}}")
        right.add(self.report, 1)
        board.add(right, 0, 1)

        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def _picked(self) -> str:
        raw = (self.code.currentText() or "").strip()
        m = re.search(r"\d{6}", raw)
        return m.group(0) if m else ""

    def set_code(self, code: str):
        """从公司分析页跳过来时用:填代码并立刻生成。"""
        code = (code or "").strip()
        if not code:
            return
        idx = self.code.findData(code)
        if idx >= 0:
            self.code.setCurrentIndex(idx)
        else:
            self.code.setEditText(code)
        self.refresh()

    def refresh(self):
        if self._tasks:                    # 已有一次生成在跑,别叠第二个
            return
        code = self._picked()
        if not code:
            self.hint.setText("先填 6 位股票代码")
            return
        self.hint.setText(f"生成中:{code} …(要拉新闻和资金流,约 2~8 秒)")
        self.status.emit(f"公司评估:{code}")
        self.btn.setEnabled(False)
        self.btn_save.setEnabled(False)
        job = TaskRunner(lambda: company_eval.evaluate(code))
        job.succeeded.connect(self._ok)
        job.failed.connect(self._fail)
        job.finished.connect(lambda: self._done(job))
        self._tasks.append(job)
        job.start()

    def _done(self, job):
        if job in self._tasks:
            self._tasks.remove(job)
        self.btn.setEnabled(True)

    def _fail(self, msg: str):
        self.hint.setText(f"生成失败:{msg.splitlines()[0][:90]}")
        self.report.setPlainText("")
        self.ring.set_score(None, "")
        self.radar.set_items([])
        self.risk_lbl.setText("")
        self.btn_save.setEnabled(False)
        self.btn_pdf.setEnabled(False)
        self.btn_copy.setEnabled(False)

    # ------------------------------------------------------------------ 展示
    def _ok(self, data: dict):
        d = data if isinstance(data, dict) else {}
        if not d.get("ok"):
            self._fail(str(d.get("error") or "没有数据"))
            return
        self._data = d
        sc = d.get("scores") or {}
        self.hint.setText(f"{d.get('name')}({d.get('code')}) · 生成于 {d.get('ts')}")
        self.ring.set_score(sc.get("total"), sc.get("grade") or "")
        self.radar.set_items([(it["name"], it["score"] / max(1, it["max"]))
                              for it in sc.get("items") or []])
        risks = d.get("risks") or []
        counts = {lv: sum(1 for r in risks if r["level"] == lv)
                  for lv in ("高", "中", "低")}
        self.risk_lbl.setText(
            f"风险信号:高 {counts['高']} · 中 {counts['中']} · 低 {counts['低']}")
        self.risk_lbl.setStyleSheet(
            f"color:{DOWN if counts['高'] else WARN if counts['中'] else TEXT_MUT};"
            f"font-size:12px;")

        self.report.setHtml(build_html(d))
        self._markdown = d.get("text") or ""
        self.btn_save.setEnabled(bool(self._markdown))
        self.btn_copy.setEnabled(bool(d.get("points")))
        self.btn_pdf.setEnabled(bool(self.report.toPlainText()))

    def _copy_points(self):
        d = self._data or {}
        sc = d.get("scores") or {}
        head = f"{d.get('name')}({d.get('code')}) 综合 {sc.get('total')}/100 {sc.get('grade')}"
        body = "\n".join(f"- {p}" for p in d.get("points") or [])
        QGuiApplication.clipboard().setText(f"{head}\n{body}")
        self.hint.setText("投资要点已复制到剪贴板")

    def _pdf(self):
        if not self.report.toPlainText():
            return
        d = self._data or {}
        name = f"{d.get('code', '')}_{d.get('name', '')}_评估报告.pdf"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PDF", os.path.join(os.path.expanduser("~"), name),
            "PDF (*.pdf)")
        if not path:
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        doc = self.report.document().clone()
        page = printer.pageRect(QPrinter.Point)
        doc.setPageSize(QSizeF(page.width(), page.height()))
        try:
            doc.print_(printer)
        except Exception as e:                       # noqa: BLE001
            self.hint.setText(f"导出失败:{e}")
            return
        self.hint.setText(f"已导出:{path}")

    def _save(self):
        if not self._markdown:
            return
        d = self._data or {}
        name = f"{d.get('code', '')}_{d.get('name', '')}_评估报告.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出评估报告", os.path.join(os.path.expanduser("~"), name),
            "Markdown (*.md)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._markdown)
        except OSError as e:
            self.hint.setText(f"导出失败:{e}")
            return
        self.hint.setText(f"已导出:{path}")

    # ------------------------------------------------------------------ 生命周期
    def stop(self):
        for t in list(self._tasks):
            t.wait(6000)
        self._tasks.clear()
