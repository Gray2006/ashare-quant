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
from .theme import BG, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP

CYAN = "#5ee7f0"
VIOLET = "#a78bfa"
AMBER = "#fbbf24"
GREEN = "#34d399"
RED = "#f87171"


class _Radar(QWidget):
    """七条线的能力雷达。发光的极坐标多边形。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 340)
        self._vals: list[tuple[str, float, str]] = []
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)

    def set_data(self, lines: list[dict]) -> None:
        vals = []
        for ln in lines:
            st = ln.get("status")
            v = {"accepted": 1.0, "pending": 0.55, "rejected": 0.22}.get(st, 0.3)
            vals.append((str(ln.get("name") or ""), v, str(st)))
        self._vals = vals
        self.update()

    def _tick(self) -> None:
        if self._vals:
            self._phase = (self._phase + 0.035) % (2 * math.pi)
            self.update()

    def paintEvent(self, _e) -> None:      # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) * 0.25
        n = len(self._vals)
        if n < 3:
            p.setPen(QPen(QColor(TEXT_MUT)))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "等待数据")
            return

        # 网格:三层同心多边形
        for level in (0.35, 0.7, 1.0):
            path = QPainterPath()
            for i in range(n):
                a = -math.pi / 2 + 2 * math.pi * i / n
                pt = QPointF(cx + r * level * math.cos(a),
                             cy + r * level * math.sin(a))
                if i == 0:
                    path.moveTo(pt)
                else:
                    path.lineTo(pt)
            path.closeSubpath()
            p.setPen(QPen(QColor(255, 255, 255, 18 if level < 1 else 34), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # 轴线
        for i in range(n):
            a = -math.pi / 2 + 2 * math.pi * i / n
            p.setPen(QPen(QColor(255, 255, 255, 14), 1))
            p.drawLine(QPointF(cx, cy),
                       QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))

        # 数据多边形(带呼吸)
        pulse = 0.94 + 0.06 * math.sin(self._phase)
        path = QPainterPath()
        for i, (_name, v, _st) in enumerate(self._vals):
            a = -math.pi / 2 + 2 * math.pi * i / n
            rr = r * max(0.08, v) * pulse
            pt = QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a))
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()

        grad = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
        grad.setColorAt(0.0, QColor(94, 231, 240, 90))
        grad.setColorAt(1.0, QColor(167, 139, 250, 70))
        p.setBrush(grad)
        p.setPen(QPen(QColor(CYAN), 2))
        p.drawPath(path)

        # 顶点亮点
        for i, (_name, v, st) in enumerate(self._vals):
            a = -math.pi / 2 + 2 * math.pi * i / n
            rr = r * max(0.08, v) * pulse
            pt = QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a))
            col = {"accepted": GREEN, "pending": AMBER}.get(st, RED)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(col))
            p.drawEllipse(pt, 4, 4)
            p.setBrush(QColor(col + "44"))
            p.drawEllipse(pt, 9, 9)

        # 标签:按状态上色,外移一点,加半透明底片防止压在网格上看不清
        f = p.font()
        f.setPointSize(9)
        f.setBold(False)
        p.setFont(f)
        fm = p.fontMetrics()
        for i, (name, v, st) in enumerate(self._vals):
            a = -math.pi / 2 + 2 * math.pi * i / n
            # 夹在控件内:底部那个标签总被裁,干脆按边界收
            lx = max(52.0, min(w - 52.0, cx + (r + 22) * math.cos(a)))
            ly = max(12.0, min(h - 12.0, cy + (r + 22) * math.sin(a)))
            tw = fm.horizontalAdvance(name) + 10
            rect = QRectF(lx - tw / 2, ly - 10, tw, 20)
            col = {"accepted": GREEN, "pending": AMBER}.get(st, TEXT_DIM)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(10, 14, 19, 190))
            p.drawRoundedRect(rect, 6, 6)
            p.setPen(QPen(QColor(col)))
            p.drawText(rect, Qt.AlignCenter, name)
            # 顶点数值
            f2 = p.font()
            f2.setPointSize(7)
            p.setFont(f2)
            p.setPen(QPen(QColor(255, 255, 255, 90)))
            p.drawText(QRectF(lx - 30, ly + 9, 60, 12), Qt.AlignCenter,
                       f"{v:.0%}")
            p.setFont(f)


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
        p.setBrush(QColor(GREEN + "55"))
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
        left = Card("能力雷达", hint="七条线 · 绿=通过 黄=待跟 红=证否")
        self.radar = _Radar()
        left.add(self.radar, 1)
        self.data_lbl = label("", dim=True)
        self.data_lbl.setWordWrap(True)
        left.body().addWidget(self.data_lbl)
        board.add(left, 0, 0)

        # 中
        mid = Card("训练曲线", hint="用低波动+动量预测三重障碍标签")
        self.curve = _TrainCurve()
        mid.add(self.curve, 1)
        grid = QHBoxLayout()
        self.s_loss = StatCard("最终 loss", "-")
        self.s_acc = StatCard("测试准确率", "-")
        self.s_lift = StatCard("相对基线", "-")
        for c in (self.s_loss, self.s_acc, self.s_lift):
            grid.addWidget(c)
        mid.body().addLayout(grid)
        self.train_hint = label("", dim=True)
        self.train_hint.setWordWrap(True)
        mid.body().addWidget(self.train_hint)
        board.add(mid, 0, 1)

        # 右
        right = Card("成长与进化", hint="累计尝试 vs 真正站住的")
        grow = QHBoxLayout()
        self.s_try = StatCard("累计尝试", "-")
        self.s_win = StatCard("真正通过", "-")
        self.s_rate = StatCard("成功率", "-")
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
        self.data_lbl.setText(
            f"因子库 {f.get('total', 0)} 条(通过 {f.get('accepted', 0)} / "
            f"拒绝 {f.get('rejected', 0)})\n"
            f"最近审计 {f.get('last_audit') or '—'}\n"
            f"七条线 {ln.get('accepted', 0)} 通过 / {ln.get('rejected', 0)} 证否 / "
            f"{ln.get('pending', 0)} 待跟")

        self.radar.set_data(ln.get("lines") or [])
        self._fill_lines(ln.get("lines") or [])

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
        self.s_loss.set(f"{fin.get('loss', 0):.4f}")
        self.s_acc.set(f"{(fin.get('test_acc') or 0) * 100:.1f}%")
        lift = float(tr.get("lift") or 0)
        self.s_lift.set(f"{lift * 100:+.1f}%", UP if lift > 0 else DOWN)
        self.train_hint.setText(
            f"{tr.get('n', 0):,} 个样本({tr.get('n_train', 0):,} 训练 / "
            f"{tr.get('n_test', 0):,} 测试)· 权重 {tr.get('weights')} · "
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
