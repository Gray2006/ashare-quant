# -*- coding: utf-8 -*-
"""公司分析页:一只票的基本面面板,布局参考同花顺 F10 / OpenBB 的个股研究页。

自上而下:
  · 报价条:名称、代码、行业、现价与涨跌幅、市值/PE/PB,亏损或 ST 直接标出来
  · 投资要点:行业地位、估值分位、成长、盈利、风险,一行五个事实,不写评级
  · 最新指标:ROE、毛利率、负债率、营收同比、净利同比
  · 概况:基本信息 + 上市要素 + 主营范围
  · 财务趋势:最近 8 期营收/净利同比与 ROE,配指标表
  · 主营构成:切报告期、切行业/产品/地区,收入占比有图形条
  · 估值与同业:PE/PB 在行业里的分位条 + 同业对比表
  · 股东与事件:股东 / 分红 / 公告 / 解禁 四个页签

取数与口径在 engine/company.py;这页只负责展示和「哪块没取到」的诚实提示。
"""
from __future__ import annotations

import datetime as dt
import re

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QPushButton, QScrollArea,
                               QTabWidget, QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import company
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import ACCENT, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

YI = 1e8
WAN = 1e4
TONE = {"good": UP, "bad": DOWN, "flat": TEXT_DIM}
SEG_COLORS = ("#38bdf8", "#a78bfa", "#f0b90b", "#2ebd85", "#f6465d", "#8b97a8")


def _f(v, digits: int = 2, plus: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}" if plus else f"{v:.{digits}f}"


def _yi(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v / YI:,.{digits}f}"


def _elide(s: str, n: int = 38) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _short_period(p: str) -> str:
    m = re.match(r"(\d{4})(一季报|中报|三季报|年报)", p or "")
    if not m:
        return (p or "")[-4:]
    tag = {"一季报": "Q1", "中报": "H1", "三季报": "Q3", "年报": "Y"}[m.group(2)]
    return f"{m.group(1)[2:]}{tag}"


def _period_end(period: str) -> dt.date | None:
    """'2026三季报' → 2026-09-30。解析不了返回 None。"""
    m = re.match(r"(\d{4})(一季报|中报|三季报|年报)", period or "")
    if not m:
        return None
    day = {"一季报": (3, 31), "中报": (6, 30), "三季报": (9, 30),
           "年报": (12, 31)}[m.group(2)]
    return dt.date(int(m.group(1)), *day)


class _PeriodDialog(QDialog):
    """点财务图某一期弹出的详情:该期指标 + 附近公告(双击打开原文)。"""

    def __init__(self, row: dict, announcements: list[dict], parent=None):
        super().__init__(parent)
        period = row.get("period") or ""
        self.setWindowTitle(f"{period} 详情")
        self.resize(560, 420)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(5)
        cells = [
            ("营收(亿)", _yi(row.get("revenue"))),
            ("营收同比", f'{_f(row.get("revenue_yoy"), 1, True)}%'),
            ("归母净利(亿)", _yi(row.get("net_profit"))),
            ("净利同比", f'{_f(row.get("profit_yoy"), 1, True)}%'),
            ("ROE", f'{_f(row.get("roe"), 1)}%'),
            ("毛利率", f'{_f(row.get("gross_margin"), 1)}%'),
            ("净利率", f'{_f(row.get("net_margin"), 1)}%'),
            ("资产负债率", f'{_f(row.get("debt_ratio"), 1)}%'),
            ("经营现金流/净利", _f(row.get("ocf_to_profit"), 2)),
        ]
        for i, (k, v) in enumerate(cells):
            lb = label(k, dim=True)
            lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = QLabel(v)
            val.setStyleSheet(f"color:{TEXT};font-weight:600;")
            grid.addWidget(lb, i // 3, (i % 3) * 2)
            grid.addWidget(val, i // 3, (i % 3) * 2 + 1)
        lay.addLayout(grid)

        end = _period_end(period)
        near = []
        if end:
            for a in announcements or []:
                try:
                    d = dt.date.fromisoformat(str(a.get("date") or "")[:10])
                except ValueError:
                    continue
                if 0 <= (d - end).days <= 80:
                    near.append(a)
        lay.addWidget(label(
            f"该期公告({len(near)} 条,双击打开原文)" if near else
            "该期附近没有取到公告", dim=True))
        self._anns = near
        self.list = QListWidget()
        for a in near:
            self.list.addItem(f"{a.get('date') or ''}  {a.get('title') or ''}")
        self.list.itemDoubleClicked.connect(self._open)
        lay.addWidget(self.list, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b = QPushButton("关闭")
        b.clicked.connect(self.accept)
        btns.addWidget(b)
        lay.addLayout(btns)

    def _open(self, item):
        row = self.list.row(item)
        if 0 <= row < len(self._anns):
            url = self._anns[row].get("url")
            if url:
                QDesktopServices.openUrl(QUrl(url))


class _TrendChart(QWidget):
    """上:营收/净利同比柱;下:ROE 折线。x 轴共享报告期,鼠标悬停出十字线和数值。"""

    period_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.p1 = self.glw.addPlot(row=0, col=0)
        self.p2 = self.glw.addPlot(row=1, col=0)
        self.p2.setXLink(self.p1)
        for p in (self.p1, self.p2):
            p.showGrid(x=False, y=True, alpha=0.12)
            p.getAxis("left").setTextPen(TEXT_DIM)
            p.getAxis("bottom").setTextPen(TEXT_DIM)
            p.hideAxis("bottom")
            p.hideButtons()
            p.setContentsMargins(0, 0, 0, 0)
        self.p1.setLabel("left", "同比 %", color=TEXT_MUT)
        self.p2.setLabel("left", "ROE %", color=TEXT_MUT)
        self.p2.showAxis("bottom")
        self.glw.ci.layout.setRowStretchFactor(0, 3)
        self.glw.ci.layout.setRowStretchFactor(1, 2)
        self._legend = self.p1.addLegend(offset=(-6, 2), labelTextColor=TEXT_DIM)
        self._trend: list[dict] = []
        self._vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#5b6776", width=1, style=Qt.DashLine))
        self._tip = pg.TextItem("", color=TEXT, anchor=(0.5, 1.0),
                                fill=pg.mkBrush(15, 23, 32, 235))
        self._proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved,
                                     rateLimit=30, slot=self._on_move)
        self.glw.scene().sigMouseClicked.connect(self._on_click)

    def _overlay(self):
        """p1.clear() 会把常驻的零线/十字线/提示框一起清掉,画完要补回来。"""
        self.p1.addLine(y=0, pen=pg.mkPen("#3d4856", width=1))
        self.p1.addItem(self._vline, ignoreBounds=True)
        self.p1.addItem(self._tip, ignoreBounds=True)
        self._vline.setVisible(False)
        self._tip.setVisible(False)

    def _on_move(self, evt):
        if not self._trend:
            return
        pos = evt[0]
        if not self.p1.sceneBoundingRect().contains(pos):
            self._vline.setVisible(False)
            self._tip.setVisible(False)
            return
        pt = self.p1.vb.mapSceneToView(pos)
        idx = int(round(pt.x()))
        if idx < 0 or idx >= len(self._trend):
            self._vline.setVisible(False)
            self._tip.setVisible(False)
            return
        r = self._trend[idx]
        self._vline.setPos(idx)
        self._vline.setVisible(True)
        self._tip.setHtml(
            f'<div style="color:{TEXT};font-size:11px">'
            f'{r.get("period") or ""}<br>'
            f'营收 {_f(r.get("revenue_yoy"), 1, True)}% · '
            f'净利 {_f(r.get("profit_yoy"), 1, True)}%<br>'
            f'ROE {_f(r.get("roe"), 1)}%</div>')
        top = self.p1.vb.viewRange()[1][1]
        self._tip.setPos(idx, top)
        self._tip.setVisible(True)

    def _on_click(self, evt):
        if not self._trend:
            return
        if not self.p1.sceneBoundingRect().contains(evt.scenePos()):
            return
        pt = self.p1.vb.mapSceneToView(evt.scenePos())
        idx = int(round(pt.x()))
        if 0 <= idx < len(self._trend):
            self.period_clicked.emit(idx)

    def plot(self, trend: list[dict]):
        self._trend = trend or []
        self.p1.clear()
        self.p2.clear()
        self._legend.clear()
        self._overlay()
        if not trend:
            return
        x = np.arange(len(trend))
        rev = np.array([r.get("revenue_yoy") if r.get("revenue_yoy") is not None else np.nan
                        for r in trend], dtype=float)
        prof = np.array([r.get("profit_yoy") if r.get("profit_yoy") is not None else np.nan
                         for r in trend], dtype=float)
        bar_rev = pg.BarGraphItem(x=x - 0.19, height=rev, width=0.34,
                                  brush=pg.mkBrush(ACCENT), pen=None)
        bar_prof = pg.BarGraphItem(x=x + 0.19, height=prof, width=0.34,
                                   brush=pg.mkBrush("#a78bfa"), pen=None)
        self.p1.addItem(bar_rev)
        self.p1.addItem(bar_prof)
        self._legend.addItem(bar_rev, "营收同比")
        self._legend.addItem(bar_prof, "净利同比")
        roe = np.array([r.get("roe") if r.get("roe") is not None else np.nan
                        for r in trend], dtype=float)
        self.p2.plot(x, roe, pen=pg.mkPen(WARN, width=1.7),
                     symbol="o", symbolSize=5, symbolBrush=WARN,
                     symbolPen=None)
        ticks = [(i, _short_period(r.get("period") or "")) for i, r in enumerate(trend)]
        self.p2.getAxis("bottom").setTicks([ticks])
        self.p1.setXRange(-0.6, len(trend) - 0.4, padding=0)


class _ShareBar(QWidget):
    """收入占比条:一条横条按占比切段,下面一行「项目 占比%」。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self.setMinimumHeight(44)

    def set_rows(self, rows: list[dict]):
        self._rows = rows or []
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor("transparent"))
        if not self._rows:
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignLeft | Qt.AlignVCenter,
                       "暂无构成数据")
            return
        bar_h = 16
        y = 2
        p.setPen(Qt.NoPen)
        x = 0.0
        total = sum(max(0.0, r.get("ratio") or 0) for r in self._rows) or 1.0
        for i, r in enumerate(self._rows):
            ratio = max(0.0, r.get("ratio") or 0) / total
            seg = ratio * w
            p.setBrush(QColor(SEG_COLORS[i % len(SEG_COLORS)]))
            p.drawRect(QRectF(x, y, max(1.0, seg), bar_h))
            x += seg
        p.setFont(QFont(self.font().family(), max(7, self.font().pointSize() - 1)))
        legend = "  ".join(
            f"{_elide(str(r.get('name') or ''), 8)} {((r.get('ratio') or 0) * 100):.0f}%"
            for r in self._rows[:5])
        p.setPen(QPen(QColor(TEXT_MUT)))
        p.drawText(QRectF(0, bar_h + 6, w, h - bar_h - 6),
                   Qt.AlignLeft | Qt.AlignVCenter, legend)


class _PercentBar(QWidget):
    """行业分位条:0~100% 轨道 + 本股位置 + 行业中位标记。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pct: float | None = None
        self._median: float | None = None
        self._left = ""
        self._right = ""
        self.setMinimumHeight(30)
        self.setMaximumHeight(32)

    def set_value(self, pct: float | None, left: str, right: str,
                  median: float | None = None):
        self._pct, self._left, self._right, self._median = pct, left, right, median
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setFont(QFont(self.font().family(), max(7, self.font().pointSize() - 1)))
        p.setPen(QPen(QColor(TEXT_DIM)))
        text_h = h - 10
        p.drawText(QRectF(0, 0, w, text_h), Qt.AlignLeft | Qt.AlignVCenter, self._left)
        p.drawText(QRectF(0, 0, w, text_h), Qt.AlignRight | Qt.AlignVCenter, self._right)
        track = QRectF(4, h - 7, w - 8, 4)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#232a35"))
        p.drawRoundedRect(track, 2, 2)
        if self._pct is not None:
            x = track.left() + max(0.0, min(1.0, self._pct)) * track.width()
            p.setBrush(QColor(ACCENT))
            p.drawEllipse(QRectF(x - 3.5, track.center().y() - 3.5, 7, 7))
        if self._median is not None:
            x = track.left() + max(0.0, min(1.0, self._median)) * track.width()
            p.setPen(QPen(QColor(WARN), 1.4))
            p.drawLine(x, track.top() - 3, x, track.bottom() + 3)


class CompanyPage(QWidget):
    status = Signal(str)
    open_eval = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._data: dict = {}
        self._build()
        QTimer.singleShot(900, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("公司分析", hint="F10 资料 · 主营构成 · 财务趋势 · 估值同业 · 股东事件")
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
        self.btn = QPushButton("分析")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        self.btn_eval = QPushButton("评估报告")
        self.btn_eval.clicked.connect(self._to_eval)
        bar.addWidget(self.btn_eval)
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        top.body().addLayout(bar)

        # 报价条
        quote = QHBoxLayout()
        quote.setSpacing(10)
        self.head_name = QLabel("—")
        self.head_name.setStyleSheet(
            f"color:{TEXT};font-size:20px;font-weight:700;")
        quote.addWidget(self.head_name)
        self.head_meta = label("", dim=True)
        quote.addWidget(self.head_meta)
        quote.addStretch(1)
        self.head_price = QLabel("—")
        self.head_price.setStyleSheet(f"color:{TEXT};font-size:20px;font-weight:700;")
        quote.addWidget(self.head_price)
        self.head_pct = QLabel("")
        self.head_pct.setStyleSheet(f"color:{TEXT_MUT};font-size:14px;")
        quote.addWidget(self.head_pct)
        self.head_facts = label("", dim=True)
        quote.addWidget(self.head_facts)
        top.body().addLayout(quote)

        # 投资要点
        self.highlights_row = QHBoxLayout()
        self.highlights_row.setSpacing(14)
        top.body().addLayout(self.highlights_row)

        # 最新指标
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.s_roe = StatCard("ROE", "-", "最新报告期")
        self.s_gm = StatCard("毛利率", "-", "产品溢价")
        self.s_debt = StatCard("资产负债率", "-", "偿债压力")
        self.s_rev = StatCard("营收同比", "-", "最新报告期")
        self.s_prof = StatCard("净利同比", "-", "最新报告期")
        for w in (self.s_roe, self.s_gm, self.s_debt, self.s_rev, self.s_prof):
            stats.addWidget(w, 1)
        top.body().addLayout(stats)
        root.addWidget(top)

        board = Board("company", cols=[0.34, 0.33, 0.33], rows=[0.56, 0.44])

        # 概况
        prof_card = Card("公司概况", hint="基本信息 · 上市要素")
        self.prof_host = QWidget()
        self.prof_grid = QGridLayout(self.prof_host)
        self.prof_grid.setContentsMargins(0, 0, 0, 0)
        self.prof_grid.setHorizontalSpacing(10)
        self.prof_grid.setVerticalSpacing(5)
        self.prof_grid.setColumnStretch(1, 1)
        self.prof_grid.setColumnStretch(3, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}"
                             "QScrollArea>QWidget>QWidget{background:transparent;}")
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(self.prof_host)
        prof_card.add(scroll, 1)
        board.add(prof_card, 0, 0)

        # 财务趋势:图 / 表分两个页签,各自吃满卡片高度,避免图被压扁
        fin_card = Card("财务趋势", hint="最近 8 期 · 同比与 ROE")
        self.fin_card = fin_card
        self.fin_tabs = QTabWidget()
        self.fin_tabs.setDocumentMode(True)
        self.chart = _TrendChart()
        self.chart.period_clicked.connect(self._show_period)
        self.fin_tabs.addTab(self.chart, "趋势图")
        self.fin_tbl = table(
            ["报告期", "营收(亿)", "同比%", "净利(亿)", "同比%", "ROE%", "毛利率%"],
            widths=[86, 74, 56, 74, 56, 50, 58], stretch_col=0)
        self.fin_tabs.addTab(self.fin_tbl, "指标表")
        self.fin_range = QComboBox()
        for n in (4, 8, 12):
            self.fin_range.addItem(f"近 {n} 期", n)
        self.fin_range.setCurrentIndex(1)
        self.fin_range.setMinimumWidth(78)
        self.fin_range.currentIndexChanged.connect(lambda *_: self._render_finance())
        self.fin_tabs.setCornerWidget(self.fin_range, Qt.TopRightCorner)
        fin_card.add(self.fin_tabs, 1)
        self.fin_notes = label("", dim=True)
        self.fin_notes.setVisible(False)     # 旗标改放卡片标题旁的 hint,省一行高度
        fin_card.body().addWidget(self.fin_notes)
        board.add(fin_card, 0, 1, colspan=2)

        # 主营构成:期次 + 分类 + 占比条 + 表
        mix_card = Card("主营构成", hint="比收入、比毛利率")
        mbar = QHBoxLayout()
        mbar.setSpacing(6)
        mbar.addWidget(label("报告期", dim=True))
        self.mix_period = QComboBox()
        self.mix_period.setMinimumWidth(96)
        self.mix_period.currentIndexChanged.connect(lambda *_: self._render_mix())
        mbar.addWidget(self.mix_period)
        self.mix_kind = QComboBox()
        self.mix_kind.setMinimumWidth(66)
        self.mix_kind.currentIndexChanged.connect(lambda *_: self._render_mix())
        mbar.addWidget(self.mix_kind)
        mbar.addStretch(1)
        mix_card.body().addLayout(mbar)
        self.mix_bar = _ShareBar()
        mix_card.body().addWidget(self.mix_bar)
        self.mix_tbl = table(["项目", "收入(亿)", "占比%", "毛利率%"],
                             widths=[120, 82, 60, 68], stretch_col=0)
        mix_card.add(self.mix_tbl, 1)
        self.mix_hint = label("", dim=True)
        self.mix_hint.setWordWrap(True)
        mix_card.body().addWidget(self.mix_hint)
        board.add(mix_card, 1, 0)

        # 估值与同业:一行估值摘要 + 分位条 + 同业表
        val_card = Card("估值与同业", hint="最新收盘 · 同二级行业")
        self.val_line = label("", dim=True)
        val_card.body().addWidget(self.val_line)
        self.pe_bar = _PercentBar()
        self.pb_bar = _PercentBar()
        val_card.body().addWidget(self.pe_bar)
        val_card.body().addWidget(self.pb_bar)
        self.val_hint = label("", dim=True)
        self.val_hint.setWordWrap(True)
        val_card.body().addWidget(self.val_hint)
        sort_row = QHBoxLayout()
        sort_row.addWidget(label("排序", dim=True))
        self.peer_sort = QComboBox()
        for text, key, asc in (("市值 ↓", "cap", False), ("PE ↑", "pe", True),
                               ("PB ↑", "pb", True), ("PE ↓", "pe", False)):
            self.peer_sort.addItem(text, (key, asc))
        self.peer_sort.currentIndexChanged.connect(
            lambda *_: self._fill_valuation(self._data or {}))
        sort_row.addWidget(self.peer_sort)
        sort_row.addStretch(1)
        val_card.body().addLayout(sort_row)
        self.peer_tbl = table(["名称", "PE", "PB", "市值(亿)"],
                              widths=[92, 58, 54, 76], stretch_col=0)
        val_card.add(self.peer_tbl, 1)
        board.add(val_card, 1, 1)

        # 股东与事件:页签
        hold_card = Card("股东与事件", hint="股东 · 分红 · 公告 · 解禁")
        self.hold_tabs = QTabWidget()
        self.hold_tabs.setDocumentMode(True)
        self.hold_tbl = table(["股东", "占比%", "变动(万股)"],
                              widths=[150, 62, 84], stretch_col=0)
        self.hold_tabs.addTab(self.hold_tbl, "股东")
        self.div_tbl = table(["公告日", "方案", "进度"],
                             widths=[84, 150, 66], stretch_col=1)
        self.hold_tabs.addTab(self.div_tbl, "分红")
        self.ann_tbl = table(["日期", "公告"], widths=[84, 220], stretch_col=1)
        self.hold_tabs.addTab(self.ann_tbl, "公告")
        self.unlock_tbl = table(["日期", "解禁(亿股)", "占比%", "类型"],
                                widths=[84, 78, 56, 96], stretch_col=3)
        self.hold_tabs.addTab(self.unlock_tbl, "解禁")
        hold_card.add(self.hold_tabs, 1)
        self.hold_hint = label("", dim=True)
        self.hold_hint.setWordWrap(False)
        hold_card.body().addWidget(self.hold_hint)
        board.add(hold_card, 1, 2)

        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def _picked(self) -> str:
        raw = (self.code.currentText() or "").strip()
        m = re.search(r"\d{6}", raw)
        return m.group(0) if m else ""

    def _to_eval(self):
        code = self._picked()
        if not code:
            self.hint.setText("先填 6 位股票代码,再生成评估报告")
            return
        self.open_eval.emit(code)

    def _show_period(self, idx: int):
        trend = self.chart._trend
        if not (0 <= idx < len(trend)):
            return
        dlg = _PeriodDialog(trend[idx],
                            (self._data or {}).get("announcements") or [], self)
        dlg.exec()

    def refresh(self):
        code = self._picked()
        if not code:
            self.hint.setText("先填 6 位股票代码")
            return
        self.hint.setText(f"取数中:{code} …")
        self.status.emit(f"公司分析:{code}")
        self.btn.setEnabled(False)
        job = TaskRunner(lambda: company.analyze(code))
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
        self.hint.setText(f"取数失败:{msg.splitlines()[0][:90]}")
        for t in (self.fin_tbl, self.mix_tbl, self.peer_tbl, self.hold_tbl,
                  self.div_tbl, self.ann_tbl, self.unlock_tbl):
            t.setRowCount(0)
        self.mix_period.clear()
        self.mix_kind.clear()
        self.fin_notes.setText("")
        self.mix_hint.setText("")
        self.val_hint.setText("")
        self.hold_hint.setText("")
        self.mix_bar.set_rows([])
        self.pe_bar.set_value(None, "PE", "")
        self.pb_bar.set_value(None, "PB", "")
        self.head_name.setText("—")
        self.head_price.setText("—")
        self.head_pct.setText("")
        self.head_meta.setText("")
        self.head_facts.setText("")
        while self.highlights_row.count():
            it = self.highlights_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._reset_profile()

    # ------------------------------------------------------------------ 展示
    def _reset_profile(self):
        while self.prof_grid.count():
            it = self.prof_grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    def _ok(self, data: dict):
        self._data = data if isinstance(data, dict) else {}
        d = self._data
        if not d.get("ok"):
            self._fail(str(d.get("error") or "没有数据"))
            return
        err = d.get("errors") or {}
        self.hint.setText(f"{d.get('name') or d['code']}({d['code']})"
                          f" · {'、'.join(err) + ' 未取到' if err else '更新 ' + str(d.get('bs'))}")
        self._fill_header(d)
        self._fill_highlights(d)
        self._fill_stats(d)
        self._fill_profile(d)
        self._fill_mix(d)
        self._fill_finance(d)
        self._fill_valuation(d)
        self._fill_holders(d)

    def _fill_header(self, d: dict):
        p = d.get("profile") or {}
        v = d.get("valuation") or {}
        name = d.get("name") or d.get("code") or "—"
        self.head_name.setText(name)
        meta = [d.get("code") or ""]
        if p.get("industry"):
            meta.append(p["industry"])
        if p.get("industry_csrc"):
            meta.append(p["industry_csrc"])
        if p.get("list_date"):
            meta.append("上市 " + str(p["list_date"])[:10])
        self.head_meta.setText(" · ".join(x for x in meta if x))

        price = v.get("price")
        pct = v.get("pct")
        self.head_price.setText(_f(price, 2))
        self.head_price.setStyleSheet(
            f"color:{pnl_color(pct)};font-size:20px;font-weight:700;")
        self.head_pct.setText(f"{pct:+.2f}%" if pct is not None else "")
        self.head_pct.setStyleSheet(
            f"color:{pnl_color(pct)};font-size:14px;")
        facts = []
        if v.get("total_cap") is not None:
            facts.append(f"市值 {_yi(v['total_cap'], 0)} 亿")
        if v.get("pe_ttm") is not None:
            facts.append(f"PE {_f(v['pe_ttm'], 1)}")
        if v.get("pb") is not None:
            facts.append(f"PB {_f(v['pb'], 2)}")
        self.head_facts.setText(" · ".join(facts))

    def _fill_highlights(self, d: dict):
        while self.highlights_row.count():
            it = self.highlights_row.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        for h in d.get("highlights") or []:
            chip = QLabel(f"{h['tag']}  {h['text']}")
            chip.setStyleSheet(
                f"color:{TONE.get(h.get('tone'), TEXT_DIM)};font-size:12px;")
            chip.setToolTip(h["text"])
            self.highlights_row.addWidget(chip)
        self.highlights_row.addStretch(1)

    def _fill_stats(self, d: dict):
        latest = d.get("latest") or {}
        roe = latest.get("roe")
        gm = latest.get("gross_margin")
        debt = latest.get("debt_ratio")
        rev = latest.get("revenue_yoy")
        prof = latest.get("profit_yoy")
        self.s_roe.set(_f(roe, 1) + ("%" if roe is not None else ""),
                       pnl_color(roe))
        self.s_gm.set(_f(gm, 1) + ("%" if gm is not None else ""))
        self.s_debt.set(_f(debt, 1) + ("%" if debt is not None else ""),
                        DOWN if (debt or 0) >= 70 else TEXT)
        self.s_rev.set(_f(rev, 1, True) + ("%" if rev is not None else ""),
                       pnl_color(rev))
        self.s_prof.set(_f(prof, 1, True) + ("%" if prof is not None else ""),
                        pnl_color(prof))

    def _fill_profile(self, d: dict):
        p = d.get("profile") or {}
        self._reset_profile()
        if not any(p.get(k) for k in ("full_name", "industry", "chairman")):
            self.prof_grid.addWidget(
                label("东财 F10 没有该股资料(北交所/新股常见)", dim=True), 0, 0, 1, 4)
            return

        def section(r: int, text: str) -> int:
            lb = QLabel(text)
            lb.setStyleSheet(f"color:{ACCENT};font-size:12px;font-weight:600;")
            self.prof_grid.addWidget(lb, r, 0, 1, 4)
            return r + 1

        def pair(r: int, k1, v1, k2=None, v2=None) -> int:
            if v1 in (None, "", 0) and v2 in (None, "", 0):
                return r
            for col, (k, v) in enumerate(((k1, v1), (k2, v2))):
                if v in (None, "", 0):
                    continue
                lb = label(k, dim=True)
                lb.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                val = QLabel(str(v))
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self.prof_grid.addWidget(lb, r, col * 2)
                self.prof_grid.addWidget(val, r, col * 2 + 1)
            return r + 1

        def wide(r: int, k: str, v) -> int:
            if v in (None, "", 0):
                return r
            lb = label(k, dim=True)
            lb.setAlignment(Qt.AlignRight | Qt.AlignTop)
            val = QLabel(str(v))
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.prof_grid.addWidget(lb, r, 0)
            self.prof_grid.addWidget(val, r, 1, 1, 3)
            return r + 1

        r = section(0, "基本信息")
        r = wide(r, "全称", p.get("full_name"))
        r = wide(r, "曾用名", p.get("former_name"))
        r = pair(r, "东财行业", p.get("industry"),
                 "证监会行业", p.get("industry_csrc"))
        reg = (f"{p['reg_capital']:,.0f} 万"
               if isinstance(p.get("reg_capital"), (int, float)) else p.get("reg_capital"))
        r = pair(r, "董事长", p.get("chairman"), "总经理", p.get("president"))
        r = pair(r, "董秘", p.get("secretary"), "员工", p.get("employees"))
        r = pair(r, "注册资本", reg)
        r = section(r, "上市要素")
        r = pair(r, "上市日期", str(p.get("list_date") or "")[:10],
                 "成立日期", str(p.get("found_date") or "")[:10])
        r = pair(r, "发行价", p.get("issue_price"),
                 "注册地址", p.get("address"))
        r = wide(r, "官网", p.get("website"))
        scope = (d.get("business") or {}).get("scope") or p.get("business") or ""
        if scope:
            lb = label("主营范围", dim=True)
            lb.setAlignment(Qt.AlignRight | Qt.AlignTop)
            holder = QWidget()
            hv = QVBoxLayout(holder)
            hv.setContentsMargins(0, 0, 0, 0)
            hv.setSpacing(2)
            text = QLabel(_elide(scope, 84))
            text.setWordWrap(True)
            text.setStyleSheet(f"color:{TEXT_DIM};")
            text.setToolTip(scope)
            text.setMaximumHeight(40)
            btn = QPushButton("展开")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton{{color:{ACCENT};background:transparent;border:none;"
                f"padding:0;font-size:11px;text-align:left;}}")
            btn.setFixedWidth(40)

            def _toggle(_=False, t=text, b=btn, full=scope):
                opened = bool(b.property("open"))
                t.setMaximumHeight(16777215 if not opened else 40)
                t.setText(full if not opened else _elide(full, 84))
                b.setText("收起" if not opened else "展开")
                b.setProperty("open", not opened)
                b.style().unpolish(b)
                b.style().polish(b)

            btn.clicked.connect(_toggle)
            hv.addWidget(text)
            hv.addWidget(btn)
            self.prof_grid.addWidget(lb, r, 0)
            self.prof_grid.addWidget(holder, r, 1, 1, 3)
            r += 1
        self.prof_grid.setRowStretch(r, 1)

    def _fill_mix(self, d: dict):
        biz = d.get("business") or {}
        periods = biz.get("periods") or []
        self.mix_period.blockSignals(True)
        self.mix_period.clear()
        for day in periods:
            self.mix_period.addItem(day, day)
        self.mix_period.blockSignals(False)
        groups = (biz.get("groups") or [])
        self.mix_kind.blockSignals(True)
        self.mix_kind.clear()
        for g in groups:
            self.mix_kind.addItem(g["type"], g["type"])
        self.mix_kind.blockSignals(False)
        self._render_mix()

    def _render_mix(self):
        biz = (self._data or {}).get("business") or {}
        period = self.mix_period.currentData() or biz.get("period")
        kind = self.mix_kind.currentData()
        by_period = biz.get("by_period") or {}
        groups = by_period.get(period) or biz.get("groups") or []
        group = next((g for g in groups if g["type"] == kind), None) or \
            (groups[0] if groups else None)
        self.mix_tbl.setRowCount(0)
        if not group:
            self.mix_hint.setText(f"该期没有主营构成数据({period or '-'})")
            self.mix_bar.set_rows([])
            return
        rows = group.get("rows") or []
        for i, it in enumerate(rows):
            self.mix_tbl.insertRow(i)
            item = set_cell(self.mix_tbl, i, 0, it.get("name") or "-", align="left")
            item.setToolTip(it.get("name") or "")
            set_cell(self.mix_tbl, i, 1, _yi(it.get("income")))
            ratio = it.get("ratio")
            set_cell(self.mix_tbl, i, 2,
                     f"{ratio * 100:.1f}" if ratio is not None else "-",
                     TEXT)
            m = it.get("margin")
            set_cell(self.mix_tbl, i, 3, f"{m * 100:.1f}" if m is not None else "-",
                     pnl_color(m) if m is not None else TEXT_MUT)
        self.mix_bar.set_rows(rows)
        self.mix_hint.setText(
            f"{period or '-'} · 按{group['type']};占比为收入占比,"
            f"毛利率只有部分公司披露")

    def _fill_finance(self, d: dict):
        self._render_finance(d)
        notes = d.get("notes") or []
        self.fin_card.set_hint(" · ".join(notes) if notes else "最近 8 期 · 同比与 ROE")

    def _render_finance(self, d: dict | None = None):
        d = d or self._data or {}
        fin = d.get("finance") or []
        n = self.fin_range.currentData() or 8
        trend = list(reversed(fin[:n]))
        self.chart.plot(trend)
        self.fin_tbl.setRowCount(0)
        for i, r in enumerate(reversed(trend)):
            self.fin_tbl.insertRow(i)
            set_cell(self.fin_tbl, i, 0, r.get("period") or "-", align="left")
            set_cell(self.fin_tbl, i, 1, _yi(r.get("revenue")))
            set_cell(self.fin_tbl, i, 2, _f(r.get("revenue_yoy"), 1, True),
                     pnl_color(r.get("revenue_yoy")))
            set_cell(self.fin_tbl, i, 3, _yi(r.get("net_profit")))
            set_cell(self.fin_tbl, i, 4, _f(r.get("profit_yoy"), 1, True),
                     pnl_color(r.get("profit_yoy")))
            set_cell(self.fin_tbl, i, 5, _f(r.get("roe"), 1))
            set_cell(self.fin_tbl, i, 6, _f(r.get("gross_margin"), 1))

    def _fill_valuation(self, d: dict):
        v = d.get("valuation") or {}
        st = d.get("val_stats") or {}
        self.val_line.setText(
            f"PE {_f(v.get('pe_ttm'), 1)} · PB {_f(v.get('pb'), 2)}"
            f" · PS {_f(v.get('ps'), 2)} · 市值 {_yi(v.get('total_cap'), 0)} 亿")

        board = st.get("board") or ""
        n = st.get("peer_count")
        if n:
            self.val_hint.setText(
                f"{board or '同业'} {n} 只 · PE 中位 {_f(st.get('pe_median'), 1)}"
                f" · PB 中位 {_f(st.get('pb_median'), 2)}")
        else:
            self.val_hint.setText("估值表没有这只票(北交所/新股常见),同业无法对比"
                                  if not v else "同业数据未取到")

        pe, pb = v.get("pe_ttm"), v.get("pb")
        pe_pct, pb_pct = st.get("pe_pct"), st.get("pb_pct")
        pe_ok = pe is not None and pe > 0 and pe_pct is not None
        self.pe_bar.set_value(
            pe_pct, f"PE {_f(pe, 1)}",
            f"行业分位 {pe_pct * 100:.0f}%" if pe_ok else
            ("亏损,分位不适用" if pe is not None and pe < 0 else ""))
        self.pb_bar.set_value(pb_pct, f"PB {_f(pb, 2)}",
                              f"行业分位 {pb_pct * 100:.0f}%" if pb_pct is not None else "")

        self.peer_tbl.setRowCount(0)
        peers = d.get("peers") or []
        own = d.get("code")
        key, asc = self.peer_sort.currentData() or ("cap", False)
        field = {"cap": "total_cap", "pe": "pe_ttm", "pb": "pb"}.get(key, "total_cap")
        have = [p for p in peers
                if p.get(field) is not None
                and (field != "pe_ttm" or p.get(field) > 0)]
        miss = [p for p in peers if p not in have]
        have.sort(key=lambda p: p.get(field), reverse=not asc)
        ordered = have + miss
        show = list(ordered[:6])
        if own and not any(p.get("code") == own for p in show):
            me = next((p for p in ordered if p.get("code") == own), None)
            if me:
                show.append(me)
        for i, p in enumerate(show):
            me = p.get("code") == own
            self.peer_tbl.insertRow(i)
            item = set_cell(self.peer_tbl, i, 0, p.get("name") or p.get("code") or "-",
                            ACCENT if me else TEXT, bold=me, align="left")
            item.setToolTip(p.get("name") or "")
            set_cell(self.peer_tbl, i, 1, _f(p.get("pe_ttm"), 1),
                     TEXT if me else TEXT_DIM)
            set_cell(self.peer_tbl, i, 2, _f(p.get("pb"), 2), TEXT if me else TEXT_DIM)
            set_cell(self.peer_tbl, i, 3, _yi(p.get("total_cap"), 0), TEXT if me else TEXT_DIM)

    def _fill_holders(self, d: dict):
        h = d.get("holders") or {}
        self.hold_tbl.setRowCount(0)
        for i, x in enumerate((h.get("free_top10") or [])[:10]):
            self.hold_tbl.insertRow(i)
            item = set_cell(self.hold_tbl, i, 0, x.get("name") or "-", align="left")
            item.setToolTip(x.get("name") or "")
            set_cell(self.hold_tbl, i, 1, _f(x.get("ratio"), 2))
            ch = x.get("change")
            set_cell(self.hold_tbl, i, 2,
                     f"{ch / WAN:+,.1f}" if ch is not None else "-",
                     pnl_color(ch))

        self.div_tbl.setRowCount(0)
        for i, x in enumerate((d.get("dividends") or [])[:6]):
            self.div_tbl.insertRow(i)
            set_cell(self.div_tbl, i, 0, x.get("notice") or "-", align="left")
            item = set_cell(self.div_tbl, i, 1, x.get("plan") or "-", align="left")
            item.setToolTip(x.get("plan") or "")
            set_cell(self.div_tbl, i, 2, x.get("progress") or "-", align="left")

        self.ann_tbl.setRowCount(0)
        for i, a in enumerate((d.get("announcements") or [])[:8]):
            self.ann_tbl.insertRow(i)
            set_cell(self.ann_tbl, i, 0, str(a.get("time") or a.get("date") or "")[:10],
                     align="left")
            item = set_cell(self.ann_tbl, i, 1, a.get("title") or "-", align="left")
            item.setToolTip(a.get("title") or "")

        self.unlock_tbl.setRowCount(0)
        for i, u in enumerate((h.get("unlocks") or [])[:6]):
            self.unlock_tbl.insertRow(i)
            set_cell(self.unlock_tbl, i, 0, u.get("date") or "-", align="left")
            set_cell(self.unlock_tbl, i, 1, _yi(u.get("num"), 2))
            set_cell(self.unlock_tbl, i, 2, _f(u.get("ratio"), 2))
            set_cell(self.unlock_tbl, i, 3, u.get("type") or "-", align="left")

        lines = []
        counts = h.get("holder_counts") or []
        if counts:
            c0 = counts[0]
            s = f"户数 {c0['holders'] / WAN:,.1f} 万" if c0.get("holders") else "户数 -"
            if c0.get("change_pct") is not None:
                s += f",环比 {c0['change_pct']:+.1f}%"
            if c0.get("focus"):
                s += f",{c0['focus']}"
            lines.append(s)
        if h.get("controller"):
            s = f"控股 {h['controller']}"
            if h.get("controller_ratio"):
                s += f"({h['controller_ratio']:.1f}%)"
            lines.append(s)
        inst = h.get("institutions") or []
        if inst and inst[0].get("org_num"):
            s = f"机构 {inst[0]['org_num']:,.0f} 家"
            if inst[0].get("ratio") is not None:
                s += f",持股 {inst[0]['ratio']:.1f}%"
            lines.append(s)
        self.hold_hint.setText(" · ".join(_elide(x, 52) for x in lines[:3]))
        self.hold_hint.setToolTip("\n".join(lines))

    # ------------------------------------------------------------------ 生命周期
    def stop(self):
        # 一次公司分析 = 五六个接口,可能 1~3 秒;等不够就销毁 QThread 会崩。
        for t in list(self._tasks):
            t.wait(4000)
        self._tasks.clear()
