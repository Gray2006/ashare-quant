# -*- coding: utf-8 -*-
"""数据健康页:端点状态、熔断倒计时、各领域数据新鲜度、一键探活。

不猜、不粉饰:冷却就写冷却还剩多少秒,没取过数就写「未采集」。
每条异常都带一句「怎么办」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ..data import health
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, set_cell, table
from .theme import DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

STATE_COLOR = {"正常": UP, "空闲": TEXT_DIM, "未使用": TEXT_MUT,
               "重试中": WARN, "冷却中": DOWN,
               "数据延迟": WARN, "数据已陈旧": DOWN, "取数失败": DOWN, "未采集": TEXT_MUT}


class HealthPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._snap: dict | None = None
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(5000)
        QTimer.singleShot(400, self.refresh)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("health", cols=[1.0], rows=[0.13, 0.46, 0.41])
        root.addWidget(board)

        top = Board("health.top", cols=[0.20, 0.18, 0.18, 0.18, 0.26], rows=[1.0])
        self.st_links = StatCard("链路", "--", "本次启动见过的端点")
        self.st_ok = StatCard("正常", "--", "近期 30 秒内还在取数")
        self.st_cool = StatCard("熔断中", "--", "连续失败 3 次自动停 90 秒")
        self.st_cache = StatCard("内存缓存", "--", "短 TTL 命中池")
        for i, s in enumerate((self.st_links, self.st_ok, self.st_cool, self.st_cache)):
            top.add(s, 0, i, min_w=130, min_h=80)

        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self.verdict = QLabel("读取中…")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet(f"color:{TEXT};font-size:12px;")
        bl.addWidget(self.verdict)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_probe = QPushButton("探活")
        self.btn_probe.setToolTip("对已知端点各发一次裸 GET,只判断能不能拿到响应")
        self.btn_probe.clicked.connect(self.probe)
        row.addWidget(self.btn_probe)
        self.btn_dump = QPushButton("复制报告")
        self.btn_dump.clicked.connect(self._copy)
        row.addWidget(self.btn_dump)
        bl.addLayout(row)
        top.add(box, 0, 4, min_w=200, min_h=80)
        board.add(top, 0, 0, min_h=84)

        mid = Board("health.mid", cols=[0.54, 0.46], rows=[1.0])
        lk = Card("端点", hint="熔断按 host+路径 分桶")
        self.link_tbl = table(["状态", "端点", "失败", "空闲"],
                              widths=[64, 340, 52, 62], stretch_col=1)
        self.link_tbl.setObjectName("HealthLink")
        self.link_tbl.cellClicked.connect(self._on_link)
        lk.add(self.link_tbl, 1)
        self.link_hint = label("点一行看建议", dim=True, size=11)
        self.link_hint.setWordWrap(True)
        lk.add(self.link_hint)
        mid.add(lk, 0, 0, min_w=460, min_h=190)

        dm = Card("领域新鲜度", hint="各页面最近一次取数成功")
        self.dom_tbl = table(["领域", "状态", "距上次成功", "说明"],
                             widths=[72, 76, 88, 200], stretch_col=3)
        dm.add(self.dom_tbl, 1)
        mid.add(dm, 0, 1, min_w=340, min_h=190)
        board.add(mid, 1, 0, min_h=190)

        bot = Board("health.bot", cols=[0.44, 0.56], rows=[1.0])
        pr = Card("探活结果")
        self.probe_tbl = table(["端点", "结果", "耗时"],
                               widths=[240, 76, 60], stretch_col=0)
        pr.add(self.probe_tbl, 1)
        bot.add(pr, 0, 0, min_w=340, min_h=140)

        rp = Card("完整报告")
        self.raw = QPlainTextEdit()
        self.raw.setReadOnly(True)
        rp.add(self.raw, 1)
        bot.add(rp, 0, 1, min_w=380, min_h=140)
        board.add(bot, 2, 0, min_h=140)

    # ------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        snap = health.summary()
        self._snap = snap
        tone_col = {"ok": UP, "warn": WARN}.get(snap["tone"], TEXT_DIM)
        self.st_links.set(f"{snap['links']}")
        self.st_ok.set(f"{snap['ok']}", UP if snap["ok"] else FLAT,
                       hint="近期还在取数" if snap["ok"] else "都在空闲")
        self.st_cool.set(f"{snap['cooling']}", DOWN if snap["cooling"] else FLAT,
                         hint=f"重试中 {snap['failing']} 条" if snap["failing"] else "")
        self.st_cache.set(f"{snap['cache']}")
        self.verdict.setText(snap["verdict"])
        self.verdict.setStyleSheet(f"color:{tone_col};font-size:12px;")

        links = health.links()
        self.link_tbl.setRowCount(len(links))
        for i, r in enumerate(links):
            col = STATE_COLOR.get(r["state"], TEXT)
            set_cell(self.link_tbl, i, 0, r["state"], col, bold=True, align="left")
            set_cell(self.link_tbl, i, 1, r["bucket"][:64], TEXT_DIM, align="left")
            set_cell(self.link_tbl, i, 2, str(r["fails"]) if r["fails"] else "",
                     DOWN if r["fails"] else TEXT_MUT)
            idle = "—" if r["idle"] is None else (
                f"{r['left']:.0f}s" if r["cooling"] else f"{r['idle']:.0f}s")
            set_cell(self.link_tbl, i, 3, idle, DOWN if r["cooling"] else TEXT_MUT)
        self._links = links

        doms = snap["domains"]
        self.dom_tbl.setRowCount(len(doms))
        for i, d in enumerate(doms):
            col = STATE_COLOR.get(d["state"], TEXT)
            set_cell(self.dom_tbl, i, 0, d["name"], TEXT, bold=True, align="left")
            set_cell(self.dom_tbl, i, 1, d["state"], col, align="left")
            age = "—" if d["age"] is None else (
                f"{d['age']:.0f}s" if d["age"] < 120 else f"{d['age'] / 60:.0f}分钟")
            set_cell(self.dom_tbl, i, 2, age, None)
            set_cell(self.dom_tbl, i, 3, d["detail"] or d["advice"] or "", TEXT_DIM, align="left")
        if not doms:
            self.dom_tbl.setRowCount(1)
            set_cell(self.dom_tbl, 0, 0, "—", TEXT_MUT, align="left")
            set_cell(self.dom_tbl, 0, 1, "未采集", TEXT_MUT, align="left")
            set_cell(self.dom_tbl, 0, 3, "行情页与 K 线取数后这里会有记录", TEXT_MUT,
                     align="left")
        self.raw.setPlainText(health.to_text(snap))

    # ------------------------------------------------------------ 动作
    def probe(self) -> None:
        self.btn_probe.setEnabled(False)
        self.link_hint.setText("探活中:对已知端点各发一次裸 GET…")

        def job():
            return health.probe(limit=8)

        def ok(res):
            self.btn_probe.setEnabled(True)
            self.probe_tbl.setRowCount(len(res))
            for i, r in enumerate(res):
                set_cell(self.probe_tbl, i, 0, r["bucket"][:52], TEXT_DIM, align="left")
                set_cell(self.probe_tbl, i, 1, "通" if r["ok"] else "不通",
                         UP if r["ok"] else DOWN, bold=True)
                set_cell(self.probe_tbl, i, 2, f"{r['ms']:.0f}ms")
            bad = [r for r in res if not r["ok"]]
            self.link_hint.setText(
                f"探活 {len(res)} 条,{len(res) - len(bad)} 条有响应"
                + (";失败:" + ", ".join(r["bucket"].split("/")[0] for r in bad[:3]) if bad else ""))
            self.status.emit(f"探活完成:{len(res) - len(bad)}/{len(res)} 通")

        def err(e):
            self.btn_probe.setEnabled(True)
            self.link_hint.setText(f"探活失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_probe.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _on_link(self, row: int, _col: int) -> None:
        links = getattr(self, "_links", [])
        if row < len(links):
            r = links[row]
            self.link_hint.setText(f"{r['bucket']}  {r['advice'] or '没有异常'}")

    def _copy(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(self.raw.toPlainText())
            self.status.emit("健康报告已复制")
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"复制失败:{e}")

    def refresh_now(self) -> None:
        self.refresh()

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
