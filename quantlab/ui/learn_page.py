# -*- coding: utf-8 -*-
"""学习页:私有自适应学习的一屏。

摆四样东西:
    1) 今日日报(决策、回填、当日盈亏、分批进度)
    2) 参数建议(当前 → 建议,以及为什么)
    3) 预测准确率与因子误导率
    4) 分批状态与最近预测

全部只读本地库,不发网络请求。参数建议用 preview(),打开页面不会改参数。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import selflearn as sl
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, set_cell, table
from .theme import DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP


class LearnPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._build()
        self.refresh()

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("learn", cols=[1.0], rows=[0.13, 0.47, 0.40])
        root.addWidget(board)

        top = Board("learn.top", cols=[0.13, 0.13, 0.13, 0.13, 0.13, 0.13, 0.12, 0.10],
                    rows=[1.0])
        self.st_dec = StatCard("今日决策", "--", "买 + 卖")
        self.st_fill = StatCard("已回填", "--", "有结果的决策")
        self.st_win = StatCard("胜率", "--", "最近 60 条")
        self.st_ver = StatCard("参数版本", "--", "改过几次")
        self.st_acc = StatCard("预测准确率", "--", "已回填的预测")
        self.st_pred = StatCard("今日预测", "--", "收盘后跑")
        self.st_health = StatCard("回填率", "--", "已回填/总决策")
        for i, s in enumerate((self.st_dec, self.st_fill, self.st_win,
                               self.st_ver, self.st_acc, self.st_pred,
                               self.st_health)):
            top.add(s, 0, i, min_w=110, min_h=80)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        self.btn = QPushButton("刷新")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        self.btn_save = QPushButton("生成日报")
        self.btn_save.setToolTip("把今天的报告写到 reports/ 目录")
        self.btn_save.clicked.connect(self.save_report)
        bl.addWidget(self.btn)
        bl.addWidget(self.btn_save)
        top.add(box, 0, 7, min_w=110, min_h=80)
        board.add(top, 0, 0, min_h=84)

        mid = Board("learn.mid", cols=[0.56, 0.44], rows=[1.0])
        rep = Card("今日日报")
        # 日报是 markdown,用 QTextBrowser 渲染;QPlainTextEdit 会把 # 和 | 原样吐出来
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(False)
        self.report.setStyleSheet("QTextBrowser{background:transparent;border:none;}")
        rep.add(self.report, 1)
        mid.add(rep, 0, 0, min_w=420, min_h=220)

        right = Board("learn.right", cols=[1.0], rows=[0.45, 0.30, 0.25])
        tune = Card("参数建议", hint="只算不改,采纳与否自己定")
        self.tune_tbl = table(["参数", "当前", "建议", "原因"],
                              widths=[96, 72, 72, 240], stretch_col=3)
        tune.add(self.tune_tbl, 1)
        self.tune_msg = label("", dim=True, size=11)
        self.tune_msg.setWordWrap(True)
        tune.add(self.tune_msg)
        right.add(tune, 0, 0, min_w=320, min_h=120)

        fac = Card("因子误导率", hint="准确率越低越在骗人")
        self.fac_tbl = table(["因子", "准确率", "样本"],
                             widths=[150, 72, 56], stretch_col=0)
        fac.add(self.fac_tbl, 1)
        right.add(fac, 1, 0, min_w=320, min_h=100)

        hot = Card("游资手法", hint="高胜率席位 · 样本≥3")
        self.hot_tbl = table(["席位", "样本", "5日均收益", "胜率"],
                             widths=[220, 50, 80, 56], stretch_col=0)
        hot.add(self.hot_tbl, 1)
        self.hot_msg = label("", dim=True, size=11)
        hot.add(self.hot_msg)
        right.add(hot, 2, 0, min_w=320, min_h=100)
        mid.add(right, 0, 1, min_w=320, min_h=220)
        board.add(mid, 1, 0, min_h=220)

        bot = Board("learn.bot", cols=[0.42, 0.58], rows=[1.0])
        tr = Card("分批进行中")
        self.tr_tbl = table(["代码", "方向", "已买/目标", "批次"],
                            widths=[80, 56, 110, 64], stretch_col=2)
        tr.add(self.tr_tbl, 1)
        bot.add(tr, 0, 0, min_w=300, min_h=140)

        pr = Card("最近预测")
        self.pr_tbl = table(["日期", "代码", "方向", "置信", "结果"],
                            widths=[104, 76, 56, 60, 90], stretch_col=4)
        pr.add(self.pr_tbl, 1)
        bot.add(pr, 0, 1, min_w=380, min_h=140)
        board.add(bot, 2, 0, min_h=140)

    # ------------------------------------------------------------ 数据
    def _ctx(self) -> tuple[str, str]:
        c = cfg()
        return (str(c.get("autotrade.account", "default") or "default"),
                str(c.get("autotrade.strategy", "") or ""))

    def _set_md(self, text: str) -> None:
        """markdown 渲染;Qt 版本不支持时退回纯文本,不让页面空白。"""
        try:
            self.report.document().setMarkdown(text)
        except Exception:  # noqa: BLE001
            self.report.setPlainText(text)

    def refresh(self) -> None:
        self.btn.setEnabled(False)
        acc, strat = self._ctx()

        def job():
            rep = sl.daily_report(acc, strat)
            base = {
                "per_position_pct": float(cfg().get("autotrade.per_position_pct", 0.18) or 0.18),
                "stop_loss": float(cfg().get("autotrade.stop_loss_pct", 2.0) or 2.0) / 100.0,
                "take_profit": float(cfg().get("autotrade.take_profit_pct", 5.0) or 5.0) / 100.0,
            }
            tune = sl.ParamTuner(acc, strat).preview(base)
            from ..engine import predict_learn as pl
            plog = pl.PredictLog(acc)
            try:
                from ..engine import hotmoney as hm
                hot = hm.seat_stats(min_samples=3, days=60)
            except Exception:  # noqa: BLE001
                hot = []
            return {"rep": rep, "tune": tune, "base": base,
                    "predict": plog.stats(window=200),
                    "recent": plog.recent(limit=20),
                    "summary": sl.summary(acc, strat),
                    "hot": hot}

        def ok(d):
            self.btn.setEnabled(True)
            self._render(d)

        def err(e):
            self.btn.setEnabled(True)
            self._set_md(f"读取失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _render(self, d: dict) -> None:
        rep = d["rep"]
        t = rep.get("today") or {}
        s = rep.get("stats") or {}
        tot = rep.get("total") or {}
        pr = rep.get("predict") or {}
        ps = pr.get("stats") or {}

        self.st_dec.set(str(t.get("decisions", 0)), None,
                        hint=f"买 {t.get('buys', 0)} / 卖 {t.get('sells', 0)}")
        self.st_fill.set(str(t.get("filled", 0)), None,
                         hint=f"累计 {tot.get('filled', 0)} 条")
        win = float(s.get("win") or 0)
        self.st_win.set(f"{win:.0%}" if s.get("n") else "--",
                        UP if win >= 0.5 else DOWN, hint=f"{s.get('n', 0)} 条样本")
        self.st_ver.set(str(tot.get("versions", 0)), None,
                        hint=(d["summary"].get("last_change") or "还没调过")[:28])
        acc = float(ps.get("acc") or 0)
        self.st_acc.set(f"{acc:.1%}" if ps.get("n") else "--",
                        UP if acc >= 0.5 else DOWN, hint=f"{ps.get('n', 0)} 条")
        self.st_pred.set(str(pr.get("today", 0)), None, hint="收盘后跑")

        total_dec = int(tot.get("decisions") or 0)
        filled = int(tot.get("filled") or 0)
        rate = filled / total_dec if total_dec else 0
        self.st_health.set(f"{rate:.0%}" if total_dec else "--",
                           UP if rate >= 0.5 else DOWN,
                           hint=f"{filled}/{total_dec} 条")

        hot = d.get("hot") or []
        top_hot = [h for h in hot if h.get("n", 0) >= 3][:8]
        self.hot_tbl.setRowCount(len(top_hot))
        for i, h in enumerate(top_hot):
            set_cell(self.hot_tbl, i, 0, h["seat"], TEXT_DIM, align="left")
            set_cell(self.hot_tbl, i, 1, str(h["n"]))
            set_cell(self.hot_tbl, i, 2, f"{h['avg5']:.2%}",
                     UP if h["avg5"] > 0 else DOWN)
            set_cell(self.hot_tbl, i, 3, f"{h['win5']:.0%}")
        self.hot_msg.setText(
            f"{len(top_hot)} 个席位 · 胜率已平滑,去龙虎榜页看今日名单" if top_hot
            else "暂无游资数据,去龙虎榜页点「同步」")

        self._set_md(sl.render_markdown(rep))

        tune = d["tune"]
        base = d["base"]
        sug = tune.get("params") or {}
        rows = [
            ("单票比例", base["per_position_pct"], sug.get("per_position_pct"),
             "{:.1%}"),
            ("止损", base["stop_loss"], sug.get("stop_loss"), "{:.1%}"),
            ("止盈", base["take_profit"], sug.get("take_profit"), "{:.1%}"),
        ]
        self.tune_tbl.setRowCount(len(rows))
        for i, (name, cur, new, fmt) in enumerate(rows):
            changed = new is not None and abs(float(new) - float(cur)) > 1e-9
            set_cell(self.tune_tbl, i, 0, name, TEXT, bold=True, align="left")
            set_cell(self.tune_tbl, i, 1, fmt.format(float(cur)))
            set_cell(self.tune_tbl, i, 2, fmt.format(float(new)) if new is not None else "--",
                     UP if changed else TEXT_MUT)
            set_cell(self.tune_tbl, i, 3,
                     "建议调整" if changed else "不动", TEXT_DIM, align="left")
        self.tune_msg.setText(str(tune.get("reason") or ""))

        fs = pr.get("factors") or []
        self.fac_tbl.setRowCount(len(fs))
        for i, f in enumerate(fs):
            a = float(f.get("acc") or 0)
            set_cell(self.fac_tbl, i, 0, str(f.get("name") or f.get("key")), align="left")
            set_cell(self.fac_tbl, i, 1, f"{a:.0%}", UP if a >= 0.5 else DOWN)
            set_cell(self.fac_tbl, i, 2, str(f.get("n")))

        tr = rep.get("tranches") or []
        self.tr_tbl.setRowCount(len(tr))
        for i, x in enumerate(tr):
            set_cell(self.tr_tbl, i, 0, str(x.get("code")), align="left")
            set_cell(self.tr_tbl, i, 1, "买" if x.get("side") == "buy" else "卖")
            set_cell(self.tr_tbl, i, 2,
                     f"{x.get('done_qty')}/{x.get('target_qty')}")
            set_cell(self.tr_tbl, i, 3,
                     f"{x.get('done_tranches')}/{x.get('tranches')}")

        rec = d.get("recent") or []
        self.pr_tbl.setRowCount(len(rec))
        for i, p in enumerate(rec):
            set_cell(self.pr_tbl, i, 0, str(p.get("day") or ""), align="left")
            set_cell(self.pr_tbl, i, 1, str(p.get("code") or ""), align="left")
            set_cell(self.pr_tbl, i, 2, str(p.get("verdict") or ""))
            conf = p.get("confidence")
            if isinstance(conf, (int, float)):
                conf_txt = f"{float(conf):.0%}"
            else:
                conf_txt = str(conf or "—")
            set_cell(self.pr_tbl, i, 3, conf_txt)
            c = p.get("correct")
            set_cell(self.pr_tbl, i, 4,
                     "—" if c is None else ("对" if int(c) == 1 else "错"),
                     TEXT_MUT if c is None else (UP if int(c) == 1 else DOWN))
        self.status.emit(f"学习:{t.get('decisions', 0)} 条决策,"
                         f"{tot.get('versions', 0)} 个参数版本")

    def save_report(self) -> None:
        acc, strat = self._ctx()
        try:
            rep = sl.daily_report(acc, strat)
            md, js = sl.save_report(rep)
            self.status.emit(f"日报已写:{md}")
            self._set_md(sl.render_markdown(rep))
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"写日报失败:{e}")

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
