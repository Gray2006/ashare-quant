# -*- coding: utf-8 -*-
"""板块页:行业 / 概念 / 地域 资金流榜 + 板块分时与日K + 成分股钻取。

数据口径(详见 data/sector.py):
  · 行业/概念榜 = 新浪 ssl_bkzj_bk(真实流入/流出/净额/净占比/领涨股),
    板块名点进去就是成分股,链路 100% 通。
  · 地域榜 = 腾讯 getRank,只有资金流,没有成分股数据源,界面直说。
  · 分时/日K 需要腾讯 pt 码:新浪板块名对得上才有,对不上给明确提示,不伪造。
"""
from __future__ import annotations

import time

import pandas as pd
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

from ..data import sector
from ..engine.hub import TaskRunner
from ..utils import fmt_amount, fmt_pct, fmt_price
from .board import Board
from .charts import KLineWidget, TrendWidget
from .common import Card, label, pnl_color, set_cell, table
from .theme import TEXT_DIM, TEXT_MUT

BOARD_COLS = ["板块", "涨跌幅", "主力净额", "净占比", "流入", "流出", "成交额", "领涨股", "领涨%"]
CONS_COLS = ["代码", "名称", "现价", "涨跌幅", "成交额", "换手", "流通市值"]


class SectorPage(QWidget):
    status = Signal(str)
    open_stock = Signal(str)          # 双击成分股 → 行情页

    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = "hy"
        self.rows: list[dict] = []
        self.shown: list[dict] = []
        self.current: dict | None = None
        self._tasks: list[TaskRunner] = []
        self._detail_loaded_for = ""
        self._pending_focus = ""
        self._filling = False
        self._chart_pending: set[str] = set()
        self._build()
        self._loaded = False
        self._timer = QTimer(self)
        self._timer.setInterval(20000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:          # 首次显示才拉数据,不拖慢启动
            self._loaded = True
            self.refresh(force=False)

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("sector", cols=[0.40, 0.60], rows=[1.0])
        root.addWidget(board)

        left = Card("板块资金流")
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.kind_btns: dict[str, QPushButton] = {}
        for key, text in (("hy", "行业"), ("gn", "概念"), ("dy", "地域")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(key == self.kind)
            b.clicked.connect(lambda _=False, k=key: self.set_kind(k))
            self.kind_btns[key] = b
            bar.addWidget(b)
        bar.addSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤板块")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self._apply_filter())
        bar.addWidget(self.search, 1)
        btn = QPushButton("刷新")
        btn.clicked.connect(lambda: self.refresh(force=True))
        bar.addWidget(btn)
        left._lay.addLayout(bar)

        self.tbl = table(BOARD_COLS, widths=[104, 68, 84, 62, 78, 78, 84, 78, 62],
                         stretch_col=0)
        self.tbl.itemSelectionChanged.connect(self._on_select)
        left.add(self.tbl, 1)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        left.add(self.note)
        board.add(left, 0, 0, min_w=520, min_h=300)

        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)

        detail = Card("板块详情")
        head = QHBoxLayout()
        head.setSpacing(12)
        self.d_name = QLabel("--")
        self.d_name.setObjectName("PageTitle")
        self.d_chg = QLabel("")
        self.d_chg.setStyleSheet("font-size:15px;font-weight:600;")
        self.d_amount = label("--", size=13, bold=True)
        self.d_ratio = label("", dim=True, size=12)
        head.addWidget(self.d_name)
        head.addWidget(self.d_chg)
        head.addSpacing(8)
        head.addWidget(self.d_amount)
        head.addWidget(self.d_ratio)
        head.addStretch(1)
        detail._lay.addLayout(head)

        self.d_meta = QLabel("")
        self.d_meta.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        detail._lay.addWidget(self.d_meta)

        self.charts = QTabWidget()
        self.trend = TrendWidget()
        self.kline = KLineWidget()
        self.charts.addTab(self.trend, "分时")
        self.charts.addTab(self.kline, "日K")
        self.charts.currentChanged.connect(lambda *_: self._load_chart(self.current))
        detail.add(self.charts, 1)
        self.chart_note = QLabel("")
        self.chart_note.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        detail.add(self.chart_note)
        right.addWidget(detail)

        cons_card = Card("成分股")
        self.cons_tbl = table(CONS_COLS, widths=[74, 96, 72, 70, 92, 62, 92],
                              stretch_col=1)
        self.cons_tbl.doubleClicked.connect(lambda *_: self._open_cons())
        cons_card.add(self.cons_tbl, 1)
        self.cons_note = QLabel("")
        self.cons_note.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        cons_card.add(self.cons_note)
        right.addWidget(cons_card)
        right.setSizes([420, 380])
        board.add(right, 0, 1, min_w=520, min_h=320)

    # ------------------------------------------------------------ 数据
    def _run(self, fn, on_ok, name: str = "") -> None:
        # 同名任务还在跑就跳过:网络慢时不会每 20 秒叠一层线程
        if name and any(t.name == name and t.isRunning() for t in self._tasks):
            return
        t = TaskRunner(fn, name=name or "sector")
        t.succeeded.connect(on_ok)
        t.failed.connect(lambda msg: self.status.emit(f"{name or '板块'}失败:{msg.splitlines()[0]}"))
        self._tasks.append(t)
        t.finished.connect(lambda t=t: self._tasks.remove(t) if t in self._tasks else None)
        t.start()

    def set_kind(self, kind: str) -> None:
        if kind == self.kind:
            return
        self.kind = kind
        for k, b in self.kind_btns.items():
            b.setChecked(k == kind)
        self.rows, self.shown = [], []
        self._pending_focus = ""
        self.tbl.setRowCount(0)
        self._clear_detail()          # 不留在上一个分类的板块上
        self.refresh(force=False)

    def _clear_detail(self) -> None:
        """切分类/取不到数据时清空右侧,避免详情和榜单对不上。"""
        self.current = None
        self._detail_loaded_for = ""
        self.d_name.setText("--")
        self.d_chg.setText("")
        self.d_amount.setText("--")
        self.d_ratio.setText("")
        self.d_meta.setText("")
        self.chart_note.setText("")
        self.trend.plot_trend({})
        self.kline.plot(None)
        self.kline.info.setText("")
        self.cons_tbl.setRowCount(0)
        self.cons_note.setText("")

    def refresh(self, force: bool = False) -> None:
        kind = self.kind
        self._run(lambda: (kind, sector.boards(kind, force=force)), self._on_boards, "板块榜")

    def _on_boards(self, payload) -> None:
        kind, rows = payload
        if kind != self.kind:
            return
        self.rows = rows or []
        self._apply_filter()
        src = "新浪" if (self.rows and self.rows[0]["source"] == "sina") else "腾讯"
        self.note.setText(f"{sector.KIND_LABEL[kind]} {len(self.rows)} 个 · 源 {src}"
                          f" · {time.strftime('%H:%M:%S')}")
        self.status.emit(f"板块资金流已更新:{len(self.rows)} 个{sector.KIND_LABEL[kind]}板块")

    def _apply_filter(self) -> None:
        q = self.search.text().strip()
        self.shown = [r for r in self.rows if not q or q in str(r.get("name") or "")]
        self._fill_table()

    def _fill_table(self) -> None:
        t = self.tbl
        keep = self.current["name"] if self.current else ""
        self._filling = True          # 自己重填表格时别触发 _on_select 的整套重载
        try:
            t.setRowCount(len(self.shown))
            for i, r in enumerate(self.shown):
                set_cell(t, i, 0, str(r.get("name") or ""), align="left")
                set_cell(t, i, 1, fmt_pct(r.get("change_pct")), pnl_color(r.get("change_pct")))
                set_cell(t, i, 2, fmt_amount(r.get("main")), pnl_color(r.get("main")), bold=True)
                ratio = r.get("main_ratio")
                set_cell(t, i, 3, f"{ratio:.2f}%" if ratio is not None else "-",
                         pnl_color(ratio))
                set_cell(t, i, 4, fmt_amount(r.get("main_in")))
                set_cell(t, i, 5, fmt_amount(r.get("main_out")))
                set_cell(t, i, 6, fmt_amount(r.get("amount")))
                set_cell(t, i, 7, str(r.get("lead_name") or "-"), align="left")
                set_cell(t, i, 8, fmt_pct(r.get("lead_pct")), pnl_color(r.get("lead_pct")))
            # 恢复选中;定位请求优先
            want = self._pending_focus or keep
            if want:
                for i, r in enumerate(self.shown):
                    n = str(r.get("name") or "")
                    if n == want or n.removesuffix("板块") == want.removesuffix("板块"):
                        t.selectRow(i)
                        if self._pending_focus:
                            self._pending_focus = ""
                        break
        finally:
            self._filling = False

    def _on_select(self) -> None:
        idxs = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        if not idxs:
            return
        row = idxs[0].row()
        if not (0 <= row < len(self.shown)):
            return
        b = self.shown[row]
        same = self.current is not None and b.get("key") == self.current.get("key")
        self.current = b
        self._show_head(b)
        if self._filling or same:
            return                    # 表格自己重填引起的选择变化,不重拉图片/成分
        self._load_chart(b)
        self._load_cons(b)

    def _show_head(self, b: dict) -> None:
        self.d_name.setText(str(b.get("name") or "--"))
        self.d_chg.setText(fmt_pct(b.get("change_pct")))
        self.d_chg.setStyleSheet(f"font-size:15px;font-weight:600;color:{pnl_color(b.get('change_pct'))};")
        self.d_amount.setText(f"主力净额 {fmt_amount(b.get('main'))}")
        self.d_amount.setStyleSheet(f"font-size:13px;font-weight:600;color:{pnl_color(b.get('main'))};")
        ratio = b.get("main_ratio")
        self.d_ratio.setText(f"净占比 {ratio:.2f}%" if ratio is not None else "")
        up, down = b.get("up"), b.get("down")
        bits = [f"流入 {fmt_amount(b.get('main_in'))}",
                f"流出 {fmt_amount(b.get('main_out'))}",
                f"成交额 {fmt_amount(b.get('amount'))}"]
        if up is not None and down is not None:
            bits.append(f"上涨 {up:.0f} / 下跌 {down:.0f}")
        if b.get("lead_name"):
            bits.append(f"领涨 {b['lead_name']} {fmt_pct(b.get('lead_pct'))}")
        self.d_meta.setText(" · ".join(bits))

    def _load_chart(self, b: dict | None, refresh: bool = False) -> None:
        """refresh=True 只跳过「同一个板块不重复画」的界面判重,数据缓存照旧由
        sector 层的 TTL 决定 —— 定时刷新因此不会强行打网络,只在新数据到了才重画。"""
        if not b:
            return
        which = self.charts.currentIndex()
        key = f"{b.get('key')}#{which}"
        if key in self._chart_pending:
            return
        if not refresh and key == self._detail_loaded_for:
            return
        self._chart_pending.add(key)
        if which == 0:
            self._run(lambda: (b, key, sector.minute(b)), self._on_minute, "板块分时")
        else:
            self._run(lambda: (b, key, sector.kline(b, days=120)), self._on_kline, "板块日K")

    def _on_minute(self, payload) -> None:
        b, key, data = payload
        self._chart_pending.discard(key)
        if not self.current or b.get("key") != self.current.get("key"):
            return
        self._detail_loaded_for = key
        bars = (data or {}).get("bars") or []
        self.trend.plot_trend(data or {})
        approx = (data or {}).get("approx") or ""
        if not bars:
            hint = ("该板块没有分时数据源(腾讯 pt 码对不上)" if self.kind != "dy"
                    else "地域板块无分时源")
            self.trend.hint.setText(hint)
            self.trend.hint.show()
            self.chart_note.setText("")
        else:
            self.chart_note.setText(f"近似口径:借用申万一级「{approx}」板块指数" if approx else "")
        self.status.emit(f"{b.get('name')} 分时 {len(bars)} 点")

    def _on_kline(self, payload) -> None:
        b, key, rows = payload
        self._chart_pending.discard(key)
        if not self.current or b.get("key") != self.current.get("key"):
            return
        self._detail_loaded_for = key
        if not rows:
            self.kline.info.setText("该板块没有日K数据源(腾讯 pt 码对不上)")
            self.kline.plot(None)
            self.chart_note.setText("")
            return
        resolved = sector._resolve_tx(b)
        self.chart_note.setText(
            f"近似口径:借用申万一级「{resolved.get('approx')}」板块指数"
            if resolved.get("approx") else "")
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        df["volume"] = df.get("volume", pd.Series([0] * len(df))).fillna(0)
        self.kline.plot(df)

    def _load_cons(self, b: dict) -> None:
        self.cons_tbl.setRowCount(0)
        self.cons_note.setText("加载成分股…")
        self._run(lambda: (b, sector.constituents(b)), self._on_cons, "成分股")

    def _on_cons(self, payload) -> None:
        b, rows = payload
        if not self.current or b.get("key") != self.current.get("key"):
            return
        rows = rows or []
        t = self.cons_tbl
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            set_cell(t, i, 0, str(r.get("code") or ""), align="left")
            set_cell(t, i, 1, str(r.get("name") or ""), align="left")
            set_cell(t, i, 2, fmt_price(r.get("price")))
            set_cell(t, i, 3, fmt_pct(r.get("change_pct")), pnl_color(r.get("change_pct")))
            set_cell(t, i, 4, fmt_amount(r.get("amount")))
            tr = r.get("turnover")
            set_cell(t, i, 5, f"{tr:.2f}%" if tr is not None else "-")
            cap = r.get("mktcap")
            set_cell(t, i, 6, fmt_amount(cap * 1e4) if cap else "-")
        if rows:
            self.cons_note.setText(f"{len(rows)} 只 · 双击进行情页")
        else:
            self.cons_note.setText("该板块无成分股数据源(地域板块暂缺,东财 clist 恢复后可补)")
        self.status.emit(f"{b.get('name')} 成分股 {len(rows)} 只")

    def _open_cons(self) -> None:
        idxs = self.cons_tbl.selectionModel().selectedRows() if self.cons_tbl.selectionModel() else []
        if not idxs:
            return
        row = idxs[0].row()
        item = self.cons_tbl.item(row, 0)
        if item is not None and item.text():
            self.open_stock.emit(item.text())

    # ------------------------------------------------------------ 外部跳转
    def focus_board(self, name: str) -> None:
        """行情页双击「所属板块」时定位到对应板块。"""
        name = str(name or "").strip()
        if not name:
            return

        def job():
            for k in ("hy", "gn", "dy"):
                for r in sector.boards(k):
                    n = str(r.get("name") or "")
                    if n == name or n.removesuffix("板块") == name.removesuffix("板块"):
                        return k, n
            return "", name

        self._run(job, self._on_focus, "定位板块")

    def _on_focus(self, payload) -> None:
        kind, name = payload
        if not kind:
            self.status.emit(f"板块榜里没有「{name}」")
            return
        self._pending_focus = name
        if kind == self.kind and self.rows:
            self._apply_filter()
            return
        if kind != self.kind:
            self.kind = kind
            for k, b in self.kind_btns.items():
                b.setChecked(k == kind)
            self.rows, self.shown = [], []
            self._clear_detail()
            self.tbl.setRowCount(0)
        self.refresh(force=False)     # 榜单还没加载过,拉一次再定位

    # ------------------------------------------------------------ 定时
    def _tick(self) -> None:
        if not self.isVisible():
            return
        self.refresh(force=False)
        if self.current:
            # 只刷分时;日K 是 300 秒量级的数据,每 20 秒重画没意义
            if self.charts.currentIndex() == 0:
                self._load_chart(self.current, refresh=True)
            self._load_cons(self.current)
