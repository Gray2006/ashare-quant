# -*- coding: utf-8 -*-
"""行情页:自选股实时表 + 右侧个股详情(分时/K线/资金流/新闻/公告/公司)。"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..data import em, moneyflow, sector, sina
from ..data import kline as kl
from ..engine import indicators as ind
from ..engine.hub import TaskRunner
from ..utils import (SESSION_LABEL, board_of, fmt_amount, fmt_pct, fmt_price,
                     normalize_code, session_state)
from .charts import FlowPanel, KLineWidget, TrendWidget
from .board import Board
from .common import Card, label, pnl_color, set_cell, table
from .theme import DOWN, FLAT, LINE, TEXT_DIM, TEXT_MUT, UP


class QuotePage(QWidget):
    status = Signal(str)
    open_detail = Signal(str)     # 请求外部(选股页)跳到某只票
    open_sector = Signal(str)     # 请求外部跳板块页,参数是板块名

    COLS = ["代码", "名称", "现价", "涨跌幅", "成交额", "换手", "量比", "主力净流入"]
    BOARD_COLS = ["类型", "板块", "涨跌幅", "主力净额", "净占比"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = cfg()
        self.codes: list[str] = self._load_codes()
        self.names: dict[str, str] = {}
        self.quotes: dict[str, dict] = {}
        self.current = self.codes[0] if self.codes else ""
        self._tasks: list[TaskRunner] = []
        self._detail_loaded_for = ""
        self._boards_for = ""
        self._feed_text = ""
        self._sort_col = -1          # -1 = 不排序
        self._sort_desc = True
        self._build()
        self._refresh_groups()
        self.reload_watchlist()

        # 分时/资金流/逐笔 自动刷新(只在这些标签页可见时跑,省接口)
        self._auto = QTimer(self)
        self._auto.setInterval(int(float(self.cfg.get("refresh_seconds.detail", 5)) * 1000))
        self._auto.timeout.connect(self._refresh_live)
        self._auto.start()

    # ------------------------------------------------------------ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        board = Board("quote", cols=[0.44, 0.56], rows=[1.0])
        root.addWidget(board)

        # 左:自选股
        left = Card("自选股")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.group = QComboBox()
        self.group.setMinimumWidth(84)
        self.group.addItem("全部", "")
        self.group.currentIndexChanged.connect(lambda *_: self.reload_watchlist())
        bar.addWidget(self.group)
        self.search = QLineEdit()
        self.search.setPlaceholderText("输入代码/名称,回车加自选")
        self.search.returnPressed.connect(self._add_from_search)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_from_search)
        del_btn = QPushButton("移除")
        del_btn.clicked.connect(self._remove_current)
        grp_btn = QPushButton("设分组")
        grp_btn.clicked.connect(self._set_group)
        bar.addWidget(self.search, 1)
        bar.addWidget(add_btn)
        bar.addWidget(del_btn)
        bar.addWidget(grp_btn)
        left._lay.addLayout(bar)

        self.tbl = table(self.COLS, widths=[78, 76, 82, 74, 90, 56, 52, 96], stretch_col=1)
        self.tbl.itemSelectionChanged.connect(self._on_select)
        self.tbl.doubleClicked.connect(lambda *_: self._open_full())
        self.tbl.horizontalHeader().sectionClicked.connect(self._on_sort)
        left.add(self.tbl, 1)

        self.watch_info = QLabel("")
        self.watch_info.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        left.add(self.watch_info)
        board.add(left, 0, 0, min_w=520, min_h=300)

        # 右:个股详情。再分两行,头部和图表区各占一条分栏线
        right = Board("quote.right", cols=[1.0], rows=[0.17, 0.83])
        head = Card(pad=12)
        hl = QHBoxLayout()
        hl.setSpacing(14)
        self.d_name = QLabel("--")
        self.d_name.setObjectName("PageTitle")
        self.d_code = QLabel("")
        self.d_code.setStyleSheet(f"color:{TEXT_MUT};")
        self.d_price = QLabel("--")
        self.d_price.setObjectName("BigNum")
        self.d_chg = QLabel("")
        self.d_chg.setStyleSheet("font-size:15px;font-weight:600;")
        self.d_meta = QLabel("")
        self.d_meta.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        hl.addWidget(self.d_name)
        hl.addWidget(self.d_code)
        hl.addSpacing(10)
        hl.addWidget(self.d_price)
        hl.addWidget(self.d_chg)
        hl.addStretch(1)
        head._lay.addLayout(hl)
        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.addWidget(self.d_meta)
        meta_row.addStretch(1)
        head._lay.addLayout(meta_row)

        self.ob_label = QLabel("")
        self.ob_label.setStyleSheet(
            f"color:{TEXT_DIM};font-family:Consolas,monospace;font-size:12px;"
            f"background:#0d141d;border:1px solid {LINE};border-radius:8px;"
            "padding:6px 10px;")
        head._lay.addWidget(self.ob_label)
        # 盘口 10 行 + 标题 + 元信息 ≈ 295:给死,不然数据晚到时卡片先按
        # 空标签算高度,盘口填进来就被裁掉半截
        right.add(head, 0, 0, min_w=460, min_h=300)

        self.tabs = QTabWidget()
        self.tab_trend = TrendWidget()
        self.tab_kline = KLineWidget()
        self.kline_toolbar = QWidget()
        kt = QHBoxLayout(self.kline_toolbar)
        kt.setContentsMargins(0, 0, 0, 0)
        kt.addStretch(1)
        self.kl_period = QComboBox()
        self.kl_period.addItems(["日线", "周线", "月线", "60分钟", "30分钟", "15分钟"])
        self.kl_period.currentTextChanged.connect(lambda *_: self.load_kline())
        kt.addWidget(self.kl_period)
        kline_wrap = QWidget()
        kw = QVBoxLayout(kline_wrap)
        kw.setContentsMargins(0, 0, 0, 0)
        kw.addWidget(self.kline_toolbar)
        kw.addWidget(self.tab_kline, 1)

        self.tab_flow = FlowPanel()
        self.tab_flow.period_changed.connect(self._on_flow_period)
        # 所属板块:东财 slist 一次拿全,带板块主力净额,双击跳到板块页
        self.tab_boards = table(self.BOARD_COLS, widths=[56, 130, 74, 96, 74], stretch_col=1)
        self.tab_boards.doubleClicked.connect(lambda *_: self._open_board())
        # 逐笔:4 列,跟卡片宽度自适应(以前是普通表 + 拉伸末列,
        # 卡片一窄右端「方向」直接被切,还滚不到)
        self.tab_ticks = table(["时间", "价格", "成交量(手)", "方向"],
                               widths=[84, 82, 100, 62])
        self.tab_news = QListWidget()
        self.tab_news.itemDoubleClicked.connect(self._open_link)
        self.tab_ann = QListWidget()
        self.tab_ann.itemDoubleClicked.connect(self._open_link)
        self.tab_profile = QWidget()
        self.profile_layout = QVBoxLayout(self.tab_profile)
        self.profile_layout.setContentsMargins(6, 6, 6, 6)
        # 财务:6 列,同样自适应(季度报告期 + 4 个数字列)
        self.tab_finance = table(["报告期", "营收(亿)", "同比%", "净利(亿)", "同比%", "ROE%"],
                                 widths=[88, 82, 68, 82, 68, 62])

        self.tabs.addTab(self.tab_trend, "分时")
        self.tabs.addTab(kline_wrap, "K线")
        self.tabs.addTab(self.tab_flow, "资金流")
        self.tabs.addTab(self.tab_boards, "所属板块")
        self.tabs.addTab(self.tab_ticks, "逐笔")
        self.tabs.addTab(self.tab_news, "新闻")
        self.tabs.addTab(self.tab_ann, "公告")
        self.tabs.addTab(self.tab_profile, "公司")
        self.tabs.addTab(self.tab_finance, "财务")
        self.tabs.currentChanged.connect(self._on_tab)
        right.add(self.tabs, 1, 0, min_w=460, min_h=240)
        board.add(right, 0, 1, min_w=520, min_h=300)

    # ------------------------------------------------------------ 数据
    def _load_codes(self) -> list[str]:
        from ..data.store import store
        rows = store().get_watchlist()
        codes = [r["code"] for r in rows]
        if not codes:
            codes = [normalize_code(c) for c in self.cfg.watchlist]
            for c in codes:
                store().add_watch(c)
        return codes

    def reload_watchlist(self) -> None:
        from ..data.store import store
        rows = store().get_watchlist()
        g = self.group.currentData() if hasattr(self, "group") else ""
        if g:
            rows = [r for r in rows if (r.get("group_name") or "默认") == g]
        self.codes = [r["code"] for r in rows]
        self.tbl.setRowCount(len(self.codes))
        for i, c in enumerate(self.codes):
            set_cell(self.tbl, i, 0, c, align="left")
            for j in range(1, len(self.COLS)):
                set_cell(self.tbl, i, j, "-")
        self.watch_info.setText(self._watch_text())
        if self.codes:
            self.tbl.selectRow(0)

    def select_code(self, code: str) -> None:
        """外部跳转:选中某只票。不在自选里就临时插一行,不写进自选库。"""
        code = normalize_code(code)
        if not code:
            return
        if code not in self.codes:
            self.codes.append(code)
            i = len(self.codes) - 1
            self.tbl.insertRow(i)
            set_cell(self.tbl, i, 0, code, align="left")
            for j in range(1, len(self.COLS)):
                set_cell(self.tbl, i, j, "-")
        i = self.codes.index(code)
        self.current = ""
        self.tbl.selectRow(i)
        self._on_select()
        self.raise_()

    def _watch_text(self) -> str:
        parts = [f"自选 {len(self.codes)} 只", SESSION_LABEL.get(session_state(), "")]
        if self._feed_text:
            parts.append(f"行情 {self._feed_text}")
        return " · ".join(p for p in parts if p)

    def set_feed_status(self, text: str) -> None:
        self._feed_text = text or ""
        self.watch_info.setText(self._watch_text())

    def update_quotes(self, data: dict) -> None:
        if not data:
            return
        self.quotes.update(data)
        for i, c in enumerate(self.codes):
            q = self.quotes.get(c)
            if not q:
                continue
            self.names[c] = q.get("name") or self.names.get(c, "")
            col = pnl_color(q.get("change_pct"))
            set_cell(self.tbl, i, 1, q.get("name") or "", align="left")
            set_cell(self.tbl, i, 2, fmt_price(q.get("price")), col, bold=True)
            set_cell(self.tbl, i, 3, fmt_pct(q.get("change_pct")), col, bold=True)
            set_cell(self.tbl, i, 4, fmt_amount(q.get("amount")))
            set_cell(self.tbl, i, 5, fmt_pct(q.get("turnover")).replace("%", ""))
            set_cell(self.tbl, i, 6, f"{q.get('volume_ratio') or 0:.2f}")
            inflow = q.get("main_inflow")
            set_cell(self.tbl, i, 7, fmt_amount(inflow), pnl_color(inflow))
        self._update_head()
        self.watch_info.setText(f"{self._watch_text()} · {time.strftime('%H:%M:%S')}")

    _SORT_KEYS = {2: "price", 3: "change_pct", 4: "amount",
                  5: "turnover", 6: "volume_ratio", 7: "main_inflow"}

    def _on_sort(self, col: int) -> None:
        """点列头排序,再点一次反向。"""
        if col not in self._SORT_KEYS:
            return
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        self._sort_and_fill()

    def _sort_and_fill(self) -> None:
        key = self._SORT_KEYS.get(self._sort_col)
        if not key:
            return
        cur = self.current
        self.codes.sort(
            key=lambda c: float(self.quotes.get(c, {}).get(key) or -1e18),
            reverse=self._sort_desc)
        self.tbl.setRowCount(len(self.codes))
        for i, c in enumerate(self.codes):
            set_cell(self.tbl, i, 0, c, align="left")
            for j in range(1, len(self.COLS)):
                set_cell(self.tbl, i, j, "-")
        for i, c in enumerate(self.codes):
            q = self.quotes.get(c)
            if not q:
                continue
            col = pnl_color(q.get("change_pct"))
            set_cell(self.tbl, i, 1, q.get("name") or "", align="left")
            set_cell(self.tbl, i, 2, fmt_price(q.get("price")), col, bold=True)
            set_cell(self.tbl, i, 3, fmt_pct(q.get("change_pct")), col, bold=True)
            set_cell(self.tbl, i, 4, fmt_amount(q.get("amount")))
            set_cell(self.tbl, i, 5, fmt_pct(q.get("turnover")).replace("%", ""))
            set_cell(self.tbl, i, 6, f"{q.get('volume_ratio') or 0:.2f}")
            inflow = q.get("main_inflow")
            set_cell(self.tbl, i, 7, fmt_amount(inflow), pnl_color(inflow))
        if cur in self.codes:
            self.tbl.selectRow(self.codes.index(cur))

    def _update_head(self) -> None:
        q = self.quotes.get(self.current)
        if not q:
            return
        col = pnl_color(q.get("change_pct"))
        self.d_name.setText(q.get("name") or "--")
        self.d_code.setText(f"{q.get('code')} · {board_of(q.get('code') or '')}")
        self.d_price.setText(fmt_price(q.get("price")))
        self.d_price.setStyleSheet(f"color:{col};")
        self.d_chg.setText(f"{fmt_pct(q.get('change_pct'))}  {fmt_price(q.get('change'))}")
        self.d_chg.setStyleSheet(f"color:{col};font-size:15px;font-weight:600;")
        self.d_meta.setText(
            f"开 {fmt_price(q.get('open'))}  高 {fmt_price(q.get('high'))}  "
            f"低 {fmt_price(q.get('low'))}  昨收 {fmt_price(q.get('pre_close'))}  "
            f"额 {fmt_amount(q.get('amount'))}  换手 {fmt_pct(q.get('turnover')).replace('%','')}%  "
            f"PE {q.get('pe_ttm') or '-'}  PB {q.get('pb') or '-'}  "
            f"市值 {fmt_amount(q.get('market_cap'))}")

    def _on_select(self) -> None:
        rows = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        if not rows:
            return
        i = rows[0].row()
        if i >= len(self.codes):
            return
        code = self.codes[i]
        if code == self.current and self._detail_loaded_for == code:
            return
        self.current = code
        self._update_head()
        self.load_detail(code)

    # ------------------------------------------------------------ 详情
    def _run(self, fn, on_ok, name=""):
        # 同名任务还在跑就跳过,避免网络慢时线程不断堆积(这是卡顿的主因之一)
        if name:
            for t in list(self._tasks):
                if getattr(t, "task_name", name) == name and t.isRunning():
                    return
        t = TaskRunner(fn, name=name, parent=self)
        t.task_name = name
        t.succeeded.connect(on_ok)
        t.failed.connect(lambda msg: self.status.emit(msg.splitlines()[0][:200]))
        self._tasks.append(t)
        t.finished.connect(lambda: self._tasks.remove(t) if t in self._tasks else None)
        t.start()

    def load_detail(self, code: str | None = None) -> None:
        code = code or self.current
        if not code:
            return
        self._detail_loaded_for = code

        def job():
            out = {"code": code}
            try:
                out["kline"] = kl.get_kline(code, 101, 260)[0]
            except Exception:
                out["kline"] = []
            try:
                # 腾讯分时最快最稳;失败再退东财
                out["trend"] = kl.tx_minute(code)
                if not (out["trend"] or {}).get("bars"):
                    out["trend"] = em.trends(code)
            except Exception:
                try:
                    out["trend"] = em.trends(code)
                except Exception:
                    out["trend"] = {}
            try:
                out["flow"] = em.moneyflow_intraday(code)
            except Exception:
                out["flow"] = {}
            try:
                out["ob"] = sina.orderbook(code)
            except Exception:
                out["ob"] = None
            return out

        self._run(job, self._apply_detail, "detail")

    def _apply_detail(self, out: dict) -> None:
        if out.get("code") != self.current:
            return
        bars = out.get("kline") or []
        if bars:
            df = ind.bars_to_df(bars)
            self.tab_kline.plot(df)
            self._kline_df = df
        tr = out.get("trend") or {}
        self.tab_trend.plot_trend(tr)
        fl = out.get("flow") or {}
        if self.tab_flow.is_intra():
            self.tab_flow.plot_flow(fl.get("bars") or [])
        ob = out.get("ob")
        if ob:
            self.ob_label.setText(self._ob_text(ob))
        # 换了股票,所属板块表作废,等切到那个 tab 再拉
        if self._boards_for != self.current:
            self.tab_boards.setRowCount(0)
            self._boards_for = ""

    # ------------------------------------------------------------ 所属板块
    def load_boards(self, code: str | None = None) -> None:
        code = code or self.current
        if not code:
            return
        self._boards_for = code
        self._run(lambda: (code, sector.of_stock(code)), self._fill_boards, "boards")

    def _fill_boards(self, payload) -> None:
        code, rows = payload
        if code != self.current:
            return
        rows = rows or []
        t = self.tab_boards
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            set_cell(t, i, 0, str(r.get("type") or ""), align="left")
            set_cell(t, i, 1, str(r.get("name") or ""), align="left")
            set_cell(t, i, 2, fmt_pct(r.get("change_pct")), pnl_color(r.get("change_pct")))
            set_cell(t, i, 3, fmt_amount(r.get("main")), pnl_color(r.get("main")), bold=True)
            ratio = r.get("main_ratio")
            set_cell(t, i, 4, f"{ratio:.2f}%" if ratio is not None else "-", pnl_color(ratio))
        if not rows:
            self.status.emit("所属板块暂不可用(东财 slist 冷却中)")

    def _open_board(self) -> None:
        rows = self.tab_boards.selectionModel().selectedRows() if self.tab_boards.selectionModel() else []
        if not rows:
            return
        item = self.tab_boards.item(rows[0].row(), 1)
        if item is not None and item.text():
            self.open_sector.emit(item.text())

    @staticmethod
    def _ob_text(ob: dict) -> str:
        asks = list(reversed(ob.get("asks") or []))[:5]
        bids = (ob.get("bids") or [])[:5]
        lines = []
        for p, v in asks:
            lines.append(f'<span style="color:{DOWN}">卖</span> {p:>8.2f}  {v/100:>9.0f}手')
        for p, v in bids:
            lines.append(f'<span style="color:{UP}">买</span> {p:>8.2f}  {v/100:>9.0f}手')
        body = "\n".join(lines)
        return ('<pre style="margin:0;font-family:Consolas,monospace;'
                f'font-size:12px;">{body}</pre>')

    # ------------------------------------------------------------ 资金流口径
    def _on_flow_period(self, key: str) -> None:
        """切换口径:分时→重画当日曲线;区间→按交易日合计。"""
        if key == "intra":
            if self.current:
                code = self.current

                def job() -> dict:
                    try:
                        return {"code": code, "flow": em.moneyflow_intraday(code)}
                    except Exception:  # noqa: BLE001
                        return {"code": code, "flow": {}}

                def on_ok(out: dict) -> None:
                    if out.get("code") == self.current and self.tab_flow.is_intra():
                        self.tab_flow.plot_flow((out.get("flow") or {}).get("bars") or [])

                self._run(job, on_ok, "flow_intra")
            return
        self.load_flow_window()

    def load_flow_window(self) -> None:
        """按当前口径取数:今日/3/5/7 日算合计并画柱,每日明细填表。"""
        code = self.current
        if not code:
            return
        key = self.tab_flow.key()
        if key == "intra":
            return
        days = 10 if key == "d10" else max(1, int(key))

        def job() -> dict:
            try:
                return {"code": code, "key": key, "days": days,
                        "win": moneyflow.window(code, days)}
            except Exception:  # noqa: BLE001
                return {"code": code, "key": key, "days": days, "win": {}}

        def on_ok(out: dict) -> None:
            if out.get("code") != self.current:
                return
            if self.tab_flow.key() != str(out.get("key")):
                return          # 用户已经切到别的口径,丢掉这次结果
            win = out.get("win") or {}
            if out.get("key") == "d10":
                self.tab_flow.plot_days(win)
            else:
                self.tab_flow.plot_window(win)

        self._run(job, on_ok, f"flow_win_{key}")

    def load_kline(self) -> None:
        code = self.current
        label_ = self.kl_period.currentText()
        klt = {"日线": 101, "周线": 102, "月线": 103,
               "60分钟": 60, "30分钟": 30, "15分钟": 15}.get(label_, 101)

        def job():
            bars, _src = kl.get_kline(code, klt, 300)
            return {"code": code, "bars": bars, "klt": klt}

        def on_ok(out):
            if out.get("code") != self.current or not out.get("bars"):
                return
            self.tab_kline.plot(ind.bars_to_df(out["bars"]))

        self._run(job, on_ok, "kline")

    def _on_tab(self, idx: int) -> None:
        text = self.tabs.tabText(idx)
        code = self.current
        if not code:
            return
        if text == "新闻" and self.tab_news.count() == 0:
            self._run(lambda: em.stock_news(code, 30), self._fill_news, "news")
        elif text == "公告" and self.tab_ann.count() == 0:
            self._run(lambda: em.announcements(code, 30), self._fill_ann, "ann")
        elif text == "公司" and self.profile_layout.count() == 0:
            self._run(lambda: em.company_profile(code), self._fill_profile, "profile")
        elif text == "财务" and self.tab_finance.rowCount() == 0:
            self._run(lambda: em.finance_summary(code), self._fill_finance, "finance")
        elif text == "逐笔":
            self.load_ticks()
        elif text == "资金流" and not self.tab_flow.is_intra():
            self.load_flow_window()
        elif text == "所属板块" and self._boards_for != code:
            self.load_boards(code)
        self._auto.setInterval(6000 if text in ("新闻", "公告", "公司", "财务", "K线") else
                              int(float(self.cfg.get("refresh_seconds.detail", 5)) * 1000))

    # ------------------------------------------------------------ 实时刷新
    def _refresh_live(self) -> None:
        """只刷新当前可见的实时标签页。"""
        if not self.current or not self.isVisible():
            return
        text = self.tabs.tabText(self.tabs.currentIndex())
        code = self.current
        if text == "分时":
            def job():
                out = {}
                try:
                    out["trend"] = kl.tx_minute(code)
                    if not (out["trend"] or {}).get("bars"):
                        out["trend"] = em.trends(code)
                except Exception:
                    out["trend"] = {}
                try:
                    out["ob"] = sina.orderbook(code)
                except Exception:
                    out["ob"] = None
                return {"code": code, **out}

            def on_ok(out):
                if out.get("code") != self.current:
                    return
                self.tab_trend.plot_trend(out.get("trend") or {})
                if out.get("ob"):
                    self.ob_label.setText(self._ob_text(out["ob"]))

            self._run(job, on_ok, "live_trend")
        elif text == "资金流":
            def job2():
                try:
                    return {"code": code, "flow": em.moneyflow_intraday(code)}
                except Exception:
                    return {"code": code, "flow": {}}

            def on_ok2(out):
                if out.get("code") == self.current and self.tab_flow.is_intra():
                    self.tab_flow.plot_flow((out.get("flow") or {}).get("bars") or [])

            self._run(job2, on_ok2, "live_flow")
        elif text == "逐笔":
            self.load_ticks()

    def load_ticks(self) -> None:
        code = self.current
        if not code:
            return

        def job():
            try:
                return {"code": code, "ticks": em.ticks_today(code, 80)}
            except Exception:
                return {"code": code, "ticks": []}

        def on_ok(out):
            if out.get("code") != self.current:
                return
            self._fill_ticks(out.get("ticks") or [])

        self._run(job, on_ok, "ticks")

    def _fill_ticks(self, rows: list) -> None:
        if not rows:
            # 不编造数据:取不到就明说
            self.tab_ticks.setRowCount(1)
            set_cell(self.tab_ticks, 0, 0, "--", align="left")
            set_cell(self.tab_ticks, 0, 1, "逐笔数据源暂不可用(东财限流),稍后自动重试",
                     FLAT, align="left")
            self.tab_ticks.setSpan(0, 1, 1, 3)
            return
        self.tab_ticks.clearSpans()
        self.tab_ticks.setRowCount(len(rows))
        for i, r in enumerate(rows):
            side = r.get("side") or ""
            col = UP if "买" in str(side) or side in ("1", "B") else (
                DOWN if "卖" in str(side) or side in ("2", "S") else FLAT)
            set_cell(self.tab_ticks, i, 0, str(r.get("time") or "")[:8], align="left")
            set_cell(self.tab_ticks, i, 1, fmt_price(r.get("price")), col)
            set_cell(self.tab_ticks, i, 2, f"{r.get('volume') or 0:,.0f}")
            set_cell(self.tab_ticks, i, 3, str(side), col, align="left")
        # 列宽交给 SmartTable 自适应,这里不再 resizeColumnsToContents()
        # (那会覆盖自适应宽度,窄卡片时把右端列挤没)

    def _fill_news(self, items: list) -> None:
        self.tab_news.clear()
        for it in items:
            text = f"{it.get('time', '')[5:16]}  {it.get('title', '')}"
            li = QListWidgetItem(text)
            li.setToolTip(f"{it.get('summary', '')}\n\n{it.get('url', '')}")
            li.setData(Qt.UserRole, it.get("url"))
            self.tab_news.addItem(li)

    def _fill_ann(self, items: list) -> None:
        self.tab_ann.clear()
        for it in items:
            li = QListWidgetItem(f"{it.get('date', '')}  [{it.get('type', '')}]  {it.get('title', '')}")
            li.setData(Qt.UserRole, it.get("url"))
            li.setToolTip(it.get("url", ""))
            self.tab_ann.addItem(li)

    def _fill_profile(self, p: dict) -> None:
        while self.profile_layout.count():
            w = self.profile_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        rows = [
            ("公司全称", p.get("full_name")), ("所属行业", p.get("industry")),
            ("董事长", p.get("chairman")), ("总经理", p.get("president")),
            ("董秘", p.get("secretary")), ("上市日期", p.get("list_date")),
            ("发行价", p.get("issue_price")), ("员工人数", p.get("employees")),
            ("注册资本", p.get("reg_capital")), ("注册地址", p.get("address")),
            ("办公地址", p.get("office_address")), ("网站", p.get("website")),
        ]
        for k, v in rows:
            if v:
                lab = label(f"{k}:{v}", dim=False)
                lab.setWordWrap(True)
                self.profile_layout.addWidget(lab)
        intro = p.get("intro") or ""
        if intro:
            self.profile_layout.addWidget(label("主营业务:", dim=True))
            txt = label(intro[:1200] + ("..." if len(intro) > 1200 else ""))
            txt.setWordWrap(True)
            self.profile_layout.addWidget(txt)
        self.profile_layout.addStretch(1)

    def _fill_finance(self, rows: list) -> None:
        self.tab_finance.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = [r.get("period"), f"{(r.get('revenue') or 0)/1e8:.2f}",
                    f"{r.get('revenue_yoy') or 0:.2f}",
                    f"{(r.get('net_profit') or 0)/1e8:.2f}",
                    f"{r.get('profit_yoy') or 0:.2f}", f"{r.get('roe') or 0:.2f}"]
            for j, v in enumerate(vals):
                set_cell(self.tab_finance, i, j, str(v), align="left" if j == 0 else "right")
        # 同上:列宽由 SmartTable 管

    # ------------------------------------------------------------ 操作
    def _add_from_search(self) -> None:
        raw = self.search.text().strip()
        if not raw:
            return
        code = normalize_code(raw)
        if len(code) != 6:
            # 允许用名称搜索:从全市场快照里找
            try:
                from ..engine import screener
                df = screener.snapshot()
                hit = df[df["name"].astype(str).str.contains(raw, na=False)]
                if hit.empty:
                    self.status.emit(f"没找到 {raw}")
                    return
                code = str(hit.iloc[0]["code"])
            except Exception as e:
                self.status.emit(f"搜索失败:{e}")
                return
        from ..data.store import store
        store().add_watch(code)
        self.search.clear()
        self.reload_watchlist()
        if code not in self.codes:
            self.codes.append(code)

    def _remove_current(self) -> None:
        if not self.current:
            return
        from ..data.store import store
        store().remove_watch(self.current)
        self.reload_watchlist()

    def _set_group(self) -> None:
        i = self.tbl.currentRow()
        if i < 0 or i >= len(self.codes):
            return
        code = self.codes[i]
        from ..data.store import store
        cur = ""
        for r in store().get_watchlist():
            if r["code"] == code:
                cur = r.get("group_name") or "默认"
                break
        g, ok = QInputDialog.getText(self, "设分组", "分组名(留空=默认):",
                                     text=cur)
        if not ok:
            return
        store().set_watch_group(code, (g or "").strip() or "默认")
        self._refresh_groups()
        self.reload_watchlist()

    def _refresh_groups(self) -> None:
        from ..data.store import store
        groups = sorted({(r.get("group_name") or "默认")
                         for r in store().get_watchlist()})
        cur = self.group.currentData()
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem("全部", "")
        for g in groups:
            self.group.addItem(g, g)
        i = self.group.findData(cur)
        if i >= 0:
            self.group.setCurrentIndex(i)
        self.group.blockSignals(False)

    def _open_full(self) -> None:
        self.open_detail.emit(self.current)

    def _open_link(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.UserRole)
        if url:
            from ..safeurl import open_external
            open_external(str(url), self)
