# -*- coding: utf-8 -*-
"""选股页:预设策略/自定义条件筛选 + 主力资金榜 + 板块资金流。"""
from __future__ import annotations

import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTabWidget, QVBoxLayout, QWidget)

from ..config import APP_DIR
from ..data import em, moneyflow, sector
from ..engine import screener
from ..engine.hub import TaskRunner
from ..utils import fmt_amount, fmt_pct, fmt_price
from .board import Board
from .common import Card, pnl_color, set_cell, table
from .theme import TEXT_DIM, TEXT_MUT


def _drop_high_roic(df, thr: float = 0.7):
    """剔除 ROIC 截面分位 > thr 的票。返回 (新 df, 说明文字)。

    财务数据是 PIT 的(按法定披露截止日),取最近一期。
    没有财务数据的票**不排除** —— 宁可漏掉也不误杀。
    """
    try:
        import json

        import pandas as pd

        from ..engine import finance_pit as fp

        path = fp.ROOT / "data" / "finance_pit.json"
        if not path.exists():
            return df, " · 无财务缓存,未过滤"
        cache = json.loads(path.read_text(encoding="utf-8"))
        vals = {}
        for code in df.get("code", []):
            periods = cache.get(str(code)) or {}
            best = None
            for period, row in periods.items():
                v = row.get("roic")
                if isinstance(v, (int, float)):
                    if best is None or str(period) > str(best[0]):
                        best = (period, float(v))
            if best:
                vals[str(code)] = best[1]
        if not vals:
            return df, " · 无 ROIC 数据,未过滤"
        cut = float(pd.Series(vals).quantile(thr))
        keep = [c for c in df.get("code", [])
                if str(c) not in vals or vals[str(c)] <= cut]
        out = df[df["code"].astype(str).isin(keep)]
        return out, (f" · 已排除高 ROIC {len(df) - len(out)} 只"
                     f"(覆盖 {len(vals)} 只,阈值 {cut:.1f})")
    except Exception:  # noqa: BLE001
        return df, " · ROIC 过滤失败,未过滤"


class ScreenPage(QWidget):
    status = Signal(str)
    send_to_trade = Signal(list)      # [{"code","name","price"}]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._result = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        board = Board("screen", cols=[1.0], rows=[0.21, 0.79])
        root.addWidget(board)

        cond = Card("筛选条件")
        row1 = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.addItem("(自定义)", "")
        for p in screener.preset_list():
            self.preset.addItem(p["name"], p["expr"])
        self.preset.currentIndexChanged.connect(self._on_preset)
        self.expr = QLineEdit()
        self.expr.setPlaceholderText("例:main_inflow > 3e7 and pct_chg > 0 and turnover < 15")
        self.expr.returnPressed.connect(self.run_screen)
        self.btn_run = QPushButton("筛选")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run_screen)
        row1.addWidget(QLabel("预设"))
        row1.addWidget(self.preset)
        row1.addSpacing(8)
        row1.addWidget(self.expr, 1)
        row1.addWidget(self.btn_run)
        self.btn_save = QPushButton("存为选股方式")
        self.btn_save.setToolTip("把当前表达式存进预设,以后下拉直接选")
        self.btn_save.clicked.connect(self.save_preset)
        row1.addWidget(self.btn_save)
        self.btn_del = QPushButton("删预设")
        self.btn_del.clicked.connect(self.delete_preset)
        row1.addWidget(self.btn_del)
        # 排除高 ROIC:3 年样本上 ROIC 中性化后 t=-2.05(高 ROIC 跑输)。
        # 不是 alpha,是过滤器;A 股不能做空,只能用来避开。
        self.excl_roic = QCheckBox("排除高 ROIC")
        self.excl_roic.setToolTip(
            "把 ROIC 截面分位 > 70% 的票剔掉。\n"
            "依据:3 年样本、beta 中性化后 t=-2.05 —— 高 ROIC 的票跑输。\n"
            "财务数据只覆盖部分票,没覆盖到的不排除。")
        row1.addWidget(self.excl_roic)
        cond._lay.addLayout(row1)

        self.hint = QLabel("可用字段:price pct_chg turnover volume_ratio amount amplitude "
                           "pe_ttm pb market_cap float_cap main_inflow main_ratio "
                           "super_inflow big_inflow board  ·  支持 and/or/not/in 与四则运算")
        self.hint.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        self.hint.setWordWrap(True)
        cond.add(self.hint)
        board.add(cond, 0, 0, min_h=104)

        self.tabs = QTabWidget()
        # 1 选股结果
        self.tbl = table(["代码", "名称", "现价", "涨跌幅", "换手", "量比", "成交额",
                          "主力净流入", "主力占比", "PE", "市值", "板块"],
                         widths=[62, 84, 68, 72, 54, 52, 86, 96, 72, 60, 88, 70],
                         stretch_col=1)
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        w1 = QWidget()
        l1 = QVBoxLayout(w1)
        l1.setContentsMargins(8, 8, 8, 8)
        l1.addWidget(self.tbl)
        bottom = QHBoxLayout()
        self.summary = QLabel("尚未筛选")
        self.summary.setStyleSheet(f"color:{TEXT_DIM};")
        b_add = QPushButton("加入自选")
        b_add.clicked.connect(self._add_watch)
        b_trade = QPushButton("去下单")
        b_trade.setObjectName("Primary")
        b_trade.clicked.connect(self._to_trade)
        b_csv = QPushButton("导出 CSV")
        b_csv.clicked.connect(self._export)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(b_add)
        bottom.addWidget(b_trade)
        bottom.addWidget(b_csv)
        l1.addLayout(bottom)
        self.tabs.addTab(w1, "选股结果")

        # 2 主力流入榜
        self.flow_tbl = table(["代码", "名称", "现价", "涨跌幅", "主力净流入", "主力占比",
                               "超大单", "成交额"],
                              widths=[62, 90, 68, 72, 96, 76, 92, 90], stretch_col=1)
        w2 = QWidget()
        l2 = QVBoxLayout(w2)
        l2.setContentsMargins(8, 8, 8, 8)
        bar2 = QHBoxLayout()
        self.flow_order = QComboBox()
        self.flow_order.addItems(["净流入前 50", "净流出前 50"])
        b2 = QPushButton("刷新")
        b2.clicked.connect(self.load_flow_rank)
        self.flow_span = QComboBox()
        self.flow_span.addItem("今日", 1)
        self.flow_span.addItem("近 3 日", 3)
        self.flow_span.addItem("近 5 日", 5)
        self.flow_span.addItem("近 7 日", 7)
        self.flow_span.setToolTip("区间口径按交易日累加主力净额:榜上逐只取日频,首切需几秒")
        self.flow_span.currentIndexChanged.connect(lambda *_: self.load_flow_rank())
        bar2.addWidget(self.flow_span)
        bar2.addWidget(self.flow_order)
        bar2.addWidget(b2)
        bar2.addStretch(1)
        l2.addLayout(bar2)
        # 兜底源口径和新浪不同,必须标出来,不然「主力净流入」会被当成同一口径
        self.flow_note = QLabel("")
        self.flow_note.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        l2.addWidget(self.flow_note)
        l2.addWidget(self.flow_tbl)
        self.tabs.addTab(w2, "主力资金榜")

        # 3 板块资金流
        self.sector_tbl = table(["板块", "涨跌幅", "主力净流入", "主力占比", "领涨股", "领涨%"],
                                widths=[120, 74, 100, 80, 100, 74], stretch_col=0)
        w3 = QWidget()
        l3 = QVBoxLayout(w3)
        l3.setContentsMargins(8, 8, 8, 8)
        bar3 = QHBoxLayout()
        b3 = QPushButton("刷新板块")
        b3.clicked.connect(self.load_sectors)
        bar3.addWidget(b3)
        bar3.addStretch(1)
        l3.addLayout(bar3)
        self.sector_note = QLabel("")
        self.sector_note.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        l3.addWidget(self.sector_note)
        l3.addWidget(self.sector_tbl)
        self.tabs.addTab(w3, "板块资金流")
        self.tabs.currentChanged.connect(self._on_tab)
        board.add(self.tabs, 1, 0, min_h=260)

    # ------------------------------------------------------------ 任务
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

    def _reload_presets(self) -> None:
        cur = self.preset.currentData()
        self.preset.blockSignals(True)
        self.preset.clear()
        self.preset.addItem("(自定义)", "")
        for p2 in screener.preset_list():
            self.preset.addItem(p2["name"], p2["expr"])
        i = self.preset.findData(cur) if cur else 0
        self.preset.setCurrentIndex(max(0, i))
        self.preset.blockSignals(False)

    def save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        expr = self.expr.text().strip()
        if not expr:
            self.status.emit("先写表达式")
            return
        ok, why = screener.validate_expr(expr)
        if not ok:
            self.status.emit(f"表达式有误:{why}")
            return
        name, ok2 = QInputDialog.getText(self, "存为选股方式", "起个名字:")
        if not ok2 or not name.strip():
            return
        try:
            screener.save_preset(name.strip(), expr)
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"保存失败:{e}")
            return
        self._reload_presets()
        self.status.emit(f"已存为「{name.strip()}」")

    def delete_preset(self) -> None:
        name = self.preset.currentText()
        expr = self.preset.currentData()
        if not expr:
            self.status.emit("先选中一条预设")
            return
        if screener.delete_preset(name):
            self._reload_presets()
            self.status.emit(f"已删除「{name}」")

    def _on_preset(self) -> None:
        e = self.preset.currentData()
        if e:
            self.expr.setText(e)

    def _on_tab(self, idx: int) -> None:
        if idx == 1 and self.flow_tbl.rowCount() == 0:
            self.load_flow_rank()
        elif idx == 2 and self.sector_tbl.rowCount() == 0:
            self.load_sectors()

    # ------------------------------------------------------------ 筛选
    def run_screen(self) -> None:
        expr = self.expr.text().strip() or self.preset.currentData()
        if not expr:
            self.status.emit("先选一个预设或写条件")
            return
        ok, err = screener.validate_expr(expr)
        if not ok:
            self.status.emit(f"条件有误:{err}")
            return
        self.btn_run.setEnabled(False)
        self.summary.setText("筛选中…")

        excl = self.excl_roic.isChecked()

        def job():
            df = screener.run(expr, limit=500)
            note = ""
            if excl and df is not None and len(df):
                df, note = _drop_high_roic(df)
            return df, screener.explain(df) + note

        def on_ok(pair):
            df, text = pair
            self._result = df
            self._fill(df)
            self.summary.setText(text)
            self.btn_run.setEnabled(True)
            self.status.emit(text)

        self._run(job, on_ok, "screen")

    def _fill(self, df) -> None:
        self.tbl.setRowCount(0)
        if df is None or df.empty:
            return
        self.tbl.setRowCount(len(df))
        for i, r in enumerate(df.to_dict("records")):
            col = pnl_color(r.get("pct_chg"))
            set_cell(self.tbl, i, 0, str(r.get("code")), align="left")
            set_cell(self.tbl, i, 1, str(r.get("name") or ""), align="left")
            set_cell(self.tbl, i, 2, fmt_price(r.get("price")), col)
            set_cell(self.tbl, i, 3, fmt_pct(r.get("pct_chg")), col, bold=True)
            set_cell(self.tbl, i, 4, f"{r.get('turnover') or 0:.2f}")
            set_cell(self.tbl, i, 5, f"{r.get('volume_ratio') or 0:.2f}")
            set_cell(self.tbl, i, 6, fmt_amount(r.get("amount")))
            set_cell(self.tbl, i, 7, fmt_amount(r.get("main_inflow")),
                     pnl_color(r.get("main_inflow")))
            set_cell(self.tbl, i, 8, f"{r.get('main_ratio') or 0:.2f}%")
            set_cell(self.tbl, i, 9, f"{r.get('pe_ttm') or 0:.1f}")
            set_cell(self.tbl, i, 10, fmt_amount(r.get("market_cap")))
            set_cell(self.tbl, i, 11, str(r.get("board") or ""), align="left")

    def _selected_row(self) -> dict | None:
        rows = self.tbl.selectionModel().selectedRows() if self.tbl.selectionModel() else []
        if not rows or self._result is None:
            return None
        i = rows[0].row()
        if i >= len(self._result):
            return None
        return self._result.iloc[i].to_dict()

    def _on_pick(self) -> None:
        pass

    def _add_watch(self) -> None:
        from ..data.store import store
        r = self._selected_row()
        if not r:
            self.status.emit("先选中一行")
            return
        store().add_watch(str(r["code"]), str(r.get("name") or ""))
        self.status.emit(f"已加自选:{r['code']} {r.get('name')}")

    def _to_trade(self) -> None:
        r = self._selected_row()
        if not r:
            self.status.emit("先选中一行")
            return
        self.send_to_trade.emit([{"code": str(r["code"]), "name": str(r.get("name") or ""),
                                  "price": r.get("price")}])

    def _export(self) -> None:
        if self._result is None or self._result.empty:
            self.status.emit("没有可导出的结果")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出选股结果",
                                             str(APP_DIR / f"screen_{time.strftime('%Y%m%d_%H%M')}.csv"),
                                             "CSV (*.csv)")
        if not path:
            return
        self._result.to_csv(path, index=False, encoding="utf-8-sig")
        self.status.emit(f"已导出 {path}")

    # ------------------------------------------------------------ 榜单
    def flow_days(self) -> int:
        """当前选的是几日的口径。"""
        try:
            return max(1, int(self.flow_span.currentData() or 1))
        except (TypeError, ValueError):
            return 1

    def load_flow_rank(self) -> None:
        asc = self.flow_order.currentIndex() == 1
        days = self.flow_days()

        def job():
            rows = em.moneyflow_rank(50, asc=asc)
            if days > 1:
                codes = [str(r.get("code")) for r in rows if r.get("code")]
                wins = moneyflow.batch_window(codes, days, deadline=12.0, max_parallel=8)
                merged = []
                for r in rows:
                    w = wins.get(str(r.get("code")))
                    if not w or not w.get("count"):
                        continue
                    r = dict(r)
                    r["_win"] = w
                    merged.append(r)
                merged.sort(key=lambda x: x["_win"]["total"], reverse=not asc)
                return {"rows": merged, "days": days}
            return {"rows": rows, "days": days}

        def on_ok(out):
            rows = (out or {}).get("rows") or []
            days = int((out or {}).get("days") or 1)
            if days != self.flow_days():
                return          # 用户已经切到别的档位,这次结果作废
            self.flow_tbl.setRowCount(len(rows))
            if days > 1:
                self.flow_tbl.setHorizontalHeaderLabels(
                    ["代码", "名称", "现价", "涨跌幅", f"近{days}日流入", f"近{days}日流出",
                     f"近{days}日净流入", "成交额"])
                for _c, _w in enumerate([62, 96, 70, 80, 104, 104, 104, 92]):
                    self.flow_tbl.set_col_width(_c, _w)
            else:
                self.flow_tbl.setHorizontalHeaderLabels(
                    ["代码", "名称", "现价", "涨跌幅", "主力净流入", "主力占比", "超大单", "成交额"])
                for _c, _w in enumerate([62, 90, 68, 80, 96, 76, 92, 90]):
                    self.flow_tbl.set_col_width(_c, _w)
            src = (rows[0].get("source") if rows else "") or ""
            if not rows:
                self.flow_note.setText("资金榜暂时取不到(东财与新浪都没数据)")
            elif days > 1:
                self.flow_note.setText(
                    f"口径:近 {days} 个交易日(东财 daykline / 新浪逐日)。"
                    "流入 = 净额为正的那些天合计,流出 = 净额为负的那些天合计,"
                    "净流入 = 两者之差;源只给每日净额,不是买卖毛额。"
                    "榜上逐只取日频,首切约几秒,5 分钟内再切走缓存")
            elif src == "sina":
                self.flow_note.setText("东财不可用,已切新浪资金榜:main = 大单以上净流入合计,"
                                       "「超大单」列为新浪 r0 净额,口径与东财 f62 略有差别")
            else:
                self.flow_note.setText("东财口径:主力净流入 = 超大单 + 大单(f62)")
            for i, r in enumerate(rows):
                col = pnl_color(r.get("change_pct"))
                set_cell(self.flow_tbl, i, 0, str(r.get("code")), align="left")
                set_cell(self.flow_tbl, i, 1, str(r.get("name") or ""), align="left")
                set_cell(self.flow_tbl, i, 2, fmt_price(r.get("price")), col)
                set_cell(self.flow_tbl, i, 3, fmt_pct(r.get("change_pct")), col)
                w = r.get("_win")
                if days > 1 and w:
                    set_cell(self.flow_tbl, i, 4, fmt_amount(w.get("inflow")),
                             pnl_color(w.get("inflow")))
                    set_cell(self.flow_tbl, i, 5, fmt_amount(w.get("outflow")),
                             pnl_color(w.get("outflow")))
                    set_cell(self.flow_tbl, i, 6, fmt_amount(w.get("total")),
                             pnl_color(w.get("total")), bold=True)
                    set_cell(self.flow_tbl, i, 7, fmt_amount(r.get("amount")))
                else:
                    set_cell(self.flow_tbl, i, 4, fmt_amount(r.get("main_inflow")),
                             pnl_color(r.get("main_inflow")), bold=True)
                    set_cell(self.flow_tbl, i, 5, f"{r.get('main_ratio') or 0:.2f}%")
                    set_cell(self.flow_tbl, i, 6, fmt_amount(r.get("super_inflow")),
                             pnl_color(r.get("super_inflow")))
                    set_cell(self.flow_tbl, i, 7, fmt_amount(r.get("amount")))

        # 任务名带档位:否则切档时同名任务还在跑,新请求会被静默丢掉
        self._run(job, on_ok, f"flowrank_{days}")

    def load_sectors(self) -> None:
        def job():
            return sector.boards("hy")

        def on_ok(rows):
            self.sector_tbl.setRowCount(len(rows))
            src = "新浪" if (rows and rows[0].get("source") == "sina") else "腾讯"
            self.sector_note.setText(
                f"{len(rows)} 个行业 · 源 {src} · 完整榜单与成分股见「板块」页"
                if rows else "板块资金流取不到,稍后自动重试")
            for i, r in enumerate(rows):
                set_cell(self.sector_tbl, i, 0, str(r.get("name")), align="left")
                set_cell(self.sector_tbl, i, 1, fmt_pct(r.get("change_pct")),
                         pnl_color(r.get("change_pct")))
                set_cell(self.sector_tbl, i, 2, fmt_amount(r.get("main")),
                         pnl_color(r.get("main")), bold=True)
                ratio = r.get("main_ratio")
                set_cell(self.sector_tbl, i, 3, f"{ratio:.2f}%" if ratio is not None else "-")
                set_cell(self.sector_tbl, i, 4, str(r.get("lead_name") or ""), align="left")
                set_cell(self.sector_tbl, i, 5, fmt_pct(r.get("lead_pct")),
                         pnl_color(r.get("lead_pct")))

        self._run(job, on_ok, "sector")
