# -*- coding: utf-8 -*-
"""交易页:账户 / 持仓 / 下单(带风控) / 委托记录 / 网关切换。"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..broker.risk import RiskEngine
from ..config import cfg
from ..engine.hub import TaskRunner
from ..engine.models import SIDE_BUY, SIDE_SELL
from ..utils import fmt_price, normalize_code
from .board import Board
from .common import Card, label, pnl_color, set_cell, table, watch_codes
from .theme import DOWN, FLAT, TEXT_DIM, UP, WARN


class TradePage(QWidget):
    status = Signal(str)

    COLS = ["代码", "名称", "持仓", "可卖", "成本", "现价", "市值", "盈亏", "盈亏%"]

    def __init__(self, gateway=None, parent=None):
        # main_window 现在按 NAV 统一用 cls(self.gw) 注入网关,
        # 这里必须收下第一个位置参数,否则会把 SimGateway 当成 parent 传进 QWidget
        if gateway is not None and not isinstance(gateway, QWidget):
            parent, gw0 = parent if parent is not None else None, gateway
        else:
            gw0 = None
        super().__init__(parent)
        self.cfg = cfg()
        self.risk = RiskEngine()
        self.gw = gw0
        self.quotes: dict[str, dict] = {}
        self._tasks: list[TaskRunner] = []
        self._tick_n = 0
        self._build()
        if self.gw is not None:
            self.refresh_gw_state()
            self._reload_accounts()
            self.refresh_all()
        # 持仓/账户自己走一条心跳:即使该票不在自选里、没有行情推送,
        # 也能定期重算市值和盈亏(价格仍以最近一次行情为准,不会编造)
        self._live = QTimer(self)
        self._live.setInterval(2000)
        self._live.timeout.connect(self._auto_refresh)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("trade", cols=[0.62, 0.38], rows=[1.0])
        root.addWidget(board)

        # 左:账户 / 持仓 / 委托,三块各一条可拖分栏线
        left = Board("trade.left", cols=[1.0], rows=[0.30, 0.42, 0.28])
        self.acct_card = Card("账户")
        row = QHBoxLayout()
        self.a_total = self._big("总资产")
        self.a_cash = self._big("可用资金")
        self.a_mv = self._big("持仓市值")
        self.a_pnl = self._big("浮动盈亏")
        for w in (self.a_total, self.a_cash, self.a_mv, self.a_pnl):
            row.addWidget(w)
        self.acct_card._lay.addLayout(row)
        acc_row = QHBoxLayout()
        acc_row.setSpacing(8)
        self.acct_combo = QComboBox()
        self.acct_combo.setMinimumWidth(150)
        self.acct_combo.currentIndexChanged.connect(self._on_account_changed)
        acc_row.addWidget(QLabel("模拟账户"))
        acc_row.addWidget(self.acct_combo, 1)
        self.btn_new_acct = QPushButton("新建")
        self.btn_new_acct.clicked.connect(self._new_account)
        self.btn_del_acct = QPushButton("删除")
        self.btn_del_acct.clicked.connect(self._del_account)
        acc_row.addWidget(self.btn_new_acct)
        acc_row.addWidget(self.btn_del_acct)
        self.acct_card._lay.addLayout(acc_row)
        self.gw_label = QLabel("")
        self.gw_label.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.acct_card.add(self.gw_label)
        left.add(self.acct_card, 0, 0, min_h=150)

        pos_card = Card("持仓")
        self.pos_card = pos_card
        self.pos_tbl = table(self.COLS, widths=[62, 80, 62, 58, 66, 66, 84, 80, 64], stretch_col=1)
        self.pos_tbl.itemSelectionChanged.connect(self._on_pick_position)
        pos_card.add(self.pos_tbl, 1)
        left.add(pos_card, 1, 0, min_h=170)

        order_card = Card("委托记录")
        obar = QHBoxLayout()
        obar.setSpacing(8)
        self.ord_hint = label("", dim=True)
        obar.addWidget(self.ord_hint, 1)
        self.btn_cancel = QPushButton("撤单")
        self.btn_cancel.setToolTip("撤掉选中的未成交委托")
        self.btn_cancel.clicked.connect(self._cancel_order)
        obar.addWidget(self.btn_cancel)
        order_card.body().addLayout(obar)
        self.ord_tbl = table(["时间", "代码", "方向", "价格", "数量", "状态", "说明"],
                             widths=[112, 68, 52, 76, 64, 76, 240], stretch_col=6)
        order_card.add(self.ord_tbl, 1)
        left.add(order_card, 2, 0, min_h=130)

        board.add(left, 0, 0, min_w=560)

        # 右:下单面板 + 通道
        right = Board("trade.right", cols=[1.0], rows=[0.63, 0.37])
        panel = Card("下单")
        form = QVBoxLayout()
        form.setSpacing(8)

        self.code = QComboBox()
        self.code.setEditable(True)
        self.code.setInsertPolicy(QComboBox.NoInsert)
        self.code.lineEdit().setPlaceholderText("自选或输入代码,回车查价")
        for _c in watch_codes(40):
            self.code.addItem(_c, _c)
        self.code.lineEdit().returnPressed.connect(self.lookup)
        form.addLayout(self._row("股票", self.code))

        self.name_label = QLabel("--")
        self.name_label.setStyleSheet(f"color:{TEXT_DIM};")
        form.addLayout(self._row("名称/现价", self.name_label))

        self.price = QDoubleSpinBox()
        self.price.setRange(0, 100000)
        self.price.setDecimals(2)
        self.price.setSingleStep(0.01)
        form.addLayout(self._row("委托价", self.price))

        self.qty = QSpinBox()
        self.qty.setRange(0, 1000000)
        self.qty.setSingleStep(100)
        self.qty.setValue(100)
        form.addLayout(self._row("数量", self.qty))

        self.amount_label = QLabel("≈ 0 元")
        self.amount_label.setStyleSheet(f"color:{TEXT_DIM};")
        form.addLayout(self._row("金额", self.amount_label))
        self.qty.valueChanged.connect(self._update_amount)
        self.price.valueChanged.connect(self._update_amount)

        btns = QHBoxLayout()
        self.btn_buy = QPushButton("买入")
        self.btn_buy.setObjectName("Danger")
        self.btn_buy.clicked.connect(lambda: self.submit(SIDE_BUY))
        self.btn_sell = QPushButton("卖出")
        self.btn_sell.setObjectName("Success")
        self.btn_sell.clicked.connect(lambda: self.submit(SIDE_SELL))
        btns.addWidget(self.btn_buy)
        btns.addWidget(self.btn_sell)
        form.addLayout(btns)

        self.risk_label = QLabel("")
        self.risk_label.setWordWrap(True)
        self.risk_label.setStyleSheet(f"color:{WARN};font-size:12px;")
        form.addWidget(self.risk_label)
        panel._lay.addLayout(form)
        right.add(panel, 0, 0, min_h=320)

        gw_card = Card("交易通道")
        self.gw_combo = QComboBox()
        self.gw_combo.addItem("模拟盘(本地撮合)", "sim")
        self.gw_combo.addItem("同花顺(界面自动下单)", "ths")
        self.gw_combo.currentIndexChanged.connect(lambda *_: self._on_gw_changed())
        gw_card.add(self.gw_combo)
        self.dry_run = QPushButton("")
        self.dry_run.clicked.connect(self._toggle_dry)
        gw_card.add(self.dry_run)
        self.btn_connect = QPushButton("连接/刷新")
        self.btn_connect.clicked.connect(self.reconnect)
        gw_card.add(self.btn_connect)
        self.btn_reset = QPushButton("重置模拟盘")
        self.btn_reset.clicked.connect(self._reset_sim)
        gw_card.add(self.btn_reset)
        gw_info = QLabel("同花顺网关:先跑 tools/probe_ths.py 校准控件,"
                         "并把 broker.ths.dry_run 保持 true 试跑。")
        gw_info.setWordWrap(True)
        gw_info.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        gw_card.add(gw_info)
        right.add(gw_card, 1, 0, min_h=130)
        board.add(right, 0, 1, min_w=330)
        self.refresh_gw_state()

    # ------------------------------------------------------------ 小工具
    def _row(self, text: str, w: QWidget) -> QHBoxLayout:
        lay = QHBoxLayout()
        lab = QLabel(text)
        lab.setFixedWidth(64)
        lab.setStyleSheet(f"color:{TEXT_DIM};")
        lay.addWidget(lab)
        lay.addWidget(w, 1)
        return lay

    def _big(self, title: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        v = QLabel("-")
        v.setObjectName("BigNum")
        t = QLabel(title)
        t.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        lay.addWidget(v)
        lay.addWidget(t)
        w.val = v
        return w

    # ------------------------------------------------------------ 网关
    def set_gateway(self, gw) -> None:
        self.gw = gw
        self.refresh_all()

    def _on_gw_changed(self) -> None:
        self.status.emit("切换通道后请点「连接/刷新」")
        self.refresh_gw_state()

    def refresh_gw_state(self) -> None:
        is_ths = self.gw_combo.currentData() == "ths"
        self.dry_run.setVisible(is_ths)
        if is_ths:
            dry = bool(self.cfg.get("broker.ths.dry_run", True))
            self.dry_run.setText(f"演练模式(dry_run):{'开' if dry else '关 —— 会真下单!'}")
            self.dry_run.setObjectName("Primary" if dry else "Danger")
            self.dry_run.setStyleSheet("")
            self.dry_run.setToolTip("点击切换。关闭后确认键会真的发到同花顺。")

    def _toggle_dry(self) -> None:
        if self.gw_combo.currentData() != "ths":
            return
        cur = bool(self.cfg.get("broker.ths.dry_run", True))
        if cur:
            ret = QMessageBox.warning(
                self, "关闭演练模式",
                "关闭后点击「买入/卖出」会真的在同花顺里提交委托。\n确定要关闭吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
        self.cfg.set("broker.ths.dry_run", not cur, save=True)
        self.refresh_gw_state()

    def reconnect(self) -> None:
        kind = self.gw_combo.currentData()
        from ..broker.sim import SimGateway
        from ..broker.ths import THSGateway

        def job():
            gw = SimGateway() if kind == "sim" else THSGateway()
            ok, msg = gw.connect()
            return gw, ok, msg

        def on_ok(pack):
            gw, ok, msg = pack
            self.gw = gw
            self.status.emit(msg)
            self.gw_label.setText(("已连接 · " if ok else "未连接 · ") + msg)
            self.refresh_all()

        self._run(job, on_ok, "connect")

    def _reset_sim(self) -> None:
        if not self.gw or self.gw.name != "sim":
            return
        ret = QMessageBox.question(self, "重置模拟盘", "清空持仓并把资金恢复到初始值?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret == QMessageBox.Yes:
            self.gw.reset()
            self.refresh_all()
            self.status.emit("模拟盘已重置")

    # ------------------------------------------------------------ 刷新
    def refresh_all(self) -> None:
        if not self.gw:
            return
        self._reload_accounts()
        self._refresh_account()
        self._refresh_positions()
        self._refresh_orders()

    def update_quotes(self, quotes: dict) -> None:
        self.quotes.update(quotes)
        if self.gw and hasattr(self.gw, "set_mark_prices"):
            self.gw.set_mark_prices(self.quotes)
        self._refresh_account()
        self._refresh_positions()

    # ------------------------------------------------------------ 心跳
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._live.start()
        self._auto_refresh(force=True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._live.stop()

    def _auto_refresh(self, force: bool = False) -> None:
        """持仓/市值定期重算;委托记录低频些,别每秒扫库。"""
        if not self.gw:
            return
        self._tick_n += 1
        self._refresh_account()
        self._refresh_positions()
        if force or self._tick_n % 3 == 1:
            self._refresh_orders()
        # 组合层面的持仓只数/盈亏汇总,标题栏给个一眼能读的数
        try:
            poss = self.gw.positions()
        except Exception:
            poss = []
        if poss:
            mv = sum((p.last_price or p.cost) * p.qty for p in poss)
            pnl = sum(((p.last_price or p.cost) - p.cost) * p.qty for p in poss)
            self.pos_card.set_hint(f"{len(poss)} 只 · 市值 {mv:,.0f} · "
                                   f"浮动 {pnl:+,.0f}")
        else:
            self.pos_card.set_hint("空仓")

    def _refresh_account(self) -> None:
        if not self.gw:
            return
        try:
            a = self.gw.account()
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"读取账户失败:{e}")
            return
        self.a_total.val.setText(f"{a.get('total', 0):,.0f}")
        self.a_cash.val.setText(f"{a.get('cash', 0):,.0f}")
        self.a_mv.val.setText(f"{a.get('market_value', 0):,.0f}")
        pnl = a.get("pnl") or 0
        self.a_pnl.val.setText(f"{pnl:+,.0f}")
        self.a_pnl.val.setStyleSheet(f"color:{pnl_color(pnl)};")
        name = getattr(self.gw, "display_name", "")
        self.gw_label.setText(f"通道:{name}  ·  "
                              f"浮动 {a.get('pnl_pct', 0):+.2f}%"
                              + (f"  ·  {a.get('note')}" if a.get("note") else ""))

    def _refresh_positions(self) -> None:
        if not self.gw:
            return
        try:
            poss = self.gw.positions()
        except Exception:
            return
        if self.pos_tbl.rowCount() != len(poss):
            self.pos_tbl.setRowCount(len(poss))
        for i, p in enumerate(poss):
            q = self.quotes.get(p.code) or {}
            px = q.get("price")
            live = bool(px)
            if live:
                last = float(px)
            else:
                last = p.last_price or p.cost
            p.last_price = last
            mv = last * p.qty
            pnl = (last - p.cost) * p.qty
            pct = (last / p.cost - 1) * 100 if p.cost else 0.0
            set_cell(self.pos_tbl, i, 0, p.code, align="left")
            set_cell(self.pos_tbl, i, 1, p.name or q.get("name") or "", align="left")
            set_cell(self.pos_tbl, i, 2, str(p.qty))
            set_cell(self.pos_tbl, i, 3, str(p.avail_qty))
            set_cell(self.pos_tbl, i, 4, fmt_price(p.cost, 3))
            # 没拿到行情时用灰色,别让「现价」看着像实时价
            set_cell(self.pos_tbl, i, 5, fmt_price(last), None if live else FLAT)
            set_cell(self.pos_tbl, i, 6, f"{mv:,.0f}")
            set_cell(self.pos_tbl, i, 7, f"{pnl:+,.0f}", pnl_color(pnl), bold=True)
            set_cell(self.pos_tbl, i, 8, f"{pct:+.2f}%", pnl_color(pnl) if live else FLAT)

    def _refresh_orders(self) -> None:
        from ..data.store import store
        acct = getattr(self.gw, "account_name", None) if self.gw else None
        rows = store().orders(60, account=acct if self.gw and self.gw.name == "sim" else None)
        # 加密订单在「加密」页单独展示,这里只留 A 股/同花顺的单子
        rows = [r for r in rows if (r.get("gateway") or "sim") != "okx"]
        self._ord_ids = [int(r["id"]) for r in rows]
        self.ord_tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            ts = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
            set_cell(self.ord_tbl, i, 0, ts)
            set_cell(self.ord_tbl, i, 1, r["code"], align="left")
            side = "买入" if r["side"] == "buy" else "卖出"
            set_cell(self.ord_tbl, i, 2, side, UP if r["side"] == "buy" else DOWN)
            set_cell(self.ord_tbl, i, 3, f"{r['price']:.2f}")
            set_cell(self.ord_tbl, i, 4, str(r["qty"]))
            st = {"filled": "已成交", "rejected": "被拒", "submitted": "已提交",
                  "partial": "部分成交", "cancelled": "已撤单",
                  "unverified": "待核对", "new": "新建"}.get(r["status"], r["status"])
            set_cell(self.ord_tbl, i, 5, st, TEXT_DIM)
            set_cell(self.ord_tbl, i, 6, (r.get("remark") or "")[:70], align="left")
        self.ord_hint.setText(f"{len(rows)} 笔 · 选中未成交的可撤单")

    def _cancel_order(self) -> None:
        row = self.ord_tbl.currentRow()
        if row < 0 or row >= len(getattr(self, "_ord_ids", [])):
            self.status.emit("先选中一笔委托")
            return
        if not self.gw:
            self.status.emit("没有交易网关")
            return
        oid = int(self._ord_ids[row])
        ok, msg = self.gw.cancel_order(oid)
        self.status.emit(("撤单成功:" if ok else "撤单失败:") + msg)
        self._refresh_orders()

    # ------------------------------------------------------------ 下单
    def lookup(self) -> None:
        code = normalize_code(self.code.currentText())
        if len(code) != 6:
            self.status.emit("请输入 6 位代码")
            return
        self.code.setCurrentText(code)

        def job():
            from ..data import em
            return em.quote(code)

        def on_ok(q):
            if not q:
                self.name_label.setText("查不到行情")
                return
            self.quotes[code] = q
            self.name_label.setText(f"{q.get('name')}  {fmt_price(q.get('price'))}  "
                                    f"{q.get('change_pct', 0):+.2f}%")
            if self.price.value() == 0:
                self.price.setValue(q.get("price") or 0)
            self._update_amount()
            self._precheck()

        self._run(job, on_ok, "quote")

    def _update_amount(self) -> None:
        amt = self.price.value() * self.qty.value()
        self.amount_label.setText(f"≈ {amt:,.2f} 元")

    def _on_pick_position(self) -> None:
        rows = self.pos_tbl.selectionModel().selectedRows() if self.pos_tbl.selectionModel() else []
        if not rows:
            return
        i = rows[0].row()
        code_item = self.pos_tbl.item(i, 0)
        qty_item = self.pos_tbl.item(i, 3)
        if not code_item:
            return
        self.code.setCurrentText(code_item.text())
        if qty_item:
            try:
                self.qty.setValue(int(qty_item.text()))
            except ValueError:
                pass
        self.lookup()

    def _precheck(self) -> None:
        if not self.gw:
            return
        code = normalize_code(self.code.currentText())
        q = self.quotes.get(code)
        acct = self.gw.account()
        decision = self.risk.check_order(
            code, SIDE_BUY, self.price.value() or 1, self.qty.value() or 100,
            quote=q, positions=self.gw.positions(),
            daily_orders=self._daily_orders(), account=acct,
            live=getattr(self.gw, "is_live", True))
        self.risk_label.setText("" if decision.ok else "⚠ " + decision.reason)

    def _daily_orders(self) -> int:
        from ..data.store import store
        acct = getattr(self.gw, "account_name", None) if self.gw else None
        return store().orders_today_count(account=acct if self.gw and self.gw.name == "sim" else None)

    # ------------------------------------------------------------ 模拟账户
    def _reload_accounts(self, select: str | None = None) -> None:
        if not self.gw or self.gw.name != "sim":
            self.acct_combo.setEnabled(False)
            self.btn_new_acct.setEnabled(False)
            self.btn_del_acct.setEnabled(False)
            return
        self.acct_combo.setEnabled(True)
        self.btn_new_acct.setEnabled(True)
        self.btn_del_acct.setEnabled(True)
        names = self.gw.list_accounts()
        cur = select or getattr(self.gw, "account_name", None)
        self.acct_combo.blockSignals(True)
        self.acct_combo.clear()
        self.acct_combo.addItems(names)
        if cur in names:
            self.acct_combo.setCurrentText(cur)
        self.acct_combo.blockSignals(False)

    def _on_account_changed(self, _idx: int) -> None:
        if not self.gw or self.gw.name != "sim":
            return
        name = self.acct_combo.currentText()
        if not name or name == getattr(self.gw, "account_name", None):
            return
        _ok, msg = self.gw.switch_account(name)
        self.status.emit(msg)
        self.refresh_all()

    def _new_account(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        if not self.gw or self.gw.name != "sim":
            self.status.emit("只有模拟盘支持多账户")
            return
        name, ok = QInputDialog.getText(self, "新建模拟账户", "账户名(如:打板测试):")
        if not ok or not name.strip():
            return
        cash, ok2 = QInputDialog.getDouble(self, "初始资金", "初始资金(元):",
                                          1000000.0, 1000.0, 1e9, 0)
        if not ok2:
            return
        ok3, msg = self.gw.create_account(name.strip(), cash)
        self.status.emit(msg)
        if ok3:
            self.gw.switch_account(name.strip())
            self._reload_accounts(name.strip())
            self.refresh_all()

    def _del_account(self) -> None:
        if not self.gw or self.gw.name != "sim":
            return
        name = self.acct_combo.currentText()
        ret = QMessageBox.question(self, "删除账户",
                                  f"删除模拟账户「{name}」及其持仓与委托记录?",
                                  QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        ok, msg = self.gw.delete_account(name)
        self.status.emit(msg)
        if ok:
            self._reload_accounts()
            self.refresh_all()

    def submit(self, side: str) -> None:
        if not self.gw:
            self.status.emit("没有可用交易通道,先点「连接/刷新」")
            return
        code = normalize_code(self.code.currentText())
        price = round(self.price.value(), 2)
        qty = int(self.qty.value())
        if len(code) != 6:
            self.status.emit("请输入 6 位股票代码")
            return
        q = self.quotes.get(code)
        acct = self.gw.account()
        decision = self.risk.check_order(
            code, side, price, qty, quote=q, positions=self.gw.positions(),
            daily_orders=self._daily_orders(), account=acct,
            live=getattr(self.gw, "is_live", True))
        if not decision.ok:
            self.risk_label.setText("⚠ " + decision.reason)
            self.status.emit("风控拦截:" + decision.reason)
            return

        name = (q or {}).get("name", "")
        amt = price * qty
        if self.gw.is_live and not bool(self.cfg.get("broker.ths.dry_run", True)):
            ret = QMessageBox.warning(
                self, "确认下单",
                f"通道:{self.gw.display_name}\n"
                f"{'买入' if side == SIDE_BUY else '卖出'} {code} {name}\n"
                f"价格 {price:.2f} × {qty} 股 ≈ {amt:,.0f} 元\n\n"
                f"这会真的提交到同花顺,确认吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                return

        def job():
            return self.gw.place_order(code, side, price, qty, name)

        def on_ok(order):
            self.risk_label.setText("")
            tip = f"{'买入' if side == SIDE_BUY else '卖出'} {code} {qty}股 @ {price:.2f} → {order.status}"
            if order.remark:
                tip += f"({order.remark})"
            self.status.emit(tip)
            self.refresh_all()

        self._run(job, on_ok, "order")

    def _run(self, fn, on_ok, name=""):
        if name:
            for t in list(self._tasks):
                if getattr(t, "task_name", name) == name and t.isRunning():
                    return
        t = TaskRunner(fn, name=name, parent=self)
        t.task_name = name
        t.succeeded.connect(on_ok)
        t.failed.connect(lambda m: self.status.emit(m.splitlines()[0][:200]))
        self._tasks.append(t)
        t.finished.connect(lambda: self._tasks.remove(t) if t in self._tasks else None)
        t.start()
