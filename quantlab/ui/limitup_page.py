# -*- coding: utf-8 -*-
"""涨停页:全市场当日涨停、连板梯队、炸板/跌停与涨停分析。

数据和统计口径都在 engine/limitup.py(纯标准库,可单独跑),这页只负责展示:
  · 左侧梯队按板数从高到低排,每档列出个股,一眼看出高度和厚度;
  · 右侧是当日全部涨停股,按封单额排,连板数单独一列;
  · 下排左边放炸板 + 跌停(亏钱效应的另一半),右边是文字结论;
  · 日期默认取今天,接口没有数据(非交易日/未更新)就明说,不偷偷换日期。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QPushButton,
                               QTextBrowser, QVBoxLayout, QWidget)

from ..engine import limitup
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import DOWN, TEXT_DIM, TEXT_MUT, UP, WARN

YI = 1e8


def _f(v, digits: int = 2, plus: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.{digits}f}" if plus else f"{v:.{digits}f}"


class LimitUpPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rows: list[dict] = []
        self._build()
        QTimer.singleShot(800, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("涨停", hint="全市场涨停池 · 连板梯队 · 炸板/跌停")
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(label("交易日", dim=True))
        self.day = QComboBox()
        for d in limitup.recent_days(12):
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

        stats = QHBoxLayout()
        stats.setSpacing(8)
        self.s_zt = StatCard("涨停", "-", "封住涨停的家数")
        self.s_dt = StatCard("跌停", "-", "封死跌停的家数")
        self.s_zb = StatCard("炸板率", "-", "炸板 / (涨停+炸板)")
        self.s_top = StatCard("最高板", "-", "连板高度")
        self.s_promo = StatCard("晋级率", "-", "昨日涨停 → 今日继续涨停")
        self.s_fund = StatCard("封单合计", "-", "涨停股封单金额")
        for w in (self.s_zt, self.s_dt, self.s_zb, self.s_top, self.s_promo,
                  self.s_fund):
            stats.addWidget(w, 1)
        top.body().addLayout(stats)
        root.addWidget(top)

        board = Board("limitup", cols=[0.5, 0.5], rows=[0.58, 0.42])

        lad = Card("连板梯队", hint="按板数从高到低 · 档内按封单排序")
        self.lad_tbl = table(["梯队", "只数", "个股"], widths=[80, 56, 520],
                             stretch_col=2)
        self.lad_tbl.setObjectName("LimitUpLadder")
        lad.add(self.lad_tbl, 1)
        self.lad_card = lad
        board.add(lad, 0, 0, min_w=300, min_h=230)

        zt = Card("当日涨停", hint="全市场 · 按封单额排")
        self.zt_tbl = table(
            ["代码", "名称", "连板", "首封", "炸板", "封单(亿)", "换手%", "行业"],
            widths=[62, 84, 48, 70, 44, 78, 58, 120], stretch_col=7)
        self.zt_tbl.setObjectName("LimitUpBoard")
        zt.add(self.zt_tbl, 1)
        self.zt_card = zt
        board.add(zt, 0, 1, min_w=430, min_h=230)

        weak = Card("炸板 / 跌停", hint="摸板没封住 + 封死跌停")
        self.weak_tbl = table(
            ["类型", "代码", "名称", "涨跌幅", "封板/打开", "封单(亿)", "行业"],
            widths=[50, 62, 84, 66, 74, 74, 130], stretch_col=6)
        self.weak_tbl.setObjectName("LimitUpWeak")
        weak.add(self.weak_tbl, 1)
        self.weak_card = weak
        board.add(weak, 1, 0, min_w=340, min_h=170)

        ana = Card("涨停分析", hint="数字来自当日池子,不构成建议")
        self.ana = QTextBrowser()
        self.ana.setOpenExternalLinks(False)
        self.ana.setStyleSheet(
            f"QTextBrowser{{background:transparent;border:0;color:{TEXT_DIM};"
            "font-size:12px;line-height:150%;}")
        ana.add(self.ana, 1)
        self.ana_card = ana
        board.add(ana, 1, 1, min_w=360, min_h=170)

        root.addWidget(board, 1)

    # ------------------------------------------------------------------ 取数
    def refresh(self):
        day = self.day.currentData() or ""
        self.btn.setEnabled(False)
        self.hint.setText(f"{day} 取数中…")

        def job():
            return limitup.snapshot(day or None)

        r = TaskRunner(job)
        r.succeeded.connect(self._ok)
        r.failed.connect(self._err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        r.finished.connect(lambda: self._tasks.remove(r) if r in self._tasks else None)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _ok(self, snap):
        if not snap.get("ok"):
            self._empty(snap.get("error") or "该交易日没有涨停数据")
            return
        an = snap.get("analysis") or {}
        self._rows = snap.get("rows") or []
        day = snap.get("day") or ""
        asked = snap.get("asked") or day

        note = "盘中,数字还会变" if snap.get("intraday") else "已收盘"
        if asked and day and asked != day:
            note += f" · {asked} 无数据,显示 {day}"
        self.hint.setText(f"{day} · {note} · 更新 {str(snap.get('saved_at') or '')[11:19]}")

        c = an.get("counts") or {}
        self.s_zt.set(str(c.get("zt", 0)), UP,
                      f"首板 {c.get('first', 0)} · 连板 {c.get('multi', 0)}")
        self.s_dt.set(str(c.get("dt", 0)), DOWN, f"炸板 {c.get('zb', 0)} 只")
        zbr = c.get("zb_rate")
        self.s_zb.set("-" if zbr is None else f"{zbr*100:.0f}%",
                      WARN if (zbr or 0) >= 0.30 else TEXT_MUT,
                      "摸板票里没封住的比例")
        self.s_top.set(f"{an.get('max_board', 0)} 板", UP, "最高连板数")
        pr = (an.get("promotion") or {}).get("rate")
        self.s_promo.set("-" if pr is None else f"{pr*100:.0f}%",
                         TEXT_MUT,
                         f"昨日涨停 {an.get('promotion', {}).get('prev_total', 0)} 只")
        self.s_fund.set(f"{(an.get('fund_total') or 0)/YI:.2f} 亿", TEXT_MUT,
                        "封单总额,不含炸板")

        self._fill_ladder(an)
        self._fill_zt(self._rows)
        self._fill_weak(snap.get("broken") or [], snap.get("limit_down") or [])
        self.ana.setPlainText(an.get("text") or "")
        self.lad_card.set_hint(f"最高 {an.get('max_board', 0)} 板 · "
                               f"{len(an.get('ladder') or [])} 档")
        self.zt_card.set_hint(f"{day} · {len(self._rows)} 只涨停")
        self.weak_card.set_hint(f"炸板 {len(snap.get('broken') or [])} · "
                                f"跌停 {len(snap.get('limit_down') or [])}")
        self.status.emit(f"涨停 {day} · {len(self._rows)} 只")

    def _fill_ladder(self, an: dict):
        lad = an.get("ladder") or []
        self.lad_tbl.setRowCount(len(lad))
        for i, lv in enumerate(lad):
            b = lv.get("boards") or 1
            set_cell(self.lad_tbl, i, 0, "首板" if b == 1 else f"{b} 板",
                     WARN if b >= 3 else UP, bold=True, align="left")
            count = lv.get("count", 0)
            set_cell(self.lad_tbl, i, 1, str(count))
            # 首板动辄上百只,全塞一格既看不清也撑坏行高:列前几只,余下报数
            rows = lv.get("rows") or []
            names = " · ".join(r.get("name", "") for r in rows[:10])
            if count > 10:
                names += f" … 等 {count} 只"
            set_cell(self.lad_tbl, i, 2, names, TEXT_DIM, align="left")
        if not lad:
            self.lad_tbl.setRowCount(1)
            set_cell(self.lad_tbl, 0, 2, "没有涨停就没有梯队", TEXT_MUT, align="left")

    def _fill_zt(self, rows: list[dict]):
        self.zt_tbl.setRowCount(len(rows))
        for r, it in enumerate(rows):
            set_cell(self.zt_tbl, r, 0, it.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.zt_tbl, r, 1, it.get("name", ""), align="left")
            b = it.get("boards")
            set_cell(self.zt_tbl, r, 2, "-" if b is None else str(b),
                     WARN if (b or 0) >= 3 else TEXT_MUT)
            set_cell(self.zt_tbl, r, 3, it.get("first_time") or "-", TEXT_DIM)
            set_cell(self.zt_tbl, r, 4, str(it.get("break_count") or 0),
                     WARN if (it.get("break_count") or 0) > 0 else TEXT_MUT)
            fund = set_cell(self.zt_tbl, r, 5, _f((it.get("fund") or 0) / YI))
            fund.setToolTip(f"封单 {_f((it.get('fund') or 0)/YI)} 亿 · 流通 "
                            f"{_f((it.get('float_cap') or 0)/YI)} 亿")
            set_cell(self.zt_tbl, r, 6, _f(it.get("turnover")))
            set_cell(self.zt_tbl, r, 7, it.get("sector") or "-", TEXT_DIM,
                     align="left")

    def _fill_weak(self, broken: list[dict], down: list[dict]):
        items = [("炸板", r) for r in broken] + [("跌停", r) for r in down]
        self.weak_tbl.setRowCount(len(items))
        for i, (kind, it) in enumerate(items):
            set_cell(self.weak_tbl, i, 0, kind,
                     WARN if kind == "炸板" else DOWN, bold=True, align="left")
            set_cell(self.weak_tbl, i, 1, it.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.weak_tbl, i, 2, it.get("name", ""), align="left")
            set_cell(self.weak_tbl, i, 3, _f(it.get("pct_chg"), plus=True) + "%",
                     pnl_color(it.get("pct_chg")))
            t = it.get("last_time") if kind == "炸板" else it.get("last_time")
            set_cell(self.weak_tbl, i, 4, t or it.get("first_time") or "-", TEXT_DIM)
            set_cell(self.weak_tbl, i, 5, _f((it.get("fund") or 0) / YI))
            set_cell(self.weak_tbl, i, 6, it.get("sector") or "-", TEXT_DIM,
                     align="left")

    def _empty(self, text: str):
        self._rows = []
        self.hint.setText(text)
        self.lad_tbl.setRowCount(0)
        self.zt_tbl.setRowCount(0)
        self.weak_tbl.setRowCount(0)
        self.ana.setPlainText(text)
        for s in (self.s_zt, self.s_dt, self.s_zb, self.s_top, self.s_promo,
                  self.s_fund):
            s.set("-", TEXT_MUT, "")
        self.status.emit(text)

    def _err(self, msg):
        self._empty(f"涨停取数失败:{str(msg)[:120]}")

    # ------------------------------------------------------------------ 退出
    def stop(self):
        for t in self._tasks:
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
        self._tasks.clear()
