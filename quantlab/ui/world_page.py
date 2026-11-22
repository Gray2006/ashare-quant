# -*- coding: utf-8 -*-
"""全球市场页:外盘行情(美股/港股/亚太/商品/汇率) + 国际新闻时政。

行情走 data/world.py 的腾讯+新浪组合,新闻走东财 7x24 → 新浪直播 → 金十级联。
两者都在后台线程刷新,界面只负责渲染。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..data import world
from ..engine.hub import TaskRunner
from ..utils import fmt_pct, fmt_price
from .board import Board
from .common import Card, label, pnl_color, set_cell, table
from .theme import BORDER, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP

KINDS = ["全部", "美股", "港股", "亚太", "欧洲", "商品", "汇率"]
TAGS = ["全部", "美联储", "关税贸易", "地缘政治", "宏观数据", "A股市场",
        "原油能源", "黄金贵金属", "美股", "港股", "综合"]


class WorldPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = cfg()
        self._tasks: list[TaskRunner] = []
        self._news: list[dict] = []
        self._quotes: list[dict] = []
        self._busy = {"quotes": False, "news": False}
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._countdown = 0
        self._load()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        board = Board("world", cols=[1.0], rows=[0.5, 0.5])
        root.addWidget(board)

        # ---------------- 外盘
        qcard = Card("外盘行情")
        bar = QHBoxLayout()
        self.kind = QComboBox()
        for k in KINDS:
            self.kind.addItem(k)
        self.kind.currentIndexChanged.connect(lambda *_: self._fill_quotes())
        self.q_info = QLabel("加载中…")
        self.q_info.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        b_refresh = QPushButton("刷新")
        b_refresh.clicked.connect(self.load_quotes)
        bar.addWidget(QLabel("类别"))
        bar.addWidget(self.kind)
        bar.addWidget(self.q_info, 1)
        bar.addWidget(b_refresh)
        qcard._lay.addLayout(bar)
        self.tbl = table(["类别", "名称", "现价", "涨跌", "涨跌幅", "时间"],
                         widths=[66, 150, 110, 96, 84, 160], stretch_col=1)
        qcard.add(self.tbl, 1)
        board.add(qcard, 0, 0, min_h=220)

        # ---------------- 新闻
        ncard = Card("国际新闻 · 时政宏观")
        nbar = QHBoxLayout()
        self.tag = QComboBox()
        for t in TAGS:
            self.tag.addItem(t)
        self.tag.currentIndexChanged.connect(lambda *_: self._fill_news())
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤关键词,如 美联储 / 关税 / 原油")
        self.search.textChanged.connect(lambda *_: self._fill_news())
        self.n_info = QLabel("")
        self.n_info.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        nbar.addWidget(QLabel("分类"))
        nbar.addWidget(self.tag)
        nbar.addWidget(self.search, 1)
        nbar.addWidget(self.n_info)
        ncard._lay.addLayout(nbar)
        self.news_tbl = table(["时间", "分类", "标题", "来源"],
                              widths=[128, 88, 760, 90], stretch_col=2)
        self.news_tbl.itemDoubleClicked.connect(self._open_news)
        ncard.add(self.news_tbl, 1)
        foot = QHBoxLayout()
        self.news_hint = label("双击一行在浏览器打开原文", dim=True)
        b_open = QPushButton("打开原文")
        b_open.clicked.connect(self._open_selected)
        foot.addWidget(self.news_hint, 1)
        foot.addWidget(b_open)
        ncard._lay.addLayout(foot)
        board.add(ncard, 1, 0, min_h=220)

    # ------------------------------------------------------------ 刷新
    def _tick(self) -> None:
        self._countdown -= 1
        if self._countdown <= 0:
            interval = max(3, int(self.cfg.get("world.refresh_seconds", 8) or 8))
            self._countdown = interval
            self.load_quotes()
            self.load_news()

    def _load(self) -> None:
        self._countdown = 1
        self.load_quotes()
        self.load_news()

    def load_quotes(self) -> None:
        if self._busy["quotes"]:
            return
        self._busy["quotes"] = True

        def done():
            self._busy["quotes"] = False

        r = TaskRunner(world.overseas)
        r.succeeded.connect(self._on_quotes)
        r.failed.connect(lambda e: self._on_quotes_err(e))
        r.finished.connect(done)
        self._tasks.append(r)
        r.start()

    def _on_quotes(self, rows: list) -> None:
        self._quotes = rows or []
        self._fill_quotes()

    def _on_quotes_err(self, err: str) -> None:
        self.q_info.setText(f"外盘拉取失败:{str(err)[:80]}")

    def _fill_quotes(self) -> None:
        kind = self.kind.currentText()
        rows = [r for r in self._quotes if kind == "全部" or r.get("kind") == kind]
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            col = pnl_color(r.get("change_pct"))
            set_cell(self.tbl, i, 0, str(r.get("kind") or ""), TEXT_DIM, align="left")
            set_cell(self.tbl, i, 1, str(r.get("name") or ""), align="left", bold=True)
            set_cell(self.tbl, i, 2, fmt_price(r.get("price")), col, bold=True)
            set_cell(self.tbl, i, 3, f"{r.get('change') or 0:+.2f}" if r.get("change") is not None else "-", col)
            set_cell(self.tbl, i, 4, fmt_pct(r.get("change_pct")), col)
            set_cell(self.tbl, i, 5, str(r.get("time") or ""), TEXT_DIM, align="left")
        up = sum(1 for r in rows if (r.get("change_pct") or 0) > 0)
        self.q_info.setText(f"{len(rows)} 项 · 上涨 {up} · 每 {self.cfg.get('world.refresh_seconds', 8)} 秒刷新")

    def load_news(self) -> None:
        if self._busy["news"]:
            return
        self._busy["news"] = True
        limit = int(self.cfg.get("world.news_count", 40) or 40)

        def done():
            self._busy["news"] = False

        r = TaskRunner(lambda: world.news(limit))
        r.succeeded.connect(self._on_news)
        r.failed.connect(lambda e: self.n_info.setText(f"新闻拉取失败:{str(e)[:60]}"))
        r.finished.connect(done)
        self._tasks.append(r)
        r.start()

    def _on_news(self, rows: list) -> None:
        self._news = rows or []
        self._fill_news()

    def _fill_news(self) -> None:
        tag = self.tag.currentText()
        kw = self.search.text().strip().lower()
        rows = []
        for r in self._news:
            if tag != "全部" and r.get("tag") != tag:
                continue
            if kw and kw not in (str(r.get("title") or "") + str(r.get("summary") or "")).lower():
                continue
            rows.append(r)
        self.news_tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            set_cell(self.news_tbl, i, 0, str(r.get("time") or "")[5:16], TEXT_DIM, align="left")
            set_cell(self.news_tbl, i, 1, str(r.get("tag") or ""), TEXT_DIM, align="left")
            item = set_cell(self.news_tbl, i, 2, str(r.get("title") or ""), align="left")
            if item is not None:
                item.setToolTip(str(r.get("summary") or r.get("title") or ""))
            set_cell(self.news_tbl, i, 3, str(r.get("source") or ""), TEXT_DIM, align="left")
        self.n_info.setText(f"{len(rows)}/{len(self._news)} 条")

    def _open_selected(self) -> None:
        rows = self.news_tbl.selectionModel().selectedRows() if self.news_tbl.selectionModel() else []
        if not rows:
            self.status.emit("先选中一条新闻")
            return
        self._open_index(rows[0].row())

    def _open_news(self, item) -> None:
        self._open_index(item.row())

    def _open_index(self, i: int) -> None:
        tag = self.tag.currentText()
        kw = self.search.text().strip().lower()
        rows = [r for r in self._news
                if (tag == "全部" or r.get("tag") == tag)
                and (not kw or kw in (str(r.get("title") or "") + str(r.get("summary") or "")).lower())]
        if i < 0 or i >= len(rows):
            return
        url = str(rows[i].get("url") or "")
        if not url:
            self.status.emit("这条没有原文链接")
            return
        from ..safeurl import open_external
        if not open_external(url, self):
            self.status.emit("这条链接不是安全的网页地址,已拦截")
            return
        self.status.emit(f"已打开:{url[:60]}")

    # ------------------------------------------------------------ 生命周期
    def stop(self) -> None:
        self._timer.stop()
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(500)
            except Exception:
                pass
