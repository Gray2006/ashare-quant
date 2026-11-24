# -*- coding: utf-8 -*-
"""策略导入窗口:粘贴外部买卖策略 → 自动拆成买点/卖点 → 试跑 → 存为策略。"""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout)

from ..engine import importer
from ..engine.hub import TaskRunner
from ..engine import rules as rules_mod
from ..utils import normalize_code
from .common import label, set_cell, table
from .theme import DOWN, TEXT, UP


class ImportDialog(QDialog):
    """把别人的策略文本拆成可执行的两条表达式。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导入策略 · 自动拆解买卖")
        self.setMinimumSize(920, 700)
        self.setSizeGripEnabled(True)
        self._tasks: list[TaskRunner] = []
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        tip = label("把外部策略原样贴进来(带「买点/卖点」「买入/卖出」「entry/exit」"
                    "都能认;纯自然语言也行)", dim=True)
        root.addWidget(tip)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("示例"))
        self.example = QComboBox()
        self.example.addItem("(自己贴)", "")
        for ex in importer.examples():
            self.example.addItem(ex["label"], ex["label"])
        self.example.currentIndexChanged.connect(self._load_example)
        src_row.addWidget(self.example, 1)
        self.btn_parse = QPushButton("解析")
        self.btn_parse.setObjectName("Primary")
        self.btn_parse.clicked.connect(self.parse)
        src_row.addWidget(self.btn_parse)
        root.addLayout(src_row)

        self.src = QPlainTextEdit()
        self.src.setPlaceholderText(
            "买点:close > ma(20) and volume > vol_ma(20) * 1.5\n"
            "卖点:close < ma(10)\n\n"
            "或者:5日均线上穿20日均线时买入 / 跌破20日均线时卖出")
        self.src.setFixedHeight(150)
        root.addWidget(self.src)

        self.info = label("贴好内容后点「解析」", dim=True)
        root.addWidget(self.info)

        res_row = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("买点(可改)"))
        self.buy_edit = QPlainTextEdit()
        self.buy_edit.setFixedHeight(70)
        left.addWidget(self.buy_edit)
        right = QVBoxLayout()
        right.addWidget(QLabel("卖点(可改)"))
        self.sell_edit = QPlainTextEdit()
        self.sell_edit.setFixedHeight(70)
        right.addWidget(self.sell_edit)
        res_row.addLayout(left, 1)
        res_row.addLayout(right, 1)
        root.addLayout(res_row)

        test_row = QHBoxLayout()
        test_row.addWidget(QLabel("试跑代码"))
        self.test_code = QLineEdit("600519")
        self.test_code.setFixedWidth(90)
        test_row.addWidget(self.test_code)
        test_row.addWidget(QLabel("根数"))
        self.test_bars = QSpinBox()
        self.test_bars.setRange(120, 800)
        self.test_bars.setValue(300)
        test_row.addWidget(self.test_bars)
        self.btn_test = QPushButton("试跑")
        self.btn_test.clicked.connect(self.run_test)
        test_row.addWidget(self.btn_test)
        self.btn_save = QPushButton("存为策略")
        self.btn_save.setObjectName("Primary")
        self.btn_save.clicked.connect(self.save)
        test_row.addWidget(self.btn_save)
        test_row.addStretch(1)
        root.addLayout(test_row)

        self.tbl = table(["项", "值"], widths=[110, 640], stretch_col=1)
        root.addWidget(self.tbl, 1)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFixedHeight(96)
        root.addWidget(self.detail)

        bot = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("策略名(默认:导入策略)")
        bot.addWidget(self.name_edit, 1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bot.addWidget(b_close)
        root.addLayout(bot)

    # ---------------------------------------------------------------- 解析
    def _load_example(self) -> None:
        label_ = self.example.currentData()
        if not label_:
            return
        for ex in importer.examples():
            if ex["label"] == label_:
                self.src.setPlainText(ex["text"])
                self.name_edit.setText(label_)
                break

    def parse(self) -> None:
        text = self.src.toPlainText().strip()
        r = importer.parse_strategy(text, name=self.name_edit.text().strip() or "导入策略")
        self.buy_edit.setPlainText(r["buy"])
        self.sell_edit.setPlainText(r["sell"])
        if r.get("name") and not self.name_edit.text().strip():
            self.name_edit.setText(r["name"])
        self.info.setText("解析完成" if r["ok"] else "解析有问题,看下面")
        rows = [("买点", r["buy"] or "(空)", UP if r["buy"] else DOWN),
                ("卖点", r["sell"] or "(空)", UP if r["sell"] else DOWN)]
        for i, n in enumerate(r["notes"] or []):
            rows.append((f"说明{i+1}", n, TEXT))
        for i, w in enumerate(r["warnings"] or []):
            rows.append((f"提示{i+1}", w, DOWN))
        self._fill(rows)
        self.detail.setPlainText("\n".join(r["notes"] + r["warnings"]))

    def _fill(self, rows: list) -> None:
        self.tbl.setRowCount(len(rows))
        for i, (k, v, col) in enumerate(rows):
            set_cell(self.tbl, i, 0, str(k), col, bold=True, align="left")
            set_cell(self.tbl, i, 1, str(v)[:300], align="left")

    # ---------------------------------------------------------------- 试跑
    def run_test(self) -> None:
        code = normalize_code(self.test_code.text())
        buy = self.buy_edit.toPlainText().strip()
        sell = self.sell_edit.toPlainText().strip()
        for expr, tag in ((buy, "买点"), (sell, "卖点")):
            if expr:
                ok, why = rules_mod.validate(expr)
                if not ok:
                    QMessageBox.warning(self, f"{tag}有误", why)
                    return
        self.btn_test.setEnabled(False)
        self.info.setText("试跑中…")

        def job():
            return importer.test_on(code, buy, sell, count=self.test_bars.value())

        def ok(res):
            self.btn_test.setEnabled(True)
            self.info.setText(f"{code} {res['bars']} 根:买点 {res['buy']} 次 · "
                              f"卖点 {res['sell']} 次"
                              + (" · 近 5 日有买点" if res.get("recent_buy") else ""))
            self._fill([("股票", code, TEXT), ("根数", str(res["bars"]), TEXT),
                        ("买点命中", f"{res['buy']} 次", UP if res["buy"] else DOWN),
                        ("卖点命中", f"{res['sell']} 次", UP if res["sell"] else DOWN),
                        ("近5日买点", "有" if res.get("recent_buy") else "无", TEXT)])

        def err(e):
            self.btn_test.setEnabled(True)
            self.info.setText(f"试跑失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_test.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    # ---------------------------------------------------------------- 保存
    def save(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "缺名称", "给策略起个名字")
            return
        buy = self.buy_edit.toPlainText().strip()
        sell = self.sell_edit.toPlainText().strip()
        if not buy and not sell:
            QMessageBox.warning(self, "内容为空", "先解析出买点或卖点")
            return
        for expr, tag in ((buy, "买点"), (sell, "卖点")):
            if expr:
                ok, why = rules_mod.validate(expr)
                if not ok:
                    QMessageBox.warning(self, f"{tag}有误", why)
                    return
        try:
            rules_mod.save_rule({"name": name, "desc": "外部导入", "buy": buy,
                                 "sell": sell, "max_hold": 0, "hold_until_sell": True})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
