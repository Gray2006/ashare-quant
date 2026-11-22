# -*- coding: utf-8 -*-
"""风险档位页:横截面看整个池子的波动风险,给四档 + 建议仓位。

回答的是「这些票里谁的波动风险高」,不是「这只票明天会涨会跌」。
走查命中率和「永远猜高」的基准并排显示,不藏。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import riskband as rb
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, set_cell, table
from .theme import ACCENT, BG, BORDER, DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN


class BandBar(QWidget):
    """四档分布条:按池内占比画,当前标的所在档高亮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(26)
        self._bands = []
        self._cur = -1

    def set_data(self, bands: list[dict], cur: int = -1) -> None:
        self._bands = bands or []
        self._cur = cur
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(BG))
        total = sum(max(1, b.get("n", 0)) for b in self._bands) or 1
        x = 0.0
        for i, b in enumerate(self._bands):
            seg = w * max(1, b.get("n", 0)) / total
            c = QColor(rb.BAND_COLOR[b["idx"]] if b["idx"] < len(rb.BAND_COLOR) else FLAT)
            if i == self._cur:
                p.fillRect(int(x), 0, int(seg), h, c)
            else:
                c.setAlpha(70)
                p.fillRect(int(x), int(h * 0.25), int(seg), int(h * 0.5), c)
            if seg > 42:
                p.setPen(QPen(QColor(TEXT if i == self._cur else TEXT_DIM)))
                f = p.font()
                f.setPointSizeF(8.0)
                p.setFont(f)
                p.drawText(int(x), 0, int(seg), h, Qt.AlignCenter, b["band"])
            x += seg
        p.end()


class RiskBandPage(QWidget):
    key = "risk"
    title = "风险档位"
    goto_quote = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rep: dict | None = None
        self._started = False
        self._build()

    def showEvent(self, ev):  # noqa: N802
        # 首次显示才算:全池一次 30 多秒,不能拖住启动
        super().showEvent(ev)
        self.ensure_loaded()

    def ensure_loaded(self) -> None:
        """幂等:第一次进来才起任务。切页时由主窗显式调一次,不赌 showEvent。"""
        if self._started:
            return
        self._started = True
        self.refresh()

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("标的"))
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setMinimumWidth(150)
        self.target.addItem("全池一览", "")
        for code, name in _watch():
            self.target.addItem(f"{name} {code}", code)
        bar.addWidget(self.target)

        self.btn = QPushButton("看档位")
        self.btn.clicked.connect(self.refresh_now)
        bar.addWidget(self.btn)

        self.btn_pool = QPushButton("算全池")
        self.btn_pool.clicked.connect(lambda: self.refresh_now(force=True))
        bar.addWidget(self.btn_pool)

        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet(f"color:{TEXT_MUT};")
        bar.addWidget(self.lbl_state, 1)
        outer.addLayout(bar)

        board = Board("risk", cols=[1.0], rows=[0.16, 0.42, 0.42])

        # 概览
        top = Board("risk.top", cols=[0.22, 0.20, 0.20, 0.38], rows=[1.0])
        self.c_band = StatCard("当前档位", "-", "池内波动分位")
        self.c_vol = StatCard("当前日波动", "-", "近 20 日已实现")
        self.c_pred = StatCard("未来 5 日波动", "-", "历史同档中位数")
        self.c_pos = StatCard("建议仓位上限", "-", "按档位给")
        for i, w in enumerate((self.c_band, self.c_vol, self.c_pred, self.c_pos)):
            top.add(w, 0, i, min_w=120, min_h=70)
        board.add(top, 0, 0, min_h=80)

        # 档位表 + 走查
        mid = Board("risk.mid", cols=[0.62, 0.38], rows=[1.0])
        c_lv = Card("四档对照", hint="当前波动区间 → 历史同档的未来波动")
        self.lv_tbl = table(["档位", "当前波动", "样本", "未来波动中位", "P25~P75", "仓位上限"],
                            [56, 96, 62, 100, 104, 74], stretch_col=3)
        c_lv.add(self.lv_tbl, 1)
        mid.add(c_lv, 0, 0, min_w=380, min_h=150)

        c_bt = Card("走查校验", hint="只用当时之前的数据分档,不偷看未来")
        self.bt_tbl = table(["项目", "数值"], [96, 130], stretch_col=1)
        c_bt.add(self.bt_tbl, 1)
        mid.add(c_bt, 0, 1, min_w=230, min_h=150)
        board.add(mid, 1, 0, min_h=150)

        # 池子明细
        c_pool = Card("池内明细", hint="按波动分位从高到低;双击进行情页")
        self.pool_tbl = table(["排名", "代码", "名称", "档位", "当前波动", "未来波动", "仓位上限"],
                              [58, 76, 110, 58, 92, 92, 78], stretch_col=2)
        self.pool_tbl.cellDoubleClicked.connect(self._goto_quote)
        c_pool.add(self.pool_tbl, 1)
        board.add(c_pool, 2, 0, min_h=150)

        outer.addWidget(board, 1)

        self.band_bar = BandBar()
        outer.addWidget(self.band_bar)

        for t in (self.lv_tbl, self.bt_tbl):
            t.verticalHeader().setDefaultSectionSize(26)
        self.pool_tbl.verticalHeader().setDefaultSectionSize(25)

    # ---------------------------------------------------------------- 取数
    def refresh(self) -> None:
        self.refresh_now()

    def refresh_now(self, force: bool = False) -> None:
        if self._tasks:
            return
        code = self.target.currentData() if self.target.isEditable() else None
        self.btn.setEnabled(False)
        self.lbl_state.setText("算档位中…")

        def job():
            if code:
                return rb.risk_band(code)
            return rb.panel_risk()

        t = TaskRunner(job, name="riskband")
        t.succeeded.connect(lambda rep: self._on_report(rep, bool(code)))
        t.failed.connect(self._on_fail)
        t.finished.connect(lambda: self._tasks.remove(t) if t in self._tasks else None)
        t.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks.append(t)
        t.start()

    def _on_fail(self, msg: str) -> None:
        self.lbl_state.setText(f"失败:{msg[:60]}")
        self.btn.setEnabled(True)

    def _on_report(self, rep: dict, single: bool) -> None:
        self._rep = rep
        bt = rep.get("backtest") or {}
        levels = rep.get("levels") or []

        if single:
            self.lbl_state.setText(f"{rep['name']}({rep['code']}) · {rep['asof']}")
            self.c_band.set(rep["band"], rep["band_color"], f"池内 {rep['rank'] * 100:.0f}% 分位")
            self.c_vol.set(f"{rep['cur_vol_pct']:.2f}%", TEXT)
            self.c_pred.set(f"{rep['pred_vol_pct']:.2f}%",
                            TEXT,
                            f"P25~P75 {rep['vol_lo_pct']:.2f}~{rep['vol_hi_pct']:.2f}%")
            self.c_pos.set(f"{rep['pos_cap'] * 100:.0f}%", WARN if rep["pos_cap"] <= 0.4 else TEXT)
            cur_idx = rep["band_idx"]
            items = [rep]
        else:
            self.lbl_state.setText(f"全池 {rep['pool']} 只 · 截至 {rep['asof']}")
            items = rep.get("items") or []
            self.c_band.set(f"{len(items)} 只", TEXT, "池内标的")
            if items:
                hi = items[0]
                self.c_band.set(hi["band"], hi["band_color"], f"最高:{hi['name']}")
            self.c_vol.set(f"{_median([it['cur_vol_pct'] for it in items]):.2f}%", TEXT, "池内中位")
            self.c_pred.set(f"{_median([it['pred_vol_pct'] for it in items if it['pred_vol_pct']]):.2f}%",
                            TEXT, "历史同档中位")
            self.c_pos.set("按档位", TEXT, "见下表")
            cur_idx = -1

        # 档位表
        self.lv_tbl.setRowCount(len(levels))
        for i, lv in enumerate(levels):
            col = rb.BAND_COLOR[lv["idx"]]
            bold = lv["idx"] == cur_idx
            set_cell(self.lv_tbl, i, 0, lv["band"], col, bold=bold, align="left")
            set_cell(self.lv_tbl, i, 1, f"{lv['lo'] * 100:.2f}~{lv['hi'] * 100:.2f}%", TEXT)
            set_cell(self.lv_tbl, i, 2, str(lv["n"]), TEXT_DIM)
            set_cell(self.lv_tbl, i, 3, f"{(lv['fut_med'] or 0) * 100:.2f}%", TEXT, bold=bold)
            set_cell(self.lv_tbl, i, 4,
                     f"{(lv['fut_p25'] or 0) * 100:.2f}~{(lv['fut_p75'] or 0) * 100:.2f}%", TEXT_DIM)
            set_cell(self.lv_tbl, i, 5, f"{lv['pos_cap'] * 100:.0f}%", TEXT_DIM)

        # 走查
        rows = []
        if bt.get("n"):
            exc = bt.get("excess") or 0.0
            rows = [
                ("走查样本", f"{bt['n']} 次", TEXT),
                ("命中率", f"{bt['hit'] * 100:.1f}%(基准 {bt['base'] * 100:.1f}%)",
                 UP if exc > 0.02 else TEXT_DIM),
                ("超额", f"{exc * 100:+.1f} 个点", UP if exc > 0 else DOWN),
                ("排名相关", f"{bt['ic']:+.2f}" if bt.get("ic") is not None else "-",
                 UP if (bt.get("ic") or 0) > 0.1 else TEXT_DIM),
                ("置信", rep.get("confidence", "-"), TEXT),
            ]
        else:
            rows = [("走查样本", "不足", TEXT_DIM)]
        self.bt_tbl.setRowCount(len(rows))
        for i, (k, v, col) in enumerate(rows):
            set_cell(self.bt_tbl, i, 0, k, align="left")
            set_cell(self.bt_tbl, i, 1, v, col, align="left")

        # 明细
        self.pool_tbl.setRowCount(len(items))
        for i, it in enumerate(items):
            set_cell(self.pool_tbl, i, 0, f"{it['rank'] * 100:.0f}%", TEXT_DIM)
            set_cell(self.pool_tbl, i, 1, it["code"], TEXT_DIM)
            set_cell(self.pool_tbl, i, 2, it["name"], TEXT, align="left")
            set_cell(self.pool_tbl, i, 3, it["band"], it["band_color"], bold=True, align="left")
            set_cell(self.pool_tbl, i, 4, f"{it['cur_vol_pct']:.2f}%", TEXT)
            set_cell(self.pool_tbl, i, 5,
                     f"{(it['pred_vol_pct'] or 0):.2f}%", TEXT)
            set_cell(self.pool_tbl, i, 6, f"{it['pos_cap'] * 100:.0f}%",
                     WARN if it["pos_cap"] <= 0.4 else TEXT_DIM)

        self.band_bar.set_data(levels, cur_idx)
        self.btn.setEnabled(True)

    def _goto_quote(self, row: int, _col: int) -> None:
        it = self.pool_tbl.item(row, 1)
        if not it:
            return
        code = it.text().strip()
        if len(code) == 6:
            self.goto_quote.emit(code)

    def stop(self) -> None:
        # 别清空 _tasks:主窗 closeEvent 还会从各页的 _tasks 收集线程并兜底
        # terminate,清掉引用就漏成「QThread: Destroyed while thread is still running」
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(1200)
            except Exception:
                pass


def _watch() -> list[tuple[str, str]]:
    """自选股列表。不在这里拉全市场快照 —— 那要十几秒,会拖住窗口启动。"""
    out = []
    try:
        for c in cfg().watchlist:
            out.append((c, c))
    except Exception:
        pass
    return out


def _median(vals) -> float:
    """中位数。只为这一处,不引 numpy。"""
    v = sorted(float(x) for x in vals if x is not None)
    if not v:
        return 0.0
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0
