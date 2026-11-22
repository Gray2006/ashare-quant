# -*- coding: utf-8 -*-
"""大盘研判页:五维打分 + 要点/风险 + 交易规则速查。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..engine import exchange
from ..engine import macro as macro_mod
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import BORDER, DOWN, FLAT, TEXT, TEXT_DIM, UP

STANCE_COLOR = {"进攻": UP, "偏多": "#f0883e", "中性": FLAT, "偏空": "#58a6ff", "防守": DOWN}


class MacroPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rep: dict | None = None
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(180000)      # 三分钟自动刷一次,别把接口打爆
        QTimer.singleShot(600, self.refresh)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        board = Board("macro", cols=[1.0], rows=[0.15, 0.11, 0.48, 0.26])
        root.addWidget(board)

        # 顶部:三个数字 + 动作按钮
        top = Board("macro.top", cols=[0.26, 0.26, 0.30, 0.18], rows=[1.0])
        self.st_score = StatCard("大盘总分", "--", "0~100,越高越偏进攻")
        self.st_stance = StatCard("站位", "--", "进攻/偏多/中性/偏空/防守")
        self.st_asof = StatCard("数据时间", "--", "")
        for i, s in enumerate((self.st_score, self.st_stance, self.st_asof)):
            top.add(s, 0, i, min_w=150, min_h=88)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.btn_refresh = QPushButton("重新研判")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_rules = QPushButton("交易规则速查")
        self.btn_rules.clicked.connect(self.show_rules)
        rl.addWidget(self.btn_refresh)
        rl.addWidget(self.btn_rules)
        top.add(right, 0, 3, min_w=140, min_h=88)
        board.add(top, 0, 0, min_h=92)

        advice_card = Card("研判结论")
        self.advice = QLabel("点「重新研判」拉一遍数据")
        self.advice.setWordWrap(True)
        self.advice.setStyleSheet(f"color:{TEXT};font-size:13px;")
        advice_card.add(self.advice)
        board.add(advice_card, 1, 0, min_h=66)

        mid = Board("macro.mid", cols=[0.58, 0.42], rows=[1.0])
        left = Card("五维打分")
        self.dim_tbl = table(["维度", "分数", "权重", "依据"], widths=[72, 62, 56, 520],
                             stretch_col=3)
        left.add(self.dim_tbl, 1)
        mid.add(left, 0, 0, min_w=420, min_h=170)

        rt = Card("要点 / 风险")
        self.hl_tbl = table(["类型", "内容"], widths=[48, 380], stretch_col=1)
        rt.add(self.hl_tbl, 1)
        mid.add(rt, 0, 1, min_w=300, min_h=170)
        board.add(mid, 2, 0, min_h=180)

        raw_card = Card("完整报告")
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        self.raw.setPlaceholderText("完整报告")
        raw_card.add(self.raw, 1)
        board.add(raw_card, 3, 0, min_h=90)

    # ------------------------------------------------------------ 数据
    def refresh(self) -> None:
        self.btn_refresh.setEnabled(False)
        self.advice.setText("取数中:指数趋势 / 市场广度 / 资金 / 外围 / 消息…")

        def job():
            return macro_mod.analyze()

        def ok(rep):
            self.btn_refresh.setEnabled(True)
            self._on_report(rep)

        def err(e):
            self.btn_refresh.setEnabled(True)
            self.advice.setText(f"研判失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_refresh.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _auto_refresh(self) -> None:
        if self.isVisible() and self.btn_refresh.isEnabled():
            self.refresh()

    def _on_report(self, rep: dict) -> None:
        self._rep = rep
        color = STANCE_COLOR.get(rep["stance"], TEXT)
        self.st_score.set(f"{rep['score']:.0f}", color, hint=rep["advice"][:22])
        self.st_stance.set(rep["stance"], color, hint=f"缺 {len(rep['missing'])} 维"
                          if rep["missing"] else "五维齐全")
        self.st_asof.set(rep["asof"][5:], hint=rep["asof"][:10])
        self.advice.setText(rep["advice"])

        dims = rep["dims"]
        self.dim_tbl.setRowCount(len(dims))
        for i, d in enumerate(dims):
            sc = d["score"]
            txt = "—" if sc is None else f"{sc:.0f}"
            set_cell(self.dim_tbl, i, 0, d["name"], align="left", bold=True)
            set_cell(self.dim_tbl, i, 1, txt,
                     None if sc is None else (UP if sc >= 60 else (DOWN if sc <= 40 else TEXT)))
            set_cell(self.dim_tbl, i, 2, f"{d['weight']}%")
            set_cell(self.dim_tbl, i, 3, d["detail"][:150], align="left")

        rows = [("机会", x, UP) for x in rep["highlights"]]
        rows += [("风险", x, DOWN) for x in rep["risks"]]
        if not rows:
            rows = [("提示", "暂无明确信号,维持原仓位", TEXT_DIM)]
        self.hl_tbl.setRowCount(len(rows))
        for i, (kind, text, col) in enumerate(rows):
            set_cell(self.hl_tbl, i, 0, kind, col, bold=True, align="left")
            set_cell(self.hl_tbl, i, 1, str(text)[:180], align="left")

        self.raw.setPlainText(macro_mod.to_text(rep))
        self.status.emit(f"研判完成:{rep['score']:.0f} 分 · {rep['stance']}")

    # ------------------------------------------------------------ 规则
    def show_rules(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("A 股交易规则速查")
        dlg.setMinimumSize(620, 420)
        dlg.setSizeGripEnabled(True)
        lay = QVBoxLayout(dlg)
        txt = QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(_rules_text())
        lay.addWidget(txt, 1)
        b = QPushButton("关闭")
        b.clicked.connect(dlg.accept)
        lay.addWidget(b)
        dlg.exec()

    def stop(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass


def _rules_text() -> str:
    return ("【A 股交易规则 · 2026-07 现状】\n\n"
            + exchange.describe()
            + "\n\n【本工具的落地方式】\n"
            "· 下单前过风控:申报单位(科创板 200 股起)、涨跌停价、时段、单票仓位、单笔金额\n"
            "· 回测按板块取整手,涨停买不进、跌停卖不出,T+1 当日买入不可卖\n"
            "· 自动交易只在窗口内执行,频率可设每日一次 / 按间隔连续 / 每天指定次数\n"
            "· 每日下单笔数可设上限或不限(风控页面可改)\n")
