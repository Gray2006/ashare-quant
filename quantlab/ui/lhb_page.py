# -*- coding: utf-8 -*-
"""龙虎榜页:某日上榜个股 + 选中个股的买卖席位明细。

数据在 data/lhb.py(东财 datacenter)。非交易日接口返回空,页面必须说清楚原因,
不能让用户以为软件坏了 —— 所以空数据时显示 empty_hint() 的原文。

日期默认取最近 5 个工作日;若所选日接口没数据(节假日/尚未更新),自动回退到
更早的有数据的一天,并在提示里写明「实际显示的是哪一天」,不偷偷改日期。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QPushButton, QVBoxLayout,
                               QWidget)

from ..data import lhb
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, label, pnl_color, set_cell, table
from .theme import DOWN, TEXT, TEXT_DIM, UP

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
        # 弹窗要留引用:只 show() 不持有的话,Python 侧对象被回收,
        # 窗口会一闪而过(表现为「点了没反应」)。
        self._dlgs: list = []
        self._build()
        QTimer.singleShot(800, self.refresh)
        QTimer.singleShot(1400, self._load_hot)

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

        # 2×2:上面票与席位(看盘主区),下面游资名单与席位胜率(研究区)。
        # 原来是四块竖着堆,940 高的屏每块只剩一两行可看。
        board = Board("lhb", cols=[0.56, 0.44], rows=[0.56, 0.44])

        list_card = Card("上榜个股")
        self.empty = label("", dim=True)
        self.empty.setWordWrap(True)
        self.empty.hide()
        list_card.add(self.empty)
        self.tbl = table(
            ["代码", "名称", "收盘", "涨跌幅", "上榜原因", "净买入(万)"],
            widths=[70, 96, 70, 88, 440, 104], stretch_col=4)
        self.tbl.setObjectName("LhbBoard")
        self.tbl.itemSelectionChanged.connect(self._on_select)
        list_card.add(self.tbl, 1)
        board.add(list_card, 0, 0, min_h=260)
        self.list_card = list_card

        self.seat_card = Card("席位明细", hint="买/卖同一营业部已合并,净额=买-卖")
        self.seat_hint = label("先在上面选一只票", dim=True)
        self.seat_card.add(self.seat_hint)
        self.seat_tbl = table(
            ["营业部", "买入额(万)", "卖出额(万)", "净额(万)"],
            widths=[420, 108, 108, 108], stretch_col=0)
        self.seat_tbl.setObjectName("LhbSeats")
        self.seat_card.add(self.seat_tbl, 1)
        board.add(self.seat_card, 0, 1, min_h=260)

        # 游资手法:今日名单(净额为主,高胜率席位加成) + 席位胜率。
        # 以前只有一张胜率表、预测和验证从没跑过,准确率永远 0/0 —— 现在一键闭环。
        self.hot_card = Card("游资手法 · 今日名单")
        hbar = QHBoxLayout()
        hbar.setSpacing(8)
        self.hot_hint = label("", dim=True)
        self.hot_hint.setWordWrap(False)      # 折行会挤掉下面的表格
        hbar.addWidget(self.hot_hint, 1)
        self.hot_btn = QPushButton("同步")
        self.hot_btn.setToolTip("采集最近 30 天龙虎榜 → 回填收益 → 生成预测 → "
                                "验证准确率 → 刷新席位胜率,约几分钟")
        self.hot_btn.clicked.connect(self._sync_hot)
        hbar.addWidget(self.hot_btn)
        self.filter_btn = QPushButton("负面清单")
        self.filter_btn.setToolTip("哪些票该躲:大额净买入 + 60 日区间高位")
        self.filter_btn.clicked.connect(self._open_filter)
        hbar.addWidget(self.filter_btn)
        self.hot_card.body().addLayout(hbar)
        self.pred_tbl = table(["代码", "名称", "方向", "分数", "理由"],
                              widths=[80, 120, 60, 80, 460], stretch_col=4)
        self.pred_tbl.setObjectName("LhbPred")
        self.hot_card.add(self.pred_tbl, 1)
        board.add(self.hot_card, 1, 0, min_h=200)

        self.stat_card = Card("席位胜率", hint="买入后 5 日 · 贝叶斯平滑")
        self.stat_hint = label("还没同步,点上面的「同步」", dim=True)
        self.stat_card.add(self.stat_hint)
        self.hot_tbl = table(
            ["席位", "样本", "5日均收益", "胜率", "净买入(亿)"],
            widths=[420, 60, 96, 70, 96], stretch_col=0)
        self.hot_tbl.setObjectName("LhbHot")
        self.hot_tbl.setToolTip("双击一行看席位画像:买什么、怎么买、跟买有没有用")
        self.hot_tbl.doubleClicked.connect(lambda *_: self._open_seat())
        self.stat_card.add(self.hot_tbl, 1)
        board.add(self.stat_card, 1, 1, min_h=200)

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
    def _load_hot(self):
        """只读缓存,不联网:进页面先看到上次同步的结果。"""
        def job():
            from ..engine import hotmoney as hm
            return (hm.coverage(90), hm.accuracy(90), hm.walk_forward(60, 40),
                    hm.seat_stats(3, 60), hm.predict_day(""))

        r = TaskRunner(job)
        r.succeeded.connect(lambda res: self._render_hot(*res))
        r.failed.connect(lambda m: self.hot_hint.setText(f"读游资缓存失败:{str(m)[:60]}"))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _sync_hot(self):
        self.hot_btn.setEnabled(False)
        self.hot_hint.setText("同步中:采集 → 回填 → 预测 → 验证,约几分钟…")

        def job():
            from ..engine import hotmoney as hm
            out = hm.sync(days=30, max_stocks=8)
            return (out, hm.coverage(90), hm.accuracy(90), hm.walk_forward(60, 40),
                    hm.seat_stats(3, 60), hm.predict_day(""))

        def ok(res):
            out, cov, acc, wf, stats, preds = res
            self._render_hot(cov, acc, wf, stats, preds)
            self.hot_hint.setText(
                f"同步完成:新增 {out['collect']['rows']} 条 · 回填 {out['filled']} · "
                f"预测 {out['predict_rows']} 条 · 验证 {out['review']['reviewed']} 条")

        def fail(m):
            self.hot_hint.setText(f"同步失败:{str(m)[:90]}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(fail)
        r.finished.connect(lambda: self.hot_btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _open_filter(self):
        from .hotfilter_dialog import HotFilterDialog
        dlg = HotFilterDialog(self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dlgs.append(dlg)
        dlg.finished.connect(lambda *_: self._dlgs.remove(dlg)
                             if dlg in self._dlgs else None)
        dlg.show()

    def _open_seat(self):
        row = self.hot_tbl.currentRow()
        if row < 0:
            return
        item = self.hot_tbl.item(row, 0)
        seat = item.text() if item is not None else ""
        if not seat:
            return
        from .seat_dialog import SeatDialog
        dlg = SeatDialog(seat, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        self._dlgs.append(dlg)
        dlg.finished.connect(lambda *_: self._dlgs.remove(dlg)
                             if dlg in self._dlgs else None)
        dlg.show()

    def _render_hot(self, cov: dict, acc: dict, wf: dict, stats: list, preds: list):
        # 状态行:一行说清「数据多少 · 预测准不准 · 样本外站不站得住」。
        # 卡片只有半屏宽,长句会折行把表格挤掉,所以只留结论不留解释。
        parts = []
        if cov.get("n"):
            parts.append(f"{cov['lo'][5:]}~{cov['hi'][5:]} · {cov['n']} 条")
        else:
            parts.append("无席位数据")
        if acc.get("n"):
            parts.append(f"预测 {acc['n']} 条 {acc['acc']:.0%}")
            if acc["acc"] < 0.45:
                parts.append("(反向)")
        else:
            parts.append("预测 0 条")
        if wf.get("hot_seats"):
            parts.append(f"样本外 {wf['train_avg']:+.1%}→{wf['test_avg']:+.1%}"
                         + (" 失效" if wf.get("overfit") else ""))
        # 采集脚本攒够可验证天数会落这个标记,提醒该重跑验证了
        try:
            from pathlib import Path
            flag = Path(__file__).resolve().parents[2] / "reports" / "lhb-ready.flag"
            if flag.exists():
                parts.append("★ 可验证天数已够,建议重跑反向验证")
        except Exception:  # noqa: BLE001
            pass
        self.hot_hint.setText(" · ".join(parts))

        # 今日名单
        self.pred_tbl.setRowCount(len(preds))
        for i, p in enumerate(preds):
            d = p.get("direction") or "flat"
            dc = UP if d == "up" else (DOWN if d == "down" else TEXT_DIM)
            set_cell(self.pred_tbl, i, 0, p.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.pred_tbl, i, 1, p.get("name", ""), TEXT, align="left")
            set_cell(self.pred_tbl, i, 2, {"up": "看涨", "down": "看跌"}.get(d, "中性"), dc)
            set_cell(self.pred_tbl, i, 3, f"{p.get('score', 0):+.2f}", pnl_color(p.get("score")))
            set_cell(self.pred_tbl, i, 4, p.get("reason", ""), TEXT_DIM, align="left")

        # 席位胜率
        top = [s for s in stats if s["n"] >= 3][:20]
        self.hot_tbl.setRowCount(len(top))
        for i, s in enumerate(top):
            set_cell(self.hot_tbl, i, 0, s["seat"], TEXT_DIM, align="left")
            set_cell(self.hot_tbl, i, 1, str(s["n"]))
            set_cell(self.hot_tbl, i, 2, f"{s['avg5']:.2%}", pnl_color(s["avg5"]))
            set_cell(self.hot_tbl, i, 3, f"{s['win5']:.0%}")
            set_cell(self.hot_tbl, i, 4, f"{s['net_sum'] / 1e8:+.2f}",
                     pnl_color(s["net_sum"]))
        self.stat_hint.setText(
            f"{len(top)} 个席位(样本≥3) · 胜率已按 Beta(2,2) 平滑"
            if top else "还没有席位样本")

    # ------------------------------------------------------------------ 退出
    def stop(self):
        for t in self._tasks:
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
        self._tasks.clear()
