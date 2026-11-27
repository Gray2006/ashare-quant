# -*- coding: utf-8 -*-
"""策略因子挖掘页(易用版):一键挖掘 + 样本外/稳定性 + 组合回测 + 取用。

设计目标:
  * 第一次打开就知道点哪个按钮、结果怎么看;
  * 默认只露「一键挖掘」,参数收进「高级设置」;
  * 每个指标都有中文解释,选中因子后明确告诉你下一步能干什么;
  * 选因子必须过样本外,表里直接标「过拟合风险」。
"""
from __future__ import annotations

import json
import threading
import time

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..config import USER_DIR
from ..engine import factor_mine
from ..engine.hub import TaskRunner
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import ACCENT, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

def _fmt(v, spec: str, default: str = "—") -> str:
    """None/NaN 不炸格式化。"""
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return default


HELP = """<h3>这是什么</h3>
<p>程序自动生成一批「因子公式」(比如 <code>roc(close,20)</code> = 20 日涨幅),
用历史数据检验它们和未来涨跌有没有稳定关系。挖出来的公式可以直接拿去选股或回测。</p>

<h3>怎么看结果</h3>
<ul>
<li><b>训练 IC</b>:前 70% 历史里,因子值和未来收益的相关性。正数=因子越大越容易涨。</li>
<li><b>测试 IC</b>:后 30% 历史(模型没看过)的相关性。<b>这个才代表能不能用</b>。</li>
<li><b>稳定性</b>:把历史切成 3 段,有几段和训练段同向。3/3 最好,1/3 以下基本是噪声。</li>
<li><b>覆盖</b>:有多少股票/币种能算出这个因子。太低说明只对少数标的有效。</li>
<li><b>多空</b>:按因子分 5 组,最高组减最低组的平均收益差。</li>
<li><b>风险</b>:低=样本外通过且 3 段都稳;中=样本外通过但不够稳;高=样本外没通过。</li>
</ul>

<h3>怎么用</h3>
<ol>
<li>点「一键挖掘」,等结果出来(第一次约 1~3 分钟)。</li>
<li>在表里挑「测试 IC 和训练 IC 同号、稳定性 ≥ 2/3、风险低/中」的因子。</li>
<li>选中 1~5 个 →「回测这组因子」看组合净值;或「复制表达式」粘到选股页的条件里。</li>
<li>想长期留着 →「加入因子库」;想给别的程序用 →「导出 JSON」。</li>
</ol>

<h3>为什么还会过拟合</h3>
<p>程序已经用「后 30% 样本外 + 3 段稳定性 + 相关性去重」挡掉大部分,
但历史规律本来就会失效。测试 IC 很高、稳定性却只有 1/3 的,别用。</p>
"""


class _HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("因子挖掘怎么用")
        self.resize(680, 560)
        lay = QVBoxLayout(self)
        txt = QLabel(HELP)
        txt.setWordWrap(True)
        txt.setTextFormat(Qt.TextFormat.RichText)
        txt.setStyleSheet(f"color:{TEXT};")
        lay.addWidget(txt, 1)
        b = QPushButton("知道了")
        b.clicked.connect(self.accept)
        lay.addWidget(b)


class _Curve(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=False, y=True, alpha=0.12)
        self.plot.getAxis("left").setTextPen(TEXT_DIM)
        self.plot.getAxis("bottom").setTextPen(TEXT_DIM)
        self.plot.hideButtons()
        self.plot.setLabel("left", "净值", color=TEXT_MUT)
        lay.addWidget(self.plot)

    def show_curve(self, curve: list):
        self.plot.clear()
        if not curve:
            return
        ys = [float(v) for _d, v in curve]
        self.plot.plot(range(len(ys)), ys, pen=pg.mkPen(ACCENT, width=1.8),
                       fillLevel=1.0, brush=pg.mkBrush(76, 141, 255, 40))
        self.plot.addLine(y=1.0, pen=pg.mkPen("#3d4856", width=1))
        self.plot.setXRange(0, max(1, len(ys) - 1), padding=0)


class _LibraryDialog(QDialog):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("我的因子库")
        self.resize(760, 440)
        lay = QVBoxLayout(self)
        self.tbl = table(["表达式", "备注", "测试IC", "稳定性", "加入时间"],
                         widths=[320, 150, 60, 60, 110], stretch_col=0)
        lay.addWidget(self.tbl, 1)
        bar = QHBoxLayout()
        self.hint = label("", dim=True)
        bar.addWidget(self.hint, 1)
        b_copy = QPushButton("复制表达式")
        b_copy.clicked.connect(self._copy)
        b_del = QPushButton("删除")
        b_del.clicked.connect(self._del)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bar.addWidget(b_copy)
        bar.addWidget(b_del)
        bar.addWidget(b_close)
        lay.addLayout(bar)
        self._rows = []
        self.reload()

    def reload(self):
        self._rows = factor_mine.load_library()
        self.tbl.setRowCount(0)
        for i, r in enumerate(self._rows):
            self.tbl.insertRow(i)
            set_cell(self.tbl, i, 0, r.get("expr") or "", align="left")
            set_cell(self.tbl, i, 1, r.get("label") or "", align="left")
            st = r.get("stats") or {}
            set_cell(self.tbl, i, 2, _fmt(st.get("test_ic", st.get("ic")), "+.3f"))
            set_cell(self.tbl, i, 3, _fmt(st.get("stability"), ".2f"))
            set_cell(self.tbl, i, 4, r.get("created") or "", align="left")
        self.hint.setText(f"共 {len(self._rows)} 条")

    def _picked(self):
        row = self.tbl.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _copy(self):
        r = self._picked()
        if r:
            QGuiApplication.clipboard().setText(r.get("expr") or "")

    def _del(self):
        r = self._picked()
        if r and factor_mine.remove_factor(r.get("expr") or ""):
            self.reload()
            self.changed.emit()


class _MarketPanel(QWidget):
    status = Signal(str)
    round_signal = Signal(dict)

    def __init__(self, market: str, title: str, parent=None):
        super().__init__(parent)
        self.market = market
        self.title = title
        self._tasks: list[TaskRunner] = []
        self._rows: list[dict] = []
        self._res: dict = {}
        self._stop_event = threading.Event()
        self.round_signal.connect(self._round_update)
        self._build()
        self.load_saved()

    # ------------------------------------------------------------------ UI
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.btn = QPushButton("一键挖掘(推荐)")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.run)
        head.addWidget(self.btn)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_mining)
        head.addWidget(self.btn_stop)
        self.btn_adv = QPushButton("高级设置")
        self.btn_adv.setCheckable(True)
        self.btn_adv.toggled.connect(self._toggle_adv)
        head.addWidget(self.btn_adv)
        b_help = QPushButton("怎么用")
        b_help.clicked.connect(lambda: _HelpDialog(self).exec())
        head.addWidget(b_help)
        self.hint = label("点「一键挖掘」开始;第一次约 1~3 分钟", dim=True)
        head.addWidget(self.hint, 1)
        root.addLayout(head)

        # 高级设置(默认收起)
        self.adv = QWidget()
        adv = QHBoxLayout(self.adv)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setSpacing(8)
        adv.addWidget(label("股票池", dim=True))
        self.u = QComboBox()
        for n in (200, 300, 600):
            self.u.addItem(f"{n} 只", n)
        self.u.setCurrentIndex(1)
        self.u.setEnabled(self.market == "a")
        adv.addWidget(self.u)
        adv.addWidget(label("历史", dim=True))
        self.days = QComboBox()
        for n in (120, 250, 400):
            self.days.addItem(f"{n} 日", n)
        self.days.setCurrentIndex(1)
        adv.addWidget(self.days)
        adv.addWidget(label("预测期", dim=True))
        self.h = QComboBox()
        for n in (1, 3, 5, 10, 20):
            self.h.addItem(f"{n} 日", n)
        self.h.setCurrentIndex(2)
        adv.addWidget(self.h)
        adv.addWidget(label("模式", dim=True))
        self.mode = QComboBox()
        self.mode.addItem("模板迭代", "tpl")
        self.mode.addItem("遗传(GP)", "gp")
        adv.addWidget(self.mode)
        adv.addWidget(label("轮数", dim=True))
        self.rounds = QComboBox()
        for n in (3, 5, 8, 12, 20):
            self.rounds.addItem(f"{n} 轮", n)
        self.rounds.setCurrentIndex(1)
        adv.addWidget(self.rounds)
        self.loop = QCheckBox("循环")
        self.loop.setToolTip("一轮跑完等 10 秒再挖,直到点停止")
        adv.addWidget(self.loop)
        adv.addStretch(1)
        self.adv.setVisible(False)
        root.addWidget(self.adv)

        body = QHBoxLayout()
        body.setSpacing(8)

        left = Card("候选因子", hint="按样本外表现排序 · 多选后回测")
        self.tbl = table(["因子", "训练IC", "测试IC", "稳定性", "覆盖", "多空", "风险"],
                         widths=[250, 62, 62, 62, 50, 76, 44], stretch_col=0)
        self.tbl.setSelectionMode(self.tbl.SelectionMode.ExtendedSelection)
        self.tbl.itemSelectionChanged.connect(self._on_pick)
        self.tbl.doubleClicked.connect(lambda _i: self.backtest())
        left.add(self.tbl, 1)
        lb = QHBoxLayout()
        self.count = label("", dim=True)
        lb.addWidget(self.count, 1)
        b_bt = QPushButton("回测这组因子")
        b_bt.setObjectName("Primary")
        b_bt.clicked.connect(self.backtest)
        b_save = QPushButton("加入因子库")
        b_save.clicked.connect(self.save_selected)
        b_copy = QPushButton("复制表达式")
        b_copy.clicked.connect(self.copy_combo)
        b_json = QPushButton("导出 JSON")
        b_json.clicked.connect(self.export_json)
        b_lib = QPushButton("因子库")
        b_lib.clicked.connect(self.open_lib)
        for b in (b_bt, b_save, b_copy, b_json, b_lib):
            lb.addWidget(b)
        left.body().addLayout(lb)
        body.addWidget(left, 58)

        right = Card("组合回测", hint="日频 · 等权 Top5 · 含成本")
        grid = QGridLayout()
        grid.setSpacing(6)
        self.s_ret = StatCard("总收益", "-")
        self.s_ann = StatCard("年化", "-")
        self.s_sharpe = StatCard("夏普", "-")
        self.s_mdd = StatCard("最大回撤", "-")
        # 只看绝对收益会被 beta 骗:一段普涨行情里任何策略都赚。
        # 这两个卡是「同期全池等权」和「相对它的超额」。
        self.s_oos = StatCard("超额收益", "-")
        self.s_oos_sharpe = StatCard("同期基准", "-")
        for i, w in enumerate((self.s_ret, self.s_ann, self.s_sharpe,
                               self.s_mdd, self.s_oos, self.s_oos_sharpe)):
            grid.addWidget(w, i // 3, i % 3)
        right.body().addLayout(grid)
        self.curve = _Curve()
        right.add(self.curve, 1)
        self.bt_hint = label("选中 1~5 个因子,点「回测这组因子」", dim=True)
        self.bt_hint.setWordWrap(True)
        right.body().addWidget(self.bt_hint)
        body.addWidget(right, 42)
        root.addLayout(body, 1)

    def _toggle_adv(self, on: bool):
        self.adv.setVisible(bool(on))

    # ------------------------------------------------------------------ 数据
    def load_saved(self):
        doc = factor_mine.load_result(self.market)
        if not doc:
            self.hint.setText("今天还没挖过,点「一键挖掘」")
            return
        self._fill(doc.get("result") or {})
        self.hint.setText(f"上次自动挖掘 {doc.get('date', '')} {doc.get('saved', '')}")

    def _fill(self, res: dict, keep_hint: bool = False):
        self._res = res
        self._rows = res.get("rows") or []
        self.tbl.setRowCount(0)
        for i, r in enumerate(self._rows):
            self.tbl.insertRow(i)
            mark = "★ " if r.get("selected") else ""
            set_cell(self.tbl, i, 0, mark + (r.get("expr") or ""), align="left")
            tr_ic = r.get("train_ic", r.get("ic"))
            set_cell(self.tbl, i, 1, _fmt(tr_ic, "+.3f"), pnl_color(tr_ic))
            set_cell(self.tbl, i, 2, _fmt(r.get("test_ic"), "+.3f"),
                     pnl_color(r.get("test_ic")))
            set_cell(self.tbl, i, 3, _fmt(r.get("stability"), ".2f"))
            set_cell(self.tbl, i, 4, _fmt((r.get("coverage") or 0) * 100, ".0f") + "%")
            set_cell(self.tbl, i, 5, _fmt((r.get("spread") or 0) * 100, "+.2f") + "%",
                     pnl_color(r.get("spread")))
            risk = str(r.get("risk") or ("低" if r.get("oos_ok") else "高"))
            color = DOWN if risk == "低" else WARN if risk == "中" else UP
            set_cell(self.tbl, i, 6, risk, color)
            item = self.tbl.item(i, 0)
            if item is not None:
                item.setToolTip(
                    f'{r.get("label")} · {r.get("expr")}\n'
                    f'训练 IC {r.get("train_ic", r.get("ic"))} / '
                    f'ICIR {r.get("train_icir", r.get("icir"))}\n'
                    f'测试 IC {r.get("test_ic")} / ICIR {r.get("test_icir")}\n'
                    f'稳定性 {r.get("stability")} · 覆盖 {r.get("coverage")} · '
                    f'换手 {r.get("turnover")}')
        sel = sum(1 for r in self._rows if r.get("selected"))
        self.count.setText(f"候选 {len(self._rows)} · 入选 {sel}")
        if self._rows:
            self.tbl.selectRow(0)

    def run(self):
        if self._tasks:
            self.hint.setText("还在跑,等这一轮")
            return
        days = int(self.days.currentData())
        stocks = int(self.u.currentData())
        h = int(self.h.currentData())
        rounds = int(self.rounds.currentData())
        mode = str(self.mode.currentData())
        self._stop_event.clear()
        self.btn.setEnabled(False)
        self.btn_stop.setEnabled(True)
        unit = "代" if mode == "gp" else "轮"
        self.hint.setText(f"挖掘中:{days} 日 × {rounds} {unit} …")
        self.status.emit(f"因子挖掘:{self.title}")
        if mode == "gp":
            from ..engine import factor_gp as gp
            job = TaskRunner(lambda: gp.run(
                days=days, max_stocks=stocks, horizon=h, market=self.market,
                generations=rounds, population=40, time_budget=300,
                progress=lambda d: self.round_signal.emit(d),
                should_stop=self._stop_event.is_set))
        else:
            job = TaskRunner(lambda: factor_mine.iter_mine(
                days=days, max_stocks=stocks, horizon=h, market=self.market,
                rounds=rounds,
                progress=lambda d: self.round_signal.emit(d),
                should_stop=self._stop_event.is_set))
        job.succeeded.connect(self._ok)
        job.failed.connect(lambda m: self.hint.setText(f"失败:{m.splitlines()[0][:90]}"))
        job.finished.connect(lambda: self._done(job))
        self._tasks.append(job)
        job.start()

    def _done(self, job):
        if job in self._tasks:
            self._tasks.remove(job)
        self.btn.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _round_update(self, d: dict):
        rows = d.get("rows") or []
        sel = set(d.get("selected_exprs") or [])
        if rows:
            self._fill({"rows": [{**r, "selected": r.get("expr") in sel}
                                 for r in rows]}, keep_hint=True)
        if "generation" in d:
            self.hint.setText(f"第 {d.get('generation')}/{d.get('total')} 代 · "
                              f"已评估 {d.get('evaluated')} 个表达式")
        else:
            self.hint.setText(f"第 {d.get('round')}/{d.get('total')} 轮 · "
                              f"候选 {d.get('pool')} · 新增 {d.get('new')}")

    def stop_mining(self):
        self._stop_event.set()
        self.loop.setChecked(False)
        self.hint.setText("正在停止…")

    def _ok(self, d: dict):
        d = d if isinstance(d, dict) else {}
        if not d.get("ok"):
            self.hint.setText(str(d.get("error") or "没有结果"))
            return
        factor_mine.save_result(self.market, d)
        self._fill(d)
        sel = len(d.get("selected") or [])
        msg = (f"{d.get('n_codes')} 只 × {d.get('days')} 日 · "
               f"{d.get('n_candidates')} 个候选 · "
               f"{d.get('rounds')} 轮 · {d.get('cost_seconds')}s")
        if not sel:
            msg += (" · 没有因子通过样本外门槛(训练 IC≥0.010、测试 IC≥0.015、"
                    "测试不超训练 3 倍+0.02)。在日线数据上这是常态,不是 bug")
        self.hint.setText(msg)
        if self.loop.isChecked() and not self._stop_event.is_set():
            QTimer.singleShot(10000, self.run)

    def _picked_rows(self) -> list[dict]:
        from .common import row_key
        keys = {row_key(self.tbl, i.row()) for i in self.tbl.selectedIndexes()}
        out = [r for r in self._rows if (r.get("expr") or "") in keys]
        if not out:
            out = [r for r in self._rows if r.get("selected")][:3]
        return out[:6]

    def _on_pick(self):
        rows = self._picked_rows()
        if rows:
            self.bt_hint.setText(
                "已选:" + " + ".join((r.get("label") or r.get("expr") or "")
                                    for r in rows)
                + "  → 可回测 / 复制表达式 / 加入因子库")

    # ------------------------------------------------------------------ 回测
    def backtest(self):
        rows = self._picked_rows()
        if not rows:
            self.bt_hint.setText("先选因子")
            return
        if self._tasks:
            return
        days = int(self.days.currentData())
        h = int(self.h.currentData())
        self.bt_hint.setText("回测中…")
        job = TaskRunner(lambda: self._run_backtest(rows, days, h))
        job.succeeded.connect(self._bt_ok)
        job.failed.connect(lambda m: self.bt_hint.setText(f"回测失败:{m[:90]}"))
        job.finished.connect(lambda: self._done(job))
        self._tasks.append(job)
        job.start()

    def _run_backtest(self, rows, days, h):
        panel = factor_mine._panel(days, 300, self.market)
        return factor_mine.composite_backtest(panel, rows, horizon=h, top_n=5)

    def _bt_ok(self, d: dict):
        d = d if isinstance(d, dict) else {}
        if not d.get("ok"):
            self.bt_hint.setText(str(d.get("error") or "回测失败"))
            return
        m = d.get("metrics") or {}
        self.curve.show_curve(d.get("curve") or [])
        self.s_ret.set(_fmt(m.get("total_return_pct"), "+.1f") + "%",
                       pnl_color(m.get("total_return_pct")))
        self.s_ann.set(_fmt(m.get("annual_return_pct"), "+.1f") + "%",
                       pnl_color(m.get("annual_return_pct")))
        self.s_sharpe.set(_fmt(m.get("sharpe"), "+.2f"),
                          pnl_color(m.get("sharpe")))
        self.s_mdd.set(_fmt(m.get("max_drawdown_pct"), ".1f") + "%", DOWN)
        self.s_oos.set(_fmt(m.get("excess_total_pct"), "+.1f") + "%",
                       pnl_color(m.get("excess_total_pct")))
        self.s_oos_sharpe.set(_fmt(m.get("bench_total_pct"), "+.1f") + "%",
                              pnl_color(m.get("bench_total_pct")))
        # 池子是按「样本起点」的成交额选的,不是按今天 —— 否则是幸存者偏差。
        # 超额和基准放在卡上,样本外数据放这行提示里。
        self.bt_hint.setText(
            f'{m.get("n_days")} 天 · 换手 {m.get("turnover_avg")} · '
            f'成本 {m.get("cost")} · 样本外 {_fmt(m.get("oos_return_pct"), "+.1f")}%'
            f' · 最新持有:{"、".join((d.get("holdings") or [])[:5])}')

    # ------------------------------------------------------------------ 取用
    def _combo_expr(self, rows: list[dict]) -> str:
        parts = []
        for r in rows:
            expr = str(r.get("expr") or "")
            if not expr:
                continue
            term = f"rank({expr})"
            sign = float(r.get("test_ic", r.get("ic")) or 0) >= 0
            parts.append(("+" if sign else "-") + term)
        if not parts:
            return ""
        text = "".join(parts)
        return text[1:] if text[0] == "+" else text

    def copy_combo(self):
        expr = self._combo_expr(self._picked_rows())
        if expr:
            QGuiApplication.clipboard().setText(expr)
            self.bt_hint.setText("组合表达式已复制 → 去「选股」页粘到条件里即可")

    def save_selected(self):
        rows = self._picked_rows()
        n = 0
        for r in rows:
            if factor_mine.save_factor(r.get("expr") or "", r.get("label") or "", r):
                n += 1
        self.bt_hint.setText(f"已保存 {n} 个到因子库(可在「因子库」里复制/删除)")

    def export_json(self):
        rows = self._picked_rows()
        if not rows:
            return
        name = f"factor_combo_{self.market}_{time.strftime('%Y%m%d')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出因子组合", str(USER_DIR / name), "JSON (*.json)")
        if not path:
            return
        doc = {"market": self.market, "horizon": int(self.h.currentData()),
               "created": time.strftime("%Y-%m-%d %H:%M"),
               "combo_expr": self._combo_expr(rows),
               "factors": [{k: r.get(k) for k in
                            ("expr", "label", "train_ic", "test_ic",
                             "stability", "coverage", "turnover", "spread")}
                           for r in rows]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            self.bt_hint.setText(f"已导出:{path}")
        except OSError as e:
            self.bt_hint.setText(f"导出失败:{e}")

    def open_lib(self):
        _LibraryDialog(self).exec()

    def stop(self):
        for t in list(self._tasks):
            t.wait(6000)
        self._tasks.clear()


class FactorMinePage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top = Card("策略因子挖掘",
                   hint="自动找「和未来涨跌有关系」的公式 · 样本外验证 · 可直接用于选股/回测")
        bar = QHBoxLayout()
        self.auto_lbl = label("自动挖掘:今天还没跑", dim=True)
        bar.addWidget(self.auto_lbl, 1)
        b_all = QPushButton("全部重跑")
        b_all.clicked.connect(self.run_all)
        bar.addWidget(b_all)
        b_lib = QPushButton("因子库")
        b_lib.clicked.connect(lambda: _LibraryDialog(self).exec())
        bar.addWidget(b_lib)
        top.body().addLayout(bar)
        root.addWidget(top)

        # 公开版只做 A 股:加密面板依赖 quantlab.crypto,这个仓库没有,留着必崩
        self.panels = {"a": _MarketPanel("a", "A股", self)}
        self.panels["a"].status.connect(self.status)
        root.addWidget(self.panels["a"], 1)
        self.refresh_auto()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh_auto()

    def refresh_auto(self):
        marks = []
        for key, name in (("a", "A股"),):
            doc = factor_mine.load_result(key)
            if not doc:
                marks.append(f"{name}: 未跑")
                continue
            res = doc.get("result") or {}
            sel = len(res.get("selected") or [])
            marks.append(f"{name} {doc.get('saved', '')} · "
                         f"{len(res.get('rows') or [])} 候选 / {sel} 入选")
            self.panels[key].load_saved()
        self.auto_lbl.setText("自动挖掘 · " + "   |   ".join(marks))

    def run_all(self):
        for key in ("a",):
            panel = self.panels[key]
            if not panel._tasks:
                panel.run()

    def stop(self):
        for p in self.panels.values():
            p.stop()
