# -*- coding: utf-8 -*-
"""预警页:价格/涨跌幅/量比条件,后台轮询命中后留痕。

规则存 store 的 account 字典里(键 "alerts"),重启后还在。
命中日志只保留最近 N 条,存在同一个字典里(键 "alert_log"),
避免为这点数据再建一张表。

注意:轮询用的是行情快照(可能延迟数秒),不是精确的触价,别拿它当券商条件单。

「价格上穿/下穿」用前后两次快照比较(prev < 阈值 <= 现价 才算上穿):
第一次拿到某只票只记基准、不判命中,否则加规则瞬间只要有价就会立刻误报。
涨幅/跌幅/量比是状态量,满足即命中;每条规则命中一次后置「已触发」不再重复刷屏。
"""
from __future__ import annotations

import datetime as dt

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ..data.realtime import quotes
from ..data.store import store
from ..engine.hub import TaskRunner
from ..utils import safe_float
from .board import Board
from .common import Card, label, set_cell, table
from .theme import TEXT_DIM, TEXT_MUT, UP

KINDS = [("价格上穿", "above"), ("价格下穿", "below"),
         ("涨幅超过(%)", "gain"), ("跌幅超过(%)", "drop"),
         ("量比超过", "volratio")]
LOG_MAX = 50
POLL_SEC = 15


class AlertPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rules: list[dict] = []
        self._log: list[dict] = []
        self._prev: dict[str, float] = {}     # 上一轮快照价,上穿/下穿要用
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_SEC * 1000)
        self._timer.timeout.connect(self.poll)
        self._build()
        self.load_rules()
        QTimer.singleShot(1200, self.poll)
        self._timer.start()

    # ------------------------------------------------------------------ 界面
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        board = Board("alert", cols=[1.0], rows=[0.56, 0.44])
        root.addWidget(board)      # 不进布局 = 整页空白,且不报错

        top = Card("预警规则", hint=f"每 {POLL_SEC} 秒用行情快照查一次")
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.code = QLineEdit()
        self.code.setPlaceholderText("6 位代码")
        self.code.setMaximumWidth(96)
        bar.addWidget(self.code)
        self.kind = QComboBox()
        for name, key in KINDS:
            self.kind.addItem(name, key)
        bar.addWidget(self.kind)
        self.thr = QDoubleSpinBox()
        self.thr.setRange(0.0, 100000.0)
        self.thr.setDecimals(2)
        self.thr.setValue(10.0)
        self.thr.setMaximumWidth(96)
        bar.addWidget(self.thr)
        btn_add = QPushButton("添加")
        btn_add.setObjectName("Primary")
        btn_add.clicked.connect(self.add_rule)
        bar.addWidget(btn_add)
        btn_del = QPushButton("删除选中")
        btn_del.clicked.connect(self.del_rule)
        bar.addWidget(btn_del)
        btn_now = QPushButton("立即检查")
        btn_now.clicked.connect(self.poll)
        bar.addWidget(btn_now)
        bar.addStretch(1)
        self.hint = label("", dim=True, size=11)
        bar.addWidget(self.hint)
        top.body().addLayout(bar)

        self.tbl = table(["代码", "名称", "条件", "阈值", "状态", "命中时间"],
                         widths=[72, 90, 104, 76, 80, 150], stretch_col=5)
        self.tbl.setObjectName("AlertRules")
        top.add(self.tbl, 1)
        board.add(top, 0, 0, min_h=240)

        bot = Card("命中日志", hint=f"最近 {LOG_MAX} 条")
        self.log_tbl = table(["时间", "代码", "条件", "当时价格", "说明"],
                             widths=[140, 72, 104, 84, 420], stretch_col=4)
        self.log_tbl.setObjectName("AlertLog")
        bot.add(self.log_tbl, 1)
        board.add(bot, 1, 0, min_h=180)

    # ------------------------------------------------------------------ 存取
    def load_rules(self):
        try:
            st = store()
            self._rules = list(st.get_account("alerts") or [])
            self._log = list(st.get_account("alert_log") or [])
        except Exception:  # noqa: BLE001
            self._rules, self._log = [], []
        self._render()

    def _save(self):
        try:
            st = store()
            st.set_account("alerts", self._rules)
            st.set_account("alert_log", self._log[-LOG_MAX:])
        except Exception as e:  # noqa: BLE001
            self.hint.setText(f"存盘失败:{str(e)[:60]}")

    def _render(self):
        self.tbl.setRowCount(len(self._rules))
        for r, it in enumerate(self._rules):
            set_cell(self.tbl, r, 0, it.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.tbl, r, 1, it.get("name") or "-", align="left")
            set_cell(self.tbl, r, 2, it.get("kind_name", ""), align="left")
            thr = safe_float(it.get("thr"))
            set_cell(self.tbl, r, 3, "-" if thr is None else f"{thr:g}")
            hit = it.get("hit_at") or ""
            set_cell(self.tbl, r, 4, "已触发" if hit else "监控中",
                     UP if hit else TEXT_MUT)
            set_cell(self.tbl, r, 5, hit or "-", TEXT_DIM, align="left")
        self.log_tbl.setRowCount(len(self._log))
        for r, it in enumerate(self._log):
            set_cell(self.log_tbl, r, 0, it.get("ts", ""), TEXT_DIM, align="left")
            set_cell(self.log_tbl, r, 1, it.get("code", ""), TEXT_DIM, align="left")
            set_cell(self.log_tbl, r, 2, it.get("kind_name", ""), align="left")
            p = safe_float(it.get("price"))
            set_cell(self.log_tbl, r, 3, "-" if p is None else f"{p:.2f}")
            set_cell(self.log_tbl, r, 4, it.get("note", ""), TEXT_DIM, align="left")
        self.hint.setText(f"{len(self._rules)} 条规则 · 日志 {len(self._log)} 条")

    # ------------------------------------------------------------------ 增删
    def add_rule(self):
        code = "".join(ch for ch in (self.code.text() or "").strip()
                       if ch.isdigit())[:6]
        if len(code) != 6:
            self.hint.setText("代码要 6 位数字")
            return
        kind_name = self.kind.currentText()
        key = self.kind.currentData()
        rule = {"code": code, "name": "", "kind": key, "kind_name": kind_name,
                "thr": float(self.thr.value()), "hit_at": "", "created": self._now()}
        self._rules.append(rule)
        self._save()
        self._render()
        self.status.emit(f"已加预警 {code} {kind_name}")
        self.poll()

    def del_rule(self):
        from .common import row_key
        names = {row_key(self.tbl, i.row(), 1) for i in self.tbl.selectedIndexes()}
        if not names:
            self.hint.setText("先在规则表里选一行")
            return
        self._rules = [r for r in self._rules
                       if (r.get("name") or "-") not in names]
        self._save()
        self._render()

    # ------------------------------------------------------------------ 轮询
    @staticmethod
    def _now() -> str:
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def poll(self):
        if not self._rules:
            return
        codes = sorted({r.get("code") for r in self._rules if r.get("code")})
        if not codes:
            return

        def job():
            return quotes(codes)

        r = TaskRunner(job)
        r.succeeded.connect(self._checked)
        r.failed.connect(lambda m: self.hint.setText(f"取价失败:{str(m)[:60]}"))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _checked(self, payload):
        data, _src = payload if isinstance(payload, tuple) else (payload, "")
        data = data or {}
        changed = False
        for rule in self._rules:
            code = rule.get("code")
            q = data.get(code) or {}
            price = safe_float(q.get("price"))
            if price is None or price <= 0:
                continue
            prev = self._prev.get(code)
            self._prev[code] = price
            name = q.get("name") or ""
            if name and not rule.get("name"):
                rule["name"] = name
                changed = True
            chg = safe_float(q.get("change_pct"))
            vr = safe_float(q.get("volume_ratio"))
            thr = safe_float(rule.get("thr"))
            if thr is None:
                thr = 0.0
            key = rule.get("kind")
            hit = False
            note = ""
            # 上穿/下穿:必须有上一轮快照才判,首轮只当基准,免得加规则瞬间误报
            if key == "above":
                hit = prev is not None and prev < thr <= price
                if hit:
                    note = f"{prev:.2f} → {price:.2f},上穿 {thr:g}"
            elif key == "below":
                hit = prev is not None and prev > thr >= price
                if hit:
                    note = f"{prev:.2f} → {price:.2f},下穿 {thr:g}"
            elif key == "gain":
                hit = chg is not None and chg >= thr
                if hit:
                    note = f"涨幅 {chg:+.2f}% ≥ {thr:g}%"
            elif key == "drop":
                hit = chg is not None and chg <= -abs(thr)
                if hit:
                    note = f"跌幅 {chg:+.2f}% ≤ -{abs(thr):g}%"
            elif key == "volratio":
                hit = vr is not None and thr > 0 and vr >= thr
                if hit:
                    note = f"量比 {vr:.2f} ≥ {thr:g}"
            if hit and not rule.get("hit_at"):
                rule["hit_at"] = self._now()
                self._log.append({"ts": rule["hit_at"], "code": code,
                                  "kind_name": rule.get("kind_name", ""),
                                  "price": price, "note": note})
                self._log = self._log[-LOG_MAX:]
                changed = True
                self.status.emit(f"预警命中 {code} {rule.get('kind_name')} · {note}")
        if changed:
            self._save()
            self._render()

    def stop(self):
        self._timer.stop()
        for t in self._tasks:
            if t.isRunning():
                t.wait(800)
        self._tasks.clear()
