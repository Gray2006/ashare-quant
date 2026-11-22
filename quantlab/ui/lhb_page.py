# -*- coding: utf-8 -*-
"""龙虎榜页:某日上榜个股 + 选中个股的买卖席位明细。

数据在 data/lhb.py(东财 datacenter)。非交易日接口返回空,页面必须说清楚原因,
不能让用户以为软件坏了 —— 所以空数据时显示 empty_hint() 的原文。

日期默认取最近 5 个工作日;若所选日接口没数据(节假日/尚未更新),自动回退到
更早的有数据的一天,并在提示里写明「实际显示的是哪一天」,不偷偷改日期。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QPushButton, QVBoxLayout,
                               QWidget)

from ..data import lhb
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, label, pnl_color, set_cell, table
from .theme import TEXT_DIM

WAN = 10000.0


def _num(v, digits: int = 2) -> str:
    """None / 缺失值统一显示 '-' —— 别让 f-string 撞到 None 崩掉整页。"""
    return "-" if v is None else f"{v:.{digits}f}"


class LhbPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rows: list[dict] = []
        self._filling = False
        self._build()
        QTimer.singleShot(800, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("龙虎榜", hint="东财每日榜单 · 席位按营业部合并")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("交易日", dim=True))
        self.day = QComboBox()
        for d in lhb.recent_days(5):
            self.day.addItem(d, d)
        self.day.setMinimumWidth(112)
        self.day.currentIndexChanged.connect(lambda *_: self.refresh())
        bar.addWidget(self.day)
        self.btn = QPushButton("刷新")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bar.addWidget(self.btn)
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        top.body().addLayout(bar)
        root.addWidget(top)

        board = Board("lhb", cols=[1.0], rows=[0.5, 0.28, 0.22])

        list_card = Card("上榜个股")
        self.empty = label("", dim=True)
        self.empty.setWordWrap(True)
        self.empty.hide()
        list_card.add(self.empty)
        self.tbl = table(
            ["代码", "名称", "收盘", "涨跌幅", "上榜原因", "净买入(万)"],
            widths=[70, 90, 70, 76, 460, 104], stretch_col=4)
        self.tbl.setObjectName("LhbBoard")
        self.tbl.itemSelectionChanged.connect(self._on_select)
        list_card.add(self.tbl, 1)
        board.add(list_card, 0, 0, min_h=240)
        self.list_card = list_card

        self.seat_card = Card("席位明细", hint="买/卖同一营业部已合并,净额=买-卖")
        self.seat_hint = label("先在上面选一只票", dim=True)
        self.seat_card.add(self.seat_hint)
        self.seat_tbl = table(
            ["营业部", "买入额(万)", "卖出额(万)", "净额(万)"],
            widths=[420, 108, 108, 108], stretch_col=0)
        self.seat_tbl.setObjectName("LhbSeats")
        self.seat_card.add(self.seat_tbl, 1)
        board.add(self.seat_card, 1, 0, min_h=170)

        # 游资手法:席位胜率 + 跟买回测。数据来自 engine/hotmoney.py,
        # 和云端 quant-learn 同一套口径(贝叶斯平滑后的 5 日胜率)。
        self.hot_card = Card("游资手法", hint="席位胜率 · 跟买回测(5日)")
        hbar = QHBoxLayout()
        hbar.setSpacing(8)
        self.hot_hint = label("", dim=True)
        hbar.addWidget(self.hot_hint, 1)
        self.hot_btn = QPushButton("采集")
        self.hot_btn.setToolTip("拉最近 30 天龙虎榜席位并回填收益,约几分钟")
        self.hot_btn.clicked.connect(self._collect_hot)
        hbar.addWidget(self.hot_btn)
        self.hot_card.body().addLayout(hbar)
        self.hot_tbl = table(
            ["席位", "样本", "5日均收益", "胜率", "净买入(亿)"],
            widths=[420, 60, 96, 70, 96], stretch_col=0)
        self.hot_tbl.setObjectName("LhbHot")
        self.hot_card.add(self.hot_tbl, 1)
        board.add(self.hot_card, 2, 0, min_h=150)

        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def refresh(self):
        day = self.day.currentData() or ""
        self.btn.setEnabled(False)
        self.hint.setText(f"{day} 取数中…")

        def job():
            # 所选日没数据就往更早的日子退最多两步,回退结果必须回报给界面
            order = [self.day.itemData(i) for i in range(self.day.count())]
            try:
                start = order.index(day)
            except ValueError:
                start = 0
            for d in order[start:start + 3]:
                rows = lhb.board(d)
                if rows:
                    return d, rows, day
            return day, [], day

        r = TaskRunner(job)
        r.succeeded.connect(self._ok)
        r.failed.connect(self._err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()
        self._refresh_hot()

    def _ok(self, payload):
        day, rows, asked = payload
        self._rows = rows or []
        if not self._rows:
            self._show_empty(lhb.empty_hint(asked))
            self.status.emit(f"龙虎榜 {asked} 没有数据")
            return

        self.empty.hide()
        self._filling = True
        self.tbl.setRowCount(len(self._rows))
        for r, it in enumerate(self._rows):
            chg = it.get("change_pct")
            net = it.get("net")
            set_cell(self.tbl, r, 0, it.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.tbl, r, 1, it.get("name", ""), align="left")
            set_cell(self.tbl, r, 2, _num(it.get("close")))
            set_cell(self.tbl, r, 3, "-" if chg is None else f"{chg:+.2f}%",
                     pnl_color(chg))
            reason = it.get("reason", "") or ""
            cell = set_cell(self.tbl, r, 4, reason, TEXT_DIM, align="left")
            if reason:
                cell.setToolTip(reason)
            set_cell(self.tbl, r, 5,
                     "-" if net is None else f"{net / WAN:+,.0f}",
                     pnl_color(net))
        self._filling = False

        if day != asked:
            self.hint.setText(f"{asked} 无数据 · 显示 {day} · {len(self._rows)} 条")
        else:
            self.hint.setText(f"{day} · {len(self._rows)} 条"
                              "(同一票多个上榜原因会分行)")
        self.list_card.set_hint(f"{day} · {len(self._rows)} 只上榜")
        self.status.emit(f"龙虎榜 {day} 取到 {len(self._rows)} 条")

        # 自动选第一行,下表单直接出数,不用用户先点
        self.tbl.selectRow(0)
        self._load_seats(0)

    def _show_empty(self, text: str) -> None:
        self._filling = True
        self.tbl.setRowCount(0)
        self._filling = False
        self._rows = []
        self.empty.setText(text)
        self.empty.show()
        self.seat_tbl.setRowCount(0)
        self.seat_hint.setText("没有榜单数据就没有席位")
        self.seat_card.set_hint("—")

    def _err(self, msg):
        self.hint.setText(f"取数失败:{str(msg)[:90]}")
        self._show_empty(f"龙虎榜取数失败:{str(msg)[:120]}")

    # ------------------------------------------------------------------ 席位
    def _on_select(self):
        if self._filling:
            return
        self._load_seats(self.tbl.currentRow())

    def _load_seats(self, row: int):
        if row < 0 or row >= len(self._rows):
            return
        it = self._rows[row]
        code, day = it.get("code", ""), it.get("date") or self.day.currentData()
        name = it.get("name", "")
        self.seat_hint.setText(f"{code} {name} · 取席位中…")
        self.seat_tbl.setRowCount(0)

        def job():
            return row, code, day, lhb.seats(code, day)

        def ok(payload):
            r, c, d, seats = payload
            if r != self.tbl.currentRow() or r >= len(self._rows):
                return                      # 用户已经点了别的行,丢弃这次结果
            self.seat_tbl.setRowCount(len(seats))
            for i, s in enumerate(seats):
                set_cell(self.seat_tbl, i, 0, s.get("name", ""), TEXT_DIM,
                         align="left")
                set_cell(self.seat_tbl, i, 1, f"{(s.get('buy') or 0) / WAN:,.0f}")
                set_cell(self.seat_tbl, i, 2, f"{(s.get('sell') or 0) / WAN:,.0f}")
                net = s.get("net")
                set_cell(self.seat_tbl, i, 3,
                         "-" if net is None else f"{net / WAN:+,.0f}",
                         pnl_color(net))
            if seats:
                self.seat_hint.setText(f"{c} {name} · {d} · {len(seats)} 个席位")
            else:
                self.seat_hint.setText(f"{c} {name} · {d} 接口没返回席位明细")
                self.status.emit(f"{c} 席位为空")

        def fail(msg):
            self.seat_hint.setText(f"席位取数失败:{str(msg)[:80]}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(fail)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    # ------------------------------------------------------------------ 游资
    def _refresh_hot(self):
        def job():
            from ..engine import hotmoney as hm
            return hm.seat_stats(min_samples=3, days=60)

        r = TaskRunner(job)
        r.succeeded.connect(self._hot_ok)
        r.failed.connect(lambda m: self.hot_hint.setText(f"游资数据失败:{str(m)[:80]}"))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _hot_ok(self, stats):
        top = [s for s in stats if s["win5"] >= 0.55][:10]
        self.hot_tbl.setRowCount(len(top))
        for i, s in enumerate(top):
            set_cell(self.hot_tbl, i, 0, s["seat"], TEXT_DIM, align="left")
            set_cell(self.hot_tbl, i, 1, str(s["n"]))
            set_cell(self.hot_tbl, i, 2, f"{s['avg5']:.2%}", pnl_color(s["avg5"]))
            set_cell(self.hot_tbl, i, 3, f"{s['win5']:.0%}")
            set_cell(self.hot_tbl, i, 4, f"{s['net_sum'] / 1e8:+.2f}",
                     pnl_color(s["net_sum"]))
        if top:
            self.hot_hint.setText(f"高胜率席位 {len(top)} 个(样本≥3,胜率≥55%)")
        else:
            self.hot_hint.setText("暂无游资数据,点「采集」拉最近 30 天")

    def _collect_hot(self):
        self.hot_btn.setEnabled(False)
        self.hot_hint.setText("采集龙虎榜中…")

        def job():
            from ..engine import hotmoney as hm
            out = hm.collect(days=30, max_stocks=20)
            hm.fill_returns(limit=1000)
            return out

        def ok(out):
            self.hot_hint.setText(f"采集 {out.get('days', 0)} 天 "
                                  f"{out.get('rows', 0)} 条,刷新中…")
            self._refresh_hot()

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(lambda m: self.hot_hint.setText(f"采集失败:{str(m)[:80]}"))
        r.finished.connect(lambda: self.hot_btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    # ------------------------------------------------------------------ 退出
    def stop(self):
        for t in self._tasks:
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
        self._tasks.clear()
