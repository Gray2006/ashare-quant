# -*- coding: utf-8 -*-
"""复盘页:成绩单 + 纪律检查 + 按标的/按日归因。

只摆从订单能算出来的东西。订单表里没有策略名字段,所以「哪条策略赚的」
这一层不做——真想归因,得先让下单时把策略名写进 remark。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ..engine import review
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP

LEVEL_MARK = {"ok": ("✓ 通过", UP), "warn": ("! 注意", "#f0b90b"),
              "bad": ("✗ 违规", DOWN), "none": ("· 无样本", TEXT_MUT)}


class ReviewPage(QWidget):
    status = Signal(str)

    def __init__(self, gateway=None, parent=None):
        if gateway is not None and not isinstance(gateway, QWidget):
            parent, gw0 = parent if parent is not None else None, gateway
        else:
            gw0 = None
        super().__init__(parent)
        self.gw = gw0
        self._tasks: list[TaskRunner] = []
        self._rep: dict | None = None
        self._build()
        self._refresh_accounts()
        self.refresh()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("review", cols=[1.0], rows=[0.13, 0.10, 0.42, 0.35])
        root.addWidget(board)

        top = Board("review.top", cols=[0.16, 0.15, 0.15, 0.15, 0.15, 0.14, 0.10],
                    rows=[1.0])
        self.st_today = StatCard("当日盈亏", "--", "含浮动,权益差值")
        self.st_pnl = StatCard("已实现盈亏", "--", "近 90 天平仓")
        self.st_rate = StatCard("胜率", "--", "盈利笔数 / 平仓笔数")
        self.st_pf = StatCard("盈亏比", "--", "总盈利 / 总亏损")
        self.st_loss = StatCard("最大连亏", "--", "连续亏损的最长一段")
        self.st_fee = StatCard("手续费", "--", "含最低 5 元")
        for i, s in enumerate((self.st_today, self.st_pnl, self.st_rate,
                               self.st_pf, self.st_loss, self.st_fee)):
            top.add(s, 0, i, min_w=118, min_h=80)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        self.acc = QComboBox()
        self.acc.setMinimumWidth(110)
        self.acc.currentIndexChanged.connect(lambda *_: self.refresh())
        bl.addWidget(self.acc)
        self.btn = QPushButton("重算")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bl.addWidget(self.btn)
        self.btn_del = QPushButton("删除账户")
        self.btn_del.clicked.connect(self._delete_account)
        bl.addWidget(self.btn_del)
        top.add(box, 0, 6, min_w=110, min_h=80)
        board.add(top, 0, 0, min_h=84)

        verdict = Card("复盘结论")
        self.verdict = QLabel("取数中…")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet(f"color:{TEXT};font-size:13px;")
        verdict.add(self.verdict)
        self.sub = label("", dim=True, size=11)
        verdict.add(self.sub)
        board.add(verdict, 1, 0, min_h=62)

        mid = Board("review.mid", cols=[0.40, 0.30, 0.30], rows=[1.0])
        chk = Card("纪律检查", hint="只查订单能证明的事")
        self.chk_tbl = table(["项目", "结论", "说明"], widths=[92, 78, 320], stretch_col=2)
        self.chk_tbl.setObjectName("ReviewCheck")
        self.chk_tbl.cellClicked.connect(self._on_check)
        chk.add(self.chk_tbl, 1)
        self.chk_detail = label("点一行看触发明细", dim=True, size=11)
        self.chk_detail.setWordWrap(True)
        chk.add(self.chk_detail)
        mid.add(chk, 0, 0, min_w=420, min_h=190)

        byc = Card("按标的", hint="按已实现盈亏降序")
        self.code_tbl = table(["标的", "笔数", "盈亏", "胜率", "均持天"],
                              widths=[96, 48, 92, 54, 62], stretch_col=0)
        byc.add(self.code_tbl, 1)
        mid.add(byc, 0, 1, min_w=320, min_h=190)

        attr = Card("绩效归因", hint="卡玛 / 信息比率 / Brinson")
        self.attr_tbl = table(["项目", "值"], widths=[120, 100], stretch_col=0)
        attr.add(self.attr_tbl, 1)
        self.attr_msg = label("", dim=True, size=11)
        self.attr_msg.setWordWrap(True)
        attr.add(self.attr_msg)
        mid.add(attr, 0, 2, min_w=260, min_h=190)
        board.add(mid, 2, 0, min_h=190)

        bot = Board("review.bot", cols=[0.42, 0.58], rows=[1.0])
        byd = Card("按日")
        dbar = QHBoxLayout()
        dbar.setSpacing(6)
        self.day_hint = label("", dim=True)
        dbar.addWidget(self.day_hint, 1)
        self.btn_del_day = QPushButton("隐藏该日")
        self.btn_del_day.setToolTip("只从复盘显示里隐藏,订单和学习记录不动")
        self.btn_del_day.clicked.connect(self._hide_day)
        dbar.addWidget(self.btn_del_day)
        self.btn_show_all = QPushButton("恢复隐藏")
        self.btn_show_all.setToolTip("清空隐藏日期列表")
        self.btn_show_all.clicked.connect(self._show_all_days)
        dbar.addWidget(self.btn_show_all)
        byd.body().addLayout(dbar)
        self.day_tbl = table(["日期", "盈亏", "笔数", "累计"],
                             widths=[92, 92, 48, 92], stretch_col=0)
        byd.add(self.day_tbl, 1)
        bot.add(byd, 0, 0, min_w=300, min_h=140)

        full = Card("完整报告")
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        full.add(self.raw, 1)
        bot.add(full, 0, 1, min_w=380, min_h=140)
        board.add(bot, 3, 0, min_h=140)

    # ------------------------------------------------------------ 数据
    def _refresh_accounts(self) -> None:
        self.acc.blockSignals(True)
        cur = self.acc.currentData()
        self.acc.clear()
        self.acc.addItem("全部账户", None)
        try:
            from ..broker.sim import SimGateway
            gw = self.gw or SimGateway()
            for name in gw.list_accounts():
                self.acc.addItem(name, name)
        except Exception:  # noqa: BLE001
            pass
        if cur:
            i = self.acc.findData(cur)
            if i >= 0:
                self.acc.setCurrentIndex(i)
        self.acc.blockSignals(False)

    def _delete_account(self) -> None:
        name = self.acc.currentData()
        if not name:
            self.status.emit("先选一个具体账户")
            return
        if QMessageBox.question(
                self, "删除账户",
                f"删除模拟账户「{name}」及其持仓与委托记录?") != QMessageBox.Yes:
            return
        try:
            from ..broker.sim import SimGateway
            gw = self.gw or SimGateway()
            ok, msg = gw.delete_account(name)
            self.status.emit(msg)
            self._refresh_accounts()
            self.refresh()
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"删除失败:{e}")

    def refresh(self) -> None:
        self.btn.setEnabled(False)
        self.verdict.setText("读本地成交记录…")

        acc = self.acc.currentData() if hasattr(self, "acc") else None

        def job():
            from ..config import cfg
            from ..data import em
            from ..data.store import store
            # 取价用的标的必须来自「这一份复盘的持仓」,不能按账户名去猜。
            # 以前写成 load_positions(acc or "default") —— 「全部账户」时
            # 读的是一个不存在的 default 账户,持仓为 0,浮盈就恒等于 0。
            cols = store().sim_accounts()
            names = [a["name"] for a in cols] if acc is None else [acc]
            quotes = {}
            codes: list[str] = []
            for n in names:
                for p in store().load_positions(n):
                    c = str(p.get("code") or "")
                    if c and c not in codes:
                        codes.append(c)
            if codes:
                try:
                    quotes = em.quotes(codes) or {}
                except Exception:  # noqa: BLE001
                    quotes = {}
            hidden = list(cfg().get("review.hidden_days", []) or [])
            rep = review.build(account=acc, days=90, quotes=quotes,
                               hidden_days=hidden)
            return rep, quotes

        def ok(res):
            self.btn.setEnabled(True)
            rep, quotes = res if isinstance(res, tuple) else (res, {})
            self._rep = rep
            self._quotes = quotes
            self._render(rep)

        def err(e):
            self.btn.setEnabled(True)
            self.verdict.setText(f"复盘失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def refresh_now(self) -> None:
        self.refresh()

    def _render(self, rep: dict) -> None:
        self.verdict.setText(review.verdict(rep))
        if not rep.get("ok"):
            # 没有成交记录时也别留一张空表:六项纪律是「以后要守的规矩」,
            # 空着会让人以为页面坏了。表格照填,只把成绩单那几块置空。
            # 副标题只在和主结论不同的时候才显示,否则同一句话会印两遍。
            empty = (rep.get("empty") or "").strip()
            self.sub.setText("" if empty == self.verdict.text().strip() else empty)
            self.raw.setPlainText(review.to_text(rep))
            chk = rep.get("checks") or []
            self.chk_tbl.setRowCount(len(chk))
            for i, c in enumerate(chk):
                text, col = LEVEL_MARK.get(c["level"], ("·", TEXT_MUT))
                set_cell(self.chk_tbl, i, 0, c["name"], TEXT, bold=True, align="left")
                set_cell(self.chk_tbl, i, 1, text, col, align="left")
                set_cell(self.chk_tbl, i, 2, c["detail"], TEXT_DIM, align="left")
            self._checks = chk
            return
        s = rep["stat"]
        total = s.get("total_pnl", s["pnl"])
        if s.get("has_quotes"):
            hint = f"含浮盈 {s.get('unrealized', 0):+,.0f} · 合计 {total:+,.0f}"
        else:
            hint = "没取到行情,浮盈未计入"
        self.st_pnl.set(f"{s['pnl']:+,.0f}", pnl_color(s["pnl"]), hint=hint)
        self.st_rate.set("--" if s["win_rate"] is None else f"{s['win_rate']:.0f}%",
                         (UP if (s["win_rate"] or 0) >= 50 else DOWN)
                         if s["win_rate"] is not None else FLAT,
                         hint=f"盈 {s['wins']} / 亏 {s['losses']}")
        pf = s["profit_factor"]
        self.st_pf.set("--" if pf is None else f"{pf:.2f}",
                       (UP if (pf or 0) >= 1.5 else (DOWN if pf and pf < 1 else FLAT)),
                       hint="≥1.5 算健康")
        self.st_loss.set(f"{s['max_consec_loss']}", None,
                         hint=f"均持 {s['avg_days']:.1f} 天" if s["avg_days"] is not None else "")
        self.st_fee.set(f"{s['fees']:,.0f}", TEXT_DIM,
                        hint=f"成交额 {s['turnover'] / 10000:.0f} 万")

        # 当日盈亏:含持仓浮动,和上面「已实现」是两个口径。
        # 已实现看策略能力,当日盈亏看账户今天到底赚没赚。
        extra = ""
        try:
            from ..engine import daily_review as dr
            acc = rep.get("account")
            acc = None if (not acc or acc == "全部账户") else acc
            pnl = dr.report(acc, quotes=getattr(self, "_quotes", None))
            t = (pnl.get("pnl") or {}).get("total")
            self.st_today.set(
                "--" if t is None else f"{t:+,.0f}",
                pnl_color(t) if t is not None else FLAT,
                hint="含浮动" if t is not None else "缺快照")
            extra = dr.render(pnl)
        except Exception:  # noqa: BLE001
            self.st_today.set("--", FLAT, hint="算不出来")

        sub = f"{rep['account']} · 近 {rep['days']} 天 · {rep['asof']}"
        if s["trades"] and s["win_rate"] == 100:
            sub += " · 胜率 100% 多半是自检数据(同一只票反复买卖),别当业绩看"
        if rep.get("dropped"):
            sub += f" · 已跳过账户重建前的 {rep['dropped']} 笔旧成交"
        self.sub.setText(sub)

        chk = rep["checks"]
        self.chk_tbl.setRowCount(len(chk))
        for i, c in enumerate(chk):
            text, col = LEVEL_MARK.get(c["level"], ("·", TEXT_MUT))
            set_cell(self.chk_tbl, i, 0, c["name"], TEXT, bold=True, align="left")
            set_cell(self.chk_tbl, i, 1, text, col, align="left")
            set_cell(self.chk_tbl, i, 2, c["detail"], TEXT_DIM, align="left")
        self._checks = chk

        rows = s["by_code"]
        self.code_tbl.setRowCount(len(rows))
        for i, b in enumerate(rows):
            set_cell(self.code_tbl, i, 0, b["name"] or b["code"], align="left")
            set_cell(self.code_tbl, i, 1, str(b["n"]))
            set_cell(self.code_tbl, i, 2, f"{b['pnl']:+,.0f}", pnl_color(b["pnl"]))
            set_cell(self.code_tbl, i, 3, f"{b['win'] / b['n'] * 100:.0f}%")
            set_cell(self.code_tbl, i, 4, f"{b['days'] / b['n']:.1f}")

        days = s["by_day"]
        self._day_rows = days
        self.day_tbl.setRowCount(len(days))
        for i, d in enumerate(days):
            set_cell(self.day_tbl, i, 0, d["day"], align="left")
            set_cell(self.day_tbl, i, 1, f"{d['pnl']:+,.0f}", pnl_color(d["pnl"]))
            set_cell(self.day_tbl, i, 2, str(d["n"]))
            set_cell(self.day_tbl, i, 3, f"{d.get('cum', 0):+,.0f}",
                     pnl_color(d.get("cum", 0)))

        # 绩效归因:卡玛(用按日累计权益)
        try:
            days = s.get("by_day") or []
            if len(days) >= 5:
                eq = [1_000_000 + float(d.get("cum", 0) or 0) for d in days]
                rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))
                        if eq[i - 1] > 0]
                ann = (sum(rets) / len(rets) * 252) if rets else 0.0
                peak, mdd = eq[0], 0.0
                for v in eq:
                    peak = max(peak, v)
                    if peak > 0:
                        mdd = max(mdd, (peak - v) / peak)
                calmar = ann / mdd if mdd > 0 else 0.0
                self.attr_tbl.setRowCount(2)
                set_cell(self.attr_tbl, 0, 0, "卡玛比率", align="left")
                set_cell(self.attr_tbl, 0, 1, f"{calmar:.2f}")
                set_cell(self.attr_tbl, 1, 0, "最大回撤", align="left")
                set_cell(self.attr_tbl, 1, 1, f"{mdd:.1%}")
                self.attr_msg.setText("信息比率/Brinson 需要基准和行业数据,暂未接入")
            else:
                self.attr_tbl.setRowCount(0)
                self.attr_msg.setText("样本不足,至少 5 个交易日")
        except Exception:  # noqa: BLE001
            pass

        self.raw.setPlainText((extra + "\n\n" + review.to_text(rep))
                              if extra else review.to_text(rep))
        self.status.emit(review.verdict(rep))

    def _hide_day(self) -> None:
        row = self.day_tbl.currentRow()
        days = getattr(self, "_day_rows", [])
        if row < 0 or row >= len(days):
            self.status.emit("先选中一行日期")
            return
        day = days[row]["day"]
        from ..config import cfg
        from ..engine import review
        c = cfg()
        hidden = list(c.get("review.hidden_days", []) or [])
        if day not in hidden:
            hidden.append(day)
        c.set("review.hidden_days", hidden[-200:], save=True)
        self.status.emit(f"已隐藏 {day}(订单未删,学习不受影响)")
        # 走 refresh 而不是就地重算:重算要重新取价,不然浮盈会被算成 0
        self.refresh()

    def _show_all_days(self) -> None:
        from ..config import cfg
        c = cfg()
        c.set("review.hidden_days", [], save=True)
        self.status.emit("已恢复全部隐藏日期")
        self.refresh()

    def _on_check(self, row: int, _col: int) -> None:
        checks = getattr(self, "_checks", [])
        if row >= len(checks):
            return
        c = checks[row]
        if c["items"]:
            self.chk_detail.setText(c["advice"] + " —— " + ";".join(c["items"][:6]))
        else:
            self.chk_detail.setText(c["advice"] or "这条没发现问题")

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
