# -*- coding: utf-8 -*-
"""机体学习程度页 —— 暗色 + 发光 + 动效。

三块:
  左  能力雷达(七条线) + 数据规模
  中  训练曲线(loss / 准确率),真训出来的
  右  成长指标 + 进化时间线

可交互:
  「重新训练」跑一次逻辑回归,曲线刷新
  「刷新状态」重取快照
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from ..engine import organism as og
from .board import Board
from .common import Card, StatCard, label, set_cell, table
from .theme import BG, DOWN, TEXT, TEXT_MUT, UP

CYAN = "#5ee7f0"
VIOLET = "#a78bfa"
AMBER = "#fbbf24"
GREEN = "#34d399"
RED = "#f87171"


class _Ring(QWidget):
    """能力分布环:通过 / 待跟 / 证否 三段弧 + 中心数字。

    比雷达直观 —— 八条线里几条赢了、几条输了,一眼看到比例。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self._acc = 0
        self._pen = 0
        self._rej = 0
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_data(self, acc: int, pen: int, rej: int) -> None:
        self._acc, self._pen, self._rej = acc, pen, rej
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.02) % (2 * math.pi)
        self.update()

    def paintEvent(self, _e) -> None:      # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        total = max(1, self._acc + self._pen + self._rej)
        size = min(w, h) - 24
        if size < 40:
            return
        cx, cy = w / 2, h / 2
        rect = QRectF(cx - size / 2, cy - size / 2, size, size)
        thick = max(10, size * 0.16)

        # 底环
        p.setPen(QPen(QColor(255, 255, 255, 16), thick, Qt.SolidLine, Qt.FlatCap))
        p.drawArc(rect.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2),
                  0, 360 * 16)

        # 三段弧:从 90° 开始顺时针
        start_angle = 90 * 16
        segs = [(self._acc, GREEN), (self._pen, AMBER),
                (self._rej, RED)]
        for cnt, col in segs:
            if cnt <= 0:
                continue
            span = int(-360 * 16 * cnt / total)
            p.setPen(QPen(QColor(col), thick, Qt.SolidLine, Qt.FlatCap))
            p.drawArc(rect.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2),
                      start_angle, span)
            start_angle += span

        # 中心数字(带呼吸)
        glow = 0.85 + 0.15 * math.sin(self._phase)
        p.setPen(QColor(int(230 * glow), int(240 * glow), int(250 * glow)))
        f = p.font()
        f.setPointSize(max(14, int(size * 0.2)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignCenter, f"{self._acc}/{total}")
        f.setPointSize(8)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QPen(QColor(TEXT_MUT)))
        p.drawText(QRectF(cx - size / 2, cy + size * 0.18, size, 16),
                   Qt.AlignCenter, "通过")


class _LineBars(QWidget):
    """横向条形列表:每条线一行,条长表示进度,颜色表示状态。

    比雷达图好读 —— 名字、结论、状态都能直接看见,不用猜颜色对应哪根轴。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self._rows: list[tuple[str, str, str, str]] = []

    def set_data(self, lines: list[dict]) -> None:
        rows = []
        for ln in lines:
            st = str(ln.get("status") or "")
            val, col = {
                "accepted": (1.0, GREEN),
                "pending": (0.55, AMBER),
            }.get(st, (0.18, RED))
            rows.append((str(ln.get("name") or ""), str(ln.get("detail") or ""),
                         col, str(val)))
        self._rows = rows
        self.update()

    def paintEvent(self, _e) -> None:      # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._rows)
        if n == 0:
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "等待数据")
            return
        row_h = min(34.0, h / n)
        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        fm = p.fontMetrics()
        name_w = 96
        for i, (name, detail, col, val) in enumerate(self._rows):
            y = i * row_h
            # 名字
            p.setPen(QPen(QColor(TEXT)))
            p.drawText(QRectF(2, y, name_w - 6, row_h), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(name, Qt.ElideRight, name_w - 10))
            # 条
            bar_x = name_w
            bar_w = max(20.0, w - name_w - 8)
            bar_y = y + row_h * 0.32
            bar_h = max(4.0, row_h * 0.36)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 12))
            p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), bar_h / 2, bar_h / 2)
            # QColor 认 #AARRGGBB,不认 #RRGGBBAA —— 拼 "cc" 会把 alpha 当成红分量
            bc = QColor(col)
            bc.setAlpha(210)
            p.setBrush(bc)
            p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w * float(val), bar_h),
                              bar_h / 2, bar_h / 2)
            # 结论(条上方小字)
            f2 = p.font()
            f2.setPointSize(7)
            p.setFont(f2)
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(bar_x, y + row_h * 0.02, bar_w, row_h * 0.3),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(detail, Qt.ElideRight, int(bar_w)))
            p.setFont(f)
        # 图例
        f.setPointSize(7)
        p.setFont(f)
        lx = 2.0
        for txt, col in (("通过", GREEN), ("待跟", AMBER), ("证否", RED)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col))
            p.drawEllipse(QPointF(lx + 4, h - 8), 3, 3)
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(lx + 10, h - 16, 34, 14),
                       Qt.AlignLeft | Qt.AlignVCenter, txt)
            lx += 44


class _TrainCurve(QWidget):
    """训练曲线:loss(左轴) + 准确率(右轴)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self._curve: list[dict] = []
        self._base = 0.0

    def set_data(self, curve: list[dict], baseline: float = 0.0) -> None:
        self._curve = curve or []
        self._base = baseline
        self.update()

    def paintEvent(self, _e) -> None:      # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 42, 46, 14, 22
        iw, ih = w - pad_l - pad_r, h - pad_t - pad_b
        p.fillRect(0, 0, w, h, QColor(BG))

        if not self._curve:
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "还没训练过")
            return

        n = len(self._curve)
        losses = [c["loss"] for c in self._curve]
        accs = [c["test_acc"] for c in self._curve]
        lo, hi = min(losses), max(losses)
        span = (hi - lo) or 1.0

        # 网格
        for i in range(5):
            y = pad_t + ih * i / 4
            p.setPen(QPen(QColor(255, 255, 255, 12), 1))
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + iw, y))

        def px(i):
            return pad_l + iw * i / max(1, n - 1)

        # loss 曲线(青,带渐变填充)
        path = QPainterPath()
        for i, v in enumerate(losses):
            y = pad_t + ih * (1 - (v - lo) / span)
            if i == 0:
                path.moveTo(px(i), y)
            else:
                path.lineTo(px(i), y)
        fill = QPainterPath(path)
        fill.lineTo(px(n - 1), pad_t + ih)
        fill.lineTo(px(0), pad_t + ih)
        fill.closeSubpath()
        gfill = QLinearGradient(0, pad_t, 0, pad_t + ih)
        gfill.setColorAt(0.0, QColor(94, 231, 240, 70))
        gfill.setColorAt(1.0, QColor(94, 231, 240, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(gfill)
        p.drawPath(fill)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(CYAN), 2))
        p.drawPath(path)

        # 准确率(亮绿)用右轴:自己的刻度区间,不然和 loss/基线挤在一起看不出趋势
        a_lo = min(accs + [self._base or 0.5])
        a_hi = max(accs + [self._base or 0.5])
        a_span = (a_hi - a_lo) or 0.01
        path2 = QPainterPath()
        for i, v in enumerate(accs):
            y = pad_t + ih * (1 - (v - a_lo) / a_span)
            if i == 0:
                path2.moveTo(px(i), y)
            else:
                path2.lineTo(px(i), y)
        p.setPen(QPen(QColor(GREEN), 2.5))
        p.drawPath(path2)
        # 末端亮点
        lastx = px(n - 1)
        lasty = pad_t + ih * (1 - (accs[-1] - a_lo) / a_span)
        p.setPen(Qt.NoPen)
        halo = QColor(GREEN)
        halo.setAlpha(85)
        p.setBrush(halo)
        p.drawEllipse(QPointF(lastx, lasty), 8, 8)
        p.setBrush(QColor(GREEN))
        p.drawEllipse(QPointF(lastx, lasty), 4, 4)

        # 基线(虚线)
        if self._base:
            y = pad_t + ih * (1 - (self._base - a_lo) / a_span)
            pen = QPen(QColor(AMBER), 1)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + iw, y))
            p.setPen(QPen(QColor(AMBER)))
            p.drawText(QRectF(pad_l + iw - 90, y - 16, 90, 14),
                       Qt.AlignRight, f"基线 {self._base:.3f}")

        # 轴标
        p.setPen(QPen(QColor(TEXT_MUT)))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        p.drawText(QRectF(2, pad_t - 8, pad_l - 6, 14), Qt.AlignRight,
                   f"{hi:.3f}")
        p.drawText(QRectF(2, pad_t + ih - 6, pad_l - 6, 14), Qt.AlignRight,
                   f"{lo:.3f}")
        p.setPen(QPen(QColor(CYAN)))
        p.drawText(QRectF(pad_l, 0, 60, 14), Qt.AlignLeft, "loss")
        p.setPen(QPen(QColor(VIOLET)))
        p.drawText(QRectF(pad_l + 56, 0, 80, 14), Qt.AlignLeft, "test acc")

class OrganismPage(QWidget):
    """机体学习程度。"""

    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snap: dict = {}
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(60000)
        QTimer.singleShot(200, self.refresh)

    # ------------------------------------------------------------------ 布局
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(14, 8, 14, 8)
        title = QLabel("机体学习程度")
        title.setStyleSheet(
            f"color:{TEXT};font-size:15px;font-weight:600;letter-spacing:2px;")
        hl.addWidget(title)
        self.sub = label("正在读取…", dim=True)
        hl.addWidget(self.sub)
        hl.addStretch(1)
        self.btn_train = QPushButton("重新训练")
        self.btn_train.setToolTip("用低波动+动量预测三重障碍,跑一次逻辑回归")
        self.btn_train.clicked.connect(self._train)
        self.btn_refresh = QPushButton("刷新状态")
        self.btn_refresh.clicked.connect(self.refresh)
        hl.addWidget(self.btn_train)
        hl.addWidget(self.btn_refresh)
        root.addWidget(head)

        board = Board("organism", cols=[0.30, 0.44, 0.26], rows=[1.0])
        root.addWidget(board, 1)

        # 左
        left = Card("八条路走得怎么样", hint="环=赢了几条 · 条=每条进展")
        self.ring = _Ring()
        left.add(self.ring, 0)
        self.bars = _LineBars()
        left.add(self.bars, 1)
        self.data_lbl = label("", dim=True)
        self.data_lbl.setWordWrap(True)
        left.body().addWidget(self.data_lbl)
        board.add(left, 0, 0)

        # 中
        mid = Card("让机器自己学一次", hint="猜「先涨到止盈,还是先跌到止损」")
        self.curve = _TrainCurve()
        mid.add(self.curve, 1)
        grid = QHBoxLayout()
        self.s_auc = StatCard("区分能力", "-")
        self.s_rec = StatCard("抓到上涨", "-")
        self.s_acc = StatCard("整体猜对", "-")
        for c in (self.s_auc, self.s_rec, self.s_acc):
            grid.addWidget(c)
        mid.body().addLayout(grid)
        self.s_cm = label("", dim=True)
        self.s_cm.setWordWrap(True)
        mid.body().addWidget(self.s_cm)
        self.train_hint = label("", dim=True)
        self.train_hint.setWordWrap(True)
        mid.body().addWidget(self.train_hint)
        board.add(mid, 0, 1)

        # 右
        right = Card("成长与进化", hint="试了多少次 vs 真正站住的")
        grow = QHBoxLayout()
        self.s_try = StatCard("试过几次", "-")
        self.s_win = StatCard("真站住", "-")
        self.s_rate = StatCard("命中率", "-")
        for c in (self.s_try, self.s_win, self.s_rate):
            grow.addWidget(c)
        right.body().addLayout(grow)
        self.lines_tbl = table(["线", "状态", "结论"],
                               widths=[92, 46, 200], stretch_col=2)
        right.add(self.lines_tbl, 1)
        self.maint_lbl = label("", dim=True)
        self.maint_lbl.setWordWrap(True)
        right.body().addWidget(self.maint_lbl)
        board.add(right, 0, 2)

    # ------------------------------------------------------------------ 数据
    def refresh(self) -> None:
        try:
            snap = og.snapshot()
        except Exception as e:  # noqa: BLE001
            self.sub.setText(f"读取失败:{e}")
            return
        self._snap = snap
        d = snap.get("data") or {}
        f = snap.get("factors") or {}
        ln = snap.get("lines") or {}
        g = snap.get("growth") or {}
        tr = snap.get("train") or {}

        self.sub.setText(
            f"日线 {d.get('codes', 0)} 只 / {d.get('daily_bars', 0):,} 根 · "
            f"5分钟 {d.get('min5_codes', 0)} 只 · 财务 {d.get('finance_codes', 0)} 只")
        recent = d.get("recent_days") or []
        today_n = recent[-1]["codes"] if recent else 0
        prev_n = recent[-2]["codes"] if len(recent) > 1 else 0
        delta = today_n - prev_n
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        # 当天往往还没补完(盘后任务 19:00 才跑),数字比昨天少很正常,别吓人
        note = "(今天还没补完)" if delta < -200 else ""
        self.sub.setText(
            f"已有数据:股票 {d.get('codes', 0):,} 只 · 日线 "
            f"{d.get('daily_bars', 0):,} 根 · 5分钟线 {d.get('min5_codes', 0)} 只 · "
            f"财务 {d.get('finance_codes', 0):,} 只   |   "
            f"今天收到 {today_n:,} 只 {arrow}{abs(delta):,}{note}   |   "
            f"覆盖 {str(d.get('first_day') or '')[:7]} 至今")
        self.data_lbl.setText(
            "最近 7 天每天收到的票数:\n"
            + "  ".join(f"{str(r['day'])[5:]}={r['codes']}"
                        for r in recent[-7:])
            + f"\n因子库 {f.get('total', 0)} 条"
            f"(能用 {f.get('accepted', 0)} / 排除 {f.get('rejected', 0)})"
            f" · 最近检查 {str(f.get('last_audit') or '—')[:16]}")

        lines_data = ln.get("lines") or []
        self.ring.set_data(int(ln.get("accepted") or 0),
                           int(ln.get("pending") or 0),
                           int(ln.get("rejected") or 0))
        self.bars.set_data(lines_data)
        self._fill_lines(lines_data)

        self.s_try.set(str(g.get("attempts", 0)))
        self.s_win.set(str(g.get("wins", 0)))
        self.s_rate.set(f"{(g.get('rate') or 0) * 100:.1f}%")

        m = snap.get("maintain") or {}
        if m:
            self.maint_lbl.setText(
                f"最近维护 {m.get('mtime', '—')}\n" + "\n".join(m.get("steps") or []))

        if tr.get("ok"):
            self._show_train(tr)
        else:
            self.train_hint.setText("还没训练过。点右上角「重新训练」跑一次。")

    def _fill_lines(self, lines: list[dict]) -> None:
        self.lines_tbl.setRowCount(0)
        self.lines_tbl.setRowCount(len(lines))
        for i, ln in enumerate(lines):
            st = ln.get("status")
            txt, col = {"accepted": ("通过", GREEN),
                        "pending": ("待跟", AMBER)}.get(st, ("证否", RED))
            set_cell(self.lines_tbl, i, 0, str(ln.get("name") or ""), align="left")
            set_cell(self.lines_tbl, i, 1, txt, col)
            det = str(ln.get("detail") or "")
            set_cell(self.lines_tbl, i, 2, det, align="left")
            item = self.lines_tbl.item(i, 2)
            if item is not None:
                item.setToolTip(det)

    def _show_train(self, tr: dict) -> None:
        fin = tr.get("final") or {}
        self.curve.set_data(tr.get("curve") or [], float(tr.get("baseline") or 0))
        acc = float(fin.get("test_acc") or 0)
        base = float(tr.get("baseline") or 0)
        auc = float(tr.get("auc") or 0)
        rec = float(tr.get("recall") or 0)

        # 用大白话:区分能力 / 抓到上涨 / 整体猜对
        self.s_auc.set(f"{auc:.2f}", UP if auc > 0.55 else DOWN)
        self.s_rec.set(f"{rec * 100:.1f}%", UP if rec > 0.3 else DOWN)
        self.s_acc.set(f"{acc * 100:.1f}%",
                       UP if acc > base + 0.01 else DOWN)

        cm = tr.get("confusion") or {}
        hit = int(cm.get("tp", 0)) + int(cm.get("tn", 0))
        miss = int(cm.get("fp", 0)) + int(cm.get("fn", 0))
        self.s_cm.setText(
            f"猜对 {hit:,} 次 / 猜错 {miss:,} 次 —— "
            f"其中「说会涨、结果涨了」{cm.get('tp', 0):,} 次,"
            f"「说会涨、其实没涨」{cm.get('fp', 0):,} 次")
        verdict = ("这台机器学到了东西" if auc > 0.55
                   else ("和闭眼瞎猜差不多" if auc > 0.45
                         else "比瞎猜还差 —— 学到的全是噪声"))
        self.train_hint.setText(
            f"用来判断的样本 {tr.get('n', 0):,} 条"
            f"(学 {tr.get('n_train', 0):,} 条 / 考 {tr.get('n_test', 0):,} 条)· "
            f"全部猜「不会涨」的命中率是 {base:.0%} · {verdict} · "
            f"{tr.get('at', '')}")

    # ------------------------------------------------------------------ 动作
    def _train(self) -> None:
        self.btn_train.setEnabled(False)
        self.train_hint.setText("训练中…(约 5 秒)")
        self.status.emit("开始训练")

        def job():
            return og.train_demo(epochs=60)

        def done(r):
            self.btn_train.setEnabled(True)
            if not r.get("ok"):
                self.train_hint.setText(f"训练失败:{r.get('msg')}")
                return
            self._show_train(r)
            self.status.emit(
                f"训练完成:测试准确率 {(r.get('final') or {}).get('test_acc', 0):.3f}")
            self.refresh()

        self._run_async(job, done)

    def _run_async(self, fn, on_ok) -> None:
        import threading

        def worker():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            QTimer.singleShot(0, lambda: on_ok(res))

        threading.Thread(target=worker, daemon=True).start()
