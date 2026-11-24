# -*- coding: utf-8 -*-
"""市场热点页:情绪、概念/行业涨幅榜、强势个股、热点快讯。

布局参考行情软件的「热点」页:
  · 顶部情绪条:涨停 / 跌停 / 炸板率 / 最高板 / 晋级率 / 情绪结论;
  · 概念热点、行业热点各一张榜,带主力净流入与领涨股;
  · 强势个股:涨幅榜(剔北交所、新股 N/C、ST);
  · 热点快讯:最近 18 条,双击打开原文。

数据在 engine/hotspot.py 里组合,这页只管展示和「哪块没取到」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QVBoxLayout, QWidget)

from ..engine import hotspot
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import ACCENT, TEXT, TEXT_DIM, TEXT_MUT, WARN

YI = 1e8


def _f(v, digits: int = 2, plus: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}" if plus else f"{v:.{digits}f}"


def _yi(v, digits: int = 2) -> str:
    return "-" if v is None else f"{v / YI:,.{digits}f}"


class HotspotPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._news: list[dict] = []
        self._build()
        QTimer.singleShot(1000, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("市场热点", hint="情绪 · 板块涨幅榜 · 强势个股 · 快讯")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn = QPushButton("刷新")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        top.body().addLayout(bar)

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.s_zt = StatCard("涨停", "-", "封住涨停的家数")
        self.s_dt = StatCard("跌停", "-", "封死跌停的家数")
        self.s_zb = StatCard("炸板率", "-", "炸板 / (涨停+炸板)")
        self.s_top = StatCard("最高板", "-", "连板高度")
        self.s_promo = StatCard("晋级率", "-", "昨日涨停 → 今日继续涨停")
        self.s_mood = StatCard("情绪", "-", "涨停/跌停/炸板率综合")
        for w in (self.s_zt, self.s_dt, self.s_zb, self.s_top, self.s_promo,
                  self.s_mood):
            stats.addWidget(w, 1)
        top.body().addLayout(stats)
        root.addWidget(top)

        board = Board("hotspot", cols=[0.34, 0.33, 0.33], rows=[0.56, 0.44])

        # 概念热点
        con_card = Card("概念热点", hint="按涨幅 · 主力净流入")
        self.con_tbl = table(["概念", "涨幅%", "主力(亿)", "领涨股"],
                             widths=[104, 62, 74, 104], stretch_col=0)
        con_card.add(self.con_tbl, 1)
        board.add(con_card, 0, 0)

        # 行业热点
        ind_card = Card("行业热点", hint="按涨幅")
        self.ind_tbl = table(["行业", "涨幅%", "主力(亿)", "领涨股"],
                             widths=[96, 62, 74, 100], stretch_col=0)
        ind_card.add(self.ind_tbl, 1)
        board.add(ind_card, 0, 1)

        # 强势个股
        stk_card = Card("强势个股", hint="涨幅榜 · 剔除北交所/新股/ST")
        self.stk_tbl = table(["代码", "名称", "涨幅%", "换手%", "成交(亿)"],
                             widths=[56, 84, 54, 50, 62], stretch_col=1)
        stk_card.add(self.stk_tbl, 1)
        board.add(stk_card, 0, 2)

        # 热点快讯
        news_card = Card("热点快讯", hint="最近 18 条 · 双击看原文")
        self.news_tbl = table(["时间", "标题", "来源"], widths=[86, 520, 80],
                              stretch_col=1)
        self.news_tbl.doubleClicked.connect(self._open_news)
        news_card.add(self.news_tbl, 1)
        board.add(news_card, 1, 0, colspan=3)

        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def refresh(self):
        if self._tasks:
            return
        self.hint.setText("取数中…(情绪 + 板块 + 涨幅榜 + 快讯)")
        self.status.emit("市场热点:刷新")
        self.btn.setEnabled(False)
        job = TaskRunner(hotspot.snapshot)
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

    # ------------------------------------------------------------------ 展示
    def _ok(self, d: dict):
        d = d if isinstance(d, dict) else {}
        err = d.get("errors") or {}
        self.hint.setText(f"更新 {d.get('ts')} · 交易日 {d.get('day') or '-'}"
                          + (f" · {'、'.join(err)} 未取到" if err else ""))
        m = d.get("mood") or {}
        self.s_zt.set(str(m.get("zt") if m.get("zt") is not None else "-"))
        self.s_dt.set(str(m.get("dt") if m.get("dt") is not None else "-"),
                      pnl_color(-(m.get("dt") or 0)))
        rate = m.get("zb_rate")
        self.s_zb.set(f"{rate * 100:.1f}%" if rate is not None else "-",
                      WARN if (rate or 0) >= 0.3 else TEXT)
        self.s_top.set(str(m.get("max_board") or "-"))
        pr = m.get("promote")
        self.s_promo.set(f"{pr * 100:.0f}%" if pr is not None else "-")
        mood = m.get("mood") or "-"
        self.s_mood.set(mood, ACCENT if mood else TEXT_MUT)

        self._fill_boards(self.con_tbl, d.get("concepts") or [])
        self._fill_boards(self.ind_tbl, d.get("industries") or [])

        self.stk_tbl.setRowCount(0)
        for i, r in enumerate(d.get("stocks") or []):
            self.stk_tbl.insertRow(i)
            set_cell(self.stk_tbl, i, 0, r.get("code") or "-", align="left")
            item = set_cell(self.stk_tbl, i, 1, r.get("name") or "-", align="left")
            item.setToolTip(r.get("name") or "")
            set_cell(self.stk_tbl, i, 2, _f(r.get("pct"), 2), pnl_color(r.get("pct")))
            set_cell(self.stk_tbl, i, 3, _f(r.get("turnover"), 1))
            set_cell(self.stk_tbl, i, 4, _yi(r.get("amount")))
            set_cell(self.stk_tbl, i, 5, _yi(r.get("market_cap"), 0))

        self._news = d.get("news") or []
        self.news_tbl.setRowCount(0)
        for i, n in enumerate(self._news):
            self.news_tbl.insertRow(i)
            set_cell(self.news_tbl, i, 0, n.get("time") or "-", align="left")
            item = set_cell(self.news_tbl, i, 1, n.get("title") or "-", align="left")
            item.setToolTip(n.get("title") or "")
            set_cell(self.news_tbl, i, 2, n.get("source") or "-", align="left")

    def _fill_boards(self, tbl, rows: list[dict]):
        tbl.setRowCount(0)
        for i, r in enumerate(rows):
            tbl.insertRow(i)
            item = set_cell(tbl, i, 0, r.get("name") or "-", align="left")
            item.setToolTip(r.get("name") or "")
            set_cell(tbl, i, 1, _f(r.get("pct"), 2), pnl_color(r.get("pct")))
            set_cell(tbl, i, 2, _yi(r.get("main")), pnl_color(r.get("main")))
            lead = r.get("lead_name") or "-"
            lp = r.get("lead_pct")
            item = set_cell(tbl, i, 3, f"{lead} {lp:+.1f}" if lp is not None else lead,
                            align="left")
            item.setToolTip(f"{lead} {_f(lp, 2, True)}%" if lp is not None else lead)

    def _open_news(self, idx):
        row = idx.row()
        if 0 <= row < len(self._news):
            url = self._news[row].get("url")
            if url:
                QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------ 生命周期
    def stop(self):
        for t in list(self._tasks):
            t.wait(6000)
        self._tasks.clear()
