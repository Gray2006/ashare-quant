# -*- coding: utf-8 -*-
"""技术分析页:一张 K 线图叠分析层 + 四块判定面板。

图上叠什么由勾选框决定(缠论笔/线段/中枢、道氏趋势线、订单块、买卖点),
引擎在 engine/ 下各算各的,这里只负责画和摆表格 —— 两边不重复算。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QVBoxLayout,
                               QWidget)

from ..data.kline import get_kline
from ..engine import chan, divergence, dow, resonance, smartmoney
from ..engine.hub import TaskRunner
from .board import Board
from .charts import KLineWidget
from .common import Card, StatCard, label, set_cell, table
from .theme import ACCENT, DOWN, TEXT_DIM, TEXT_MUT, UP, WARN

# 各层的颜色,图例也用它
C_CHAN = "#38bdf8"      # 缠论笔
C_LINE = "#a855f7"      # 线段
C_CENTER = "#f59e0b"    # 中枢
C_DOW = "#22d3ee"       # 道氏趋势线
C_OB = "#f472b6"        # 订单块
C_BUY = UP              # 买点(A 股红涨)
C_SELL = DOWN

PERIODS = [("日线", 101), ("周线", 102), ("60 分钟", 60), ("30 分钟", 30),
           ("15 分钟", 15)]

STYLES = [("蜡烛图", "candle"), ("美国线", "bar"), ("收盘线", "close"),
          ("山形线", "area")]


class TAPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._bars: list[dict] = []
        self._view: list[dict] = []       # 回放时只给到当前位置,不偷看未来
        self._pos = 0                     # 0 = 全览
        self._reps: dict = {}
        self._play = QTimer(self)
        self._play.setInterval(420)
        self._play.timeout.connect(self._replay_step)
        self._build()
        self.reload_codes()
        QTimer.singleShot(700, self.refresh)

    def showEvent(self, ev):  # noqa: N802
        """切到本页时重画一次。

        页面在后台时图表控件拿不到真实尺寸,pyqtgraph 画出来的内容尺寸为 0;
        切到前台后不会自动重画,所以显式补一次。
        """
        super().showEvent(ev)
        QTimer.singleShot(0, self._redraw)

    # ------------------------------------------------------------------ 界面
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        board = Board("ta", cols=[1.0], rows=[0.12, 0.58, 0.30])
        root.addWidget(board)      # 不进布局 = 整页空白(技术分析页以前一直空)

        # 顶栏
        top = Board("ta.top", cols=[0.34, 0.30, 0.36], rows=[1.0])
        box = Card("标的")
        bl = QHBoxLayout()
        bl.setSpacing(6)
        self.code = QComboBox()
        self.code.setEditable(True)
        self.code.setMinimumWidth(150)
        self.code.lineEdit().setPlaceholderText("代码 或 拼音")
        self.period = QComboBox()
        for name, klt in PERIODS:
            self.period.addItem(name, klt)
        self.btn = QPushButton("重算")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        for w in (self.code, self.period, self.btn):
            bl.addWidget(w)
        box.body().addLayout(bl)
        self.hint = label("选一只股票点「重算」", dim=True, size=11)
        box.body().addWidget(self.hint)
        top.add(box, 0, 0, min_w=380, min_h=84)

        # 共振结论
        summ = Card("共振")
        sl = QHBoxLayout()
        sl.setSpacing(8)
        self.st_res = StatCard("一致性", "-", "")
        self.st_chan = StatCard("缠论结构", "-", "")
        self.st_dow = StatCard("道氏", "-", "")
        self.st_sm = StatCard("智能资金", "-", "")
        for w in (self.st_res, self.st_chan, self.st_dow, self.st_sm):
            sl.addWidget(w)
        summ.body().addLayout(sl)
        top.add(summ, 0, 1, min_w=420, min_h=84)

        # 图层开关
        lay = Card("图层")
        ll = QHBoxLayout()
        ll.setSpacing(10)
        self.cb_stroke = QCheckBox("缠论笔")
        self.cb_line = QCheckBox("线段")
        self.cb_center = QCheckBox("中枢")
        self.cb_dow = QCheckBox("道氏线")
        self.cb_ob = QCheckBox("订单块")
        self.cb_pt = QCheckBox("买卖点")
        self.cb_ma = QCheckBox("均线")
        for cb, on in ((self.cb_stroke, True), (self.cb_line, False),
                       (self.cb_center, True), (self.cb_dow, False),
                       (self.cb_ob, True), (self.cb_pt, True), (self.cb_ma, True)):
            cb.setChecked(on)
            cb.stateChanged.connect(self._redraw)
            ll.addWidget(cb)
        ll.addStretch(1)
        lay.body().addLayout(ll)

        # 图表类型 + 回放
        rl = QHBoxLayout()
        rl.setSpacing(6)
        rl.addWidget(label("图形", dim=True, size=11))
        self.style_box = QComboBox()
        for name, key in STYLES:
            self.style_box.addItem(name, key)
        self.style_box.currentIndexChanged.connect(self._on_style)
        rl.addWidget(self.style_box)
        self.btn_play = QPushButton("回放")
        self.btn_play.setCheckable(True)
        self.btn_play.setToolTip("逐根重放:每一步都只用当时已知的 K 线重新算")
        self.btn_play.toggled.connect(self._replay_toggle)
        rl.addWidget(self.btn_play)
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedWidth(30)
        self.btn_prev.clicked.connect(lambda: self._replay_step(-1))
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedWidth(30)
        self.btn_next.clicked.connect(lambda: self._replay_step(1))
        rl.addWidget(self.btn_prev)
        rl.addWidget(self.btn_next)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1)
        self.slider.setValue(0)
        self.slider.valueChanged.connect(self._on_slider)
        rl.addWidget(self.slider, 1)
        lay.body().addLayout(rl)
        self.rp_label = label("全览", dim=True, size=10)
        lay.body().addWidget(self.rp_label)
        hint2 = label("图层只是画法,判定结果看下面三块", dim=True, size=10)
        lay.body().addWidget(hint2)
        top.add(lay, 0, 2, min_w=360, min_h=84)
        board.add(top, 0, 0, min_h=88)

        # 主图
        chart = Card("K 线 + 分析层")
        self.chart = KLineWidget()
        chart.add(self.chart, 1)
        self.legend = label("", dim=True, size=10)
        chart.add(self.legend)
        board.add(chart, 1, 0, min_h=300)

        # 底部三块
        bot = Board("ta.bot", cols=[0.40, 0.30, 0.30], rows=[1.0])
        vc = Card("共振投票", hint="每项独立判,别当预测")
        self.vote_tbl = table(["指标", "票", "依据"], widths=[86, 44, 260], stretch_col=2)
        self.vote_tbl.setObjectName("TAVote")
        vc.add(self.vote_tbl, 1)
        bot.add(vc, 0, 0, min_w=340, min_h=150)

        cc = Card("缠论", hint="简化实现,见引擎注释")
        self.chan_txt = QLabel("")
        self.chan_txt.setWordWrap(True)
        self.chan_txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.chan_txt.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;padding:2px;")
        cc.add(self.chan_txt, 1)
        bot.add(cc, 0, 1, min_w=260, min_h=150)

        oc = Card("道氏 / 智能资金 / 背离")
        self.other_txt = QLabel("")
        self.other_txt.setWordWrap(True)
        self.other_txt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.other_txt.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;padding:2px;")
        oc.add(self.other_txt, 1)
        bot.add(oc, 0, 2, min_w=260, min_h=150)
        board.add(bot, 2, 0, min_h=150)

    def reload_codes(self):
        """代码下拉:优先用自选,取不到就留空让用户自己输。"""
        try:
            from ..data.store import store
            rows = store().get_watchlist() or []
        except Exception:  # noqa: BLE001
            rows = []
        self.code.clear()
        for r in rows:
            code = r.get("code") if isinstance(r, dict) else r
            name = r.get("name", "") if isinstance(r, dict) else ""
            if code:
                self.code.addItem(f"{code} {name}".strip(), code)
        if not self.code.count():
            self.code.addItem("600519 贵州茅台", "600519")

    # ------------------------------------------------------------------ 取数
    def refresh(self):
        raw = (self.code.currentData() or self.code.currentText() or "").strip()
        code = "".join(ch for ch in raw.split()[0] if ch.isdigit()) if raw else ""
        if len(code) != 6:
            self.hint.setText("代码要 6 位数字,比如 600519")
            return
        klt = self.period.currentData() or 101
        self.btn.setEnabled(False)
        self.hint.setText(f"{code} 取数中…")

        def job():
            bars, src = get_kline(code, klt, 300)
            rep = {
                "chan": chan.analyze(bars),
                "dow": dow.analyze(bars),
                "sm": smartmoney.analyze(bars),
                "div": divergence.analyze(bars),
                "res": resonance.analyze(bars),
            }
            return code, src, bars, rep

        r = TaskRunner(job)
        r.succeeded.connect(self._ok)
        r.failed.connect(self._err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _ok(self, payload):
        code, src, bars, rep = payload
        self._bars = bars
        self._view = list(bars)
        self._pos = 0
        self._play.stop()
        self.btn_play.setChecked(False)
        self.btn_play.setText("回放")
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(1, len(bars)))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self._reps = rep
        if not bars:
            self.hint.setText(f"{code} 没取到 K 线")
            return
        self.hint.setText(f"{code} · 来源 {src} · {len(bars)} 根")
        self.rp_label.setText("全览")
        self._render_tables(rep)
        self._redraw()
        self.status.emit(f"技术分析 {code} 完成")

    def _err(self, msg):
        self.hint.setText(f"失败:{str(msg)[:80]}")

    # ------------------------------------------------------------------ 回放
    def _on_style(self):
        kind = self.style_box.currentData() or "candle"
        self.chart.set_style(kind)
        self._redraw()

    def _replay_toggle(self, on: bool):
        if on:
            if not self._bars:
                self.btn_play.setChecked(False)
                return
            # 从「最近 60 根之前」开始放,太靠前看不到结构
            if self._pos <= 0:
                self._pos = max(30, len(self._bars) - 60)
                self.slider.setValue(self._pos)
            self._play.start()
            self.btn_play.setText("暂停")
            self.rp_label.setText(f"回放中 · 第 {self._pos} / {len(self._bars)} 根")
        else:
            self._play.stop()
            self.btn_play.setText("回放")
        self._replay_apply()

    def _replay_step(self, delta: int = 1):
        if not self._bars:
            return
        pos = self._pos + delta
        pos = max(20, min(len(self._bars), pos))
        self._pos = pos
        self.slider.blockSignals(True)
        self.slider.setValue(pos)
        self.slider.blockSignals(False)
        self._replay_apply()

    def _on_slider(self, v: int):
        if not self._bars:
            return
        self._pos = int(v)
        self._replay_apply()

    def _replay_apply(self):
        """按当前位置截断 K 线,再重算 —— 保证每一步都看不到未来。"""
        if not self._bars:
            return
        if self._pos <= 0 or self._pos >= len(self._bars):
            self._view = list(self._bars)
            self.rp_label.setText("全览")
            self._reps = self._analyze(self._view)
        else:
            self._view = list(self._bars[:self._pos])
            self.rp_label.setText(
                f"第 {self._pos} / {len(self._bars)} 根 · "
                f"{str(self._view[-1].get('datetime') or '')[:10]}(其后数据已遮挡)")
            self._reps = self._analyze(self._view)
        self._render_tables(self._reps)
        self._redraw()

    @staticmethod
    def _analyze(bars: list[dict]) -> dict:
        return {"chan": chan.analyze(bars), "dow": dow.analyze(bars),
                "sm": smartmoney.analyze(bars), "div": divergence.analyze(bars),
                "res": resonance.analyze(bars)}

    # ------------------------------------------------------------------ 画图
    def _redraw(self):
        if not self._view:
            return
        from ..engine.indicators import bars_to_df
        df = bars_to_df(self._view)
        overlay = self._overlay_items()
        self.chart.plot(df, overlay=overlay)
        self.legend.setText(self._legend_text())

    def _overlay_items(self) -> list:
        items: list = []
        rep = self._reps or {}
        bars = self._view
        n = len(bars)

        ch = rep.get("chan") or {}
        if ch.get("ok"):
            # 笔:折线,顶点用底/顶分型价格
            if self.cb_stroke.isChecked() and ch.get("strokes"):
                pts = [(ch["strokes"][0]["i0"], ch["strokes"][0]["from"]["price"])]
                for s in ch["strokes"]:
                    pts.append((s["i1"], s["to"]["price"]))
                items.append({"type": "line", "points": pts, "color": C_CHAN, "width": 1.6})
            if self.cb_line.isChecked() and ch.get("lines"):
                for ln in ch["lines"]:
                    items.append({"type": "hline", "y": ln["high"], "color": C_LINE,
                                  "width": 1.0, "dash": True})
            if self.cb_center.isChecked():
                for c in ch.get("centers") or []:
                    items.append({"type": "box", "x0": c["i0"], "x1": c["i1"],
                                  "y0": c["zd"], "y1": c["zg"],
                                  "color": C_CENTER, "alpha": 0.14})
            if self.cb_pt.isChecked():
                for p in ch.get("points") or []:
                    buy = "买" in p["kind"]
                    items.append({"type": "tag", "x": p["i"], "y": p["price"],
                                  "text": p["kind"],
                                  "color": C_BUY if buy else C_SELL})
        dw = rep.get("dow") or {}
        if dw.get("ok") and self.cb_dow.isChecked():
            for ln in dw.get("trendlines") or []:
                a, b = ln["a"], ln["b"]
                x1 = n - 1
                y1 = b["price"] + ln["slope"] * (x1 - b["i"])
                items.append({"type": "line",
                              "points": [(a["i"], a["price"]), (x1, y1)],
                              "color": C_DOW, "width": 1.2, "dash": True})
        sm = rep.get("sm") or {}
        if sm.get("ok") and self.cb_ob.isChecked():
            for ob in sm.get("order_blocks") or []:
                items.append({"type": "box", "x0": ob["i"], "x1": n - 1,
                              "y0": ob["lo"], "y1": ob["hi"],
                              "color": C_OB, "alpha": 0.10 if ob["filled"] else 0.18})
        return items

    def _legend_text(self) -> str:
        parts = []
        if self.cb_stroke.isChecked():
            parts.append(f"<span style='color:{C_CHAN}'>━ 缠论笔</span>")
        if self.cb_center.isChecked():
            parts.append(f"<span style='color:{C_CENTER}'>▭ 中枢</span>")
        if self.cb_dow.isChecked():
            parts.append(f"<span style='color:{C_DOW}'>┅ 道氏线</span>")
        if self.cb_ob.isChecked():
            parts.append(f"<span style='color:{C_OB}'>▭ 订单块</span>")
        if self.cb_pt.isChecked():
            parts.append(f"<span style='color:{C_BUY}'>▲买</span> "
                         f"<span style='color:{C_SELL}'>▼卖</span>")
        parts.append("<span style='color:#5d6a7a'>虚线=延伸/未确认</span>")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 表格
    def _render_tables(self, rep):
        res = rep.get("res") or {}
        vs = res.get("votes") or []
        self.vote_tbl.setRowCount(len(vs))
        for r, v in enumerate(vs):
            vote = v["vote"]
            set_cell(self.vote_tbl, r, 0, v["name"], TEXT_DIM, align="left")
            set_cell(self.vote_tbl, r, 1, {1: "多", 0: "中", -1: "空"}.get(vote, "-"),
                     UP if vote > 0 else DOWN if vote < 0 else TEXT_MUT, align="center")
            set_cell(self.vote_tbl, r, 2, v["why"], TEXT_DIM, align="left")

        if res.get("ok"):
            color = UP if res["level"] == "bull" else DOWN if res["level"] == "bear" else WARN
            # 四块并排,每块只有 ~90px:结论用两字简称,全文进 tooltip
            short = {"多头共振": "多头", "空头共振": "空头", "多空分歧": "分歧",
                     "略微偏多": "偏多", "略微偏空": "偏空"}
            self.st_res.set(short.get(res["verdict"], res["verdict"]), color,
                            f"多{res['bull']} 空{res['bear']} 中{res['flat']}")
            self.st_res.val.setToolTip(
                f"{res['verdict']} · 看多 {res['bull']} / 看空 {res['bear']} / "
                f"中性 {res['flat']} · 净分 {res['score']:+d}")
        else:
            self.st_res.set("-", TEXT_MUT, res.get("reason", "")[:9])

        ch = rep.get("chan") or {}
        if ch.get("ok"):
            self.st_chan.set(ch["trend"], ACCENT if ch["trend"] != "盘整" else WARN,
                             f"笔{ch['stroke_n']} 中枢{ch['center_n']}")
            self.chan_txt.setText(chan.describe(ch))
        else:
            self.st_chan.set("-", TEXT_MUT, "")
            self.chan_txt.setText(ch.get("reason", "缠论算不出来"))

        dw = rep.get("dow") or {}
        if dw.get("ok"):
            kind = dw["structure"]["kind"]
            self.st_dow.set(kind, UP if "上升" in kind else DOWN if "下降" in kind else WARN,
                            dw["structure"]["detail"][:9])
            self.st_dow.hint.setToolTip(dw["structure"]["detail"])
        else:
            self.st_dow.set("-", TEXT_MUT, "")

        sm = rep.get("sm") or {}
        if sm.get("ok"):
            self.st_sm.set(sm["structure"].split(" · ")[0],
                           UP if "上升" in sm["structure"] else
                           DOWN if "下降" in sm["structure"] else WARN,
                           f"订单块{sm['ob_n']} 缺口{sm['fvg_n']}")
            self.st_sm.hint.setToolTip(sm["structure"])
        else:
            self.st_sm.set("-", TEXT_MUT, "")

        lines_txt = []
        lines_txt.append("<b>道氏</b> " + (dow.describe(dw) if dw.get("ok")
                                          else dw.get("reason", "")))
        lines_txt.append("<b>智能资金</b> " + (smartmoney.describe(sm) if sm.get("ok")
                                              else sm.get("reason", "")))
        dv = rep.get("div") or {}
        lines_txt.append("<b>背离</b> " + (divergence.describe(dv) if dv.get("ok")
                                          else dv.get("reason", "")))
        self.other_txt.setText("<br><br>".join(lines_txt))

    def stop(self):
        self._play.stop()
        for t in self._tasks:
            if t.isRunning():
                t.wait(800)
        self._tasks.clear()
