# -*- coding: utf-8 -*-
"""策略广场页:内置截面策略 + 用户规则一览、详情,以及 Pine 脚本导入。

左栏合并两个来源:
  * `engine.strategy.list_strategies()` —— 6 个内置截面策略(name / desc / params)
  * `engine.rules.list_rules()`         —— 用户存下的条件规则(name/desc/buy/sell/...)

右栏上半是选中策略的完整信息,下半是 Pine 导入:
  粘贴脚本 → `engine.pine.translate()` → 显示买/卖表达式与 notes(失败显示 error 原文)
  → 「存为规则」用 `pine.to_rule()` 组 dict,再 `rules.save_rule()` 落盘,存完刷新列表。
"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QInputDialog, QLabel, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..engine import pine as pine_mod
from ..engine import rules as rules_mod
from ..engine import strategy as strategy_mod
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, label, set_cell, table
from .theme import ACCENT, DOWN, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

PINE_SAMPLE = """//@version=5
strategy("MA Cross", overlay=true)
fast = ta.sma(close, 10)
slow = ta.sma(close, 30)
longCond = ta.crossover(fast, slow)
if longCond
    strategy.entry("long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("long")
"""

_PINE_NAME = re.compile(r"strategy\s*\(\s*[\"']([^\"']+)[\"']")


def _fmt_num(v) -> str:
    """参数值转文本:浮点别丢成 5e+07 那种难读写法。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    if v is None:
        return "—"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e12:
            return str(int(v))
        return f"{v:g}"
    if isinstance(v, (list, tuple)):
        if not v:
            return "—"
        return "/".join(_fmt_num(x) for x in v)
    return str(v)


def _fmt_params(params: dict | None) -> str:
    if not params:
        return "—"
    return " · ".join(f"{k}={_fmt_num(v)}" for k, v in params.items())


class StrategyPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rows: list[dict] = []        # 列表数据:{"name","kind","raw","source"}
        self._selected: int = -1
        self._pine_rep: dict | None = None
        self._pine_src: str = ""           # 上次翻译用的源码,防「改了脚本存旧翻译」
        self._build()
        QTimer.singleShot(700, self.refresh)      # 自动出数,别让用户先点按钮

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新读取内置策略与用户规则")
        self.btn_refresh.clicked.connect(self.refresh)
        hl.addWidget(self.btn_refresh)
        self.btn_dsl = QPushButton("表达式手册")
        self.btn_dsl.setToolTip("DSL v3 全部算子:变量、自定义函数、参数占位、"
                                "横截面、状态算子、静态体检、报错含义")
        self.btn_dsl.clicked.connect(self.show_dsl_help)
        hl.addWidget(self.btn_dsl)
        self.btn_plan = QPushButton("编译计划")
        self.btn_plan.setToolTip("把当前买卖条件展开:原始几个节点、优化后几个、"
                                 "哪些子式被复用(公共子式只算一次)")
        self.btn_plan.clicked.connect(self.show_plan)
        hl.addWidget(self.btn_plan)
        tip = label("策略广场:左栏是 6 个内置截面策略和你存下的规则;点行看完整参数与"
                    "买卖表达式,把 Pine 脚本贴到右下角还能翻译成规则直接入库。", dim=True)
        tip.setWordWrap(True)
        hl.addWidget(tip, 1)
        root.addWidget(head)

        board = Board("strategy", cols=[0.42, 0.58], rows=[1.0])
        root.addWidget(board, 1)

        # 左栏:策略列表
        self.left_card = Card("策略库", hint="双击行可选中")
        self.tbl = table(["名称", "类型", "参数摘要 / 买卖条件"],
                         widths=[128, 78, 210], stretch_col=2)
        self.tbl.itemSelectionChanged.connect(self._on_select)
        self.tbl.cellDoubleClicked.connect(self._on_double_click)
        self.left_card.add(self.tbl, 1)
        board.add(self.left_card, 0, 0, min_w=300, min_h=200)

        # 右栏:详情(上) + Pine 导入(下)
        right = Board("strategy.right", cols=[1.0], rows=[0.55, 0.45])
        board.add(right, 0, 1, min_w=360, min_h=220)

        detail = Card("策略详情", hint="选中左侧任意一行")
        self.detail_lay = detail.body()
        self.d_name = QLabel("—")
        self.d_name.setStyleSheet("color:#ffffff;font-size:15px;font-weight:600;")
        self.d_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_lay.addWidget(self.d_name)
        self.d_kind = QLabel("")
        self.d_kind.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        self.detail_lay.addWidget(self.d_kind)
        self.d_desc = self._field("说明")
        self.d_buy = self._field("买入条件(买点)")
        self.d_buy.setObjectName("Mono")
        self.d_buy.setStyleSheet(f"color:{UP};")
        self.d_sell = self._field("卖出条件(卖点)")
        self.d_sell.setObjectName("Mono")
        self.d_sell.setStyleSheet(f"color:{DOWN};")
        self.d_rule_tip = self._field("表达式提示")
        self.d_rule_tip.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.d_params = self._field("参数")
        self.d_params.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.detail_lay.addStretch(1)
        right.add(detail, 0, 0, min_h=190)

        pine_card = Card("Pine 导入", hint="translate → 内部条件 → 存为规则")
        self.pine_edit = QPlainTextEdit()
        self.pine_edit.setPlaceholderText(PINE_SAMPLE)
        self.pine_edit.setMinimumHeight(84)
        pine_card.add(self.pine_edit, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.btn_translate = QPushButton("翻译")
        self.btn_translate.setObjectName("Primary")
        self.btn_translate.setToolTip("把上面的 Pine 脚本翻成内部买卖条件")
        self.btn_translate.clicked.connect(self.translate_pine)
        self.btn_save = QPushButton("存为规则")
        self.btn_save.setToolTip("翻译成功后存进策略库(回测/自动交易都能直接用)")
        self.btn_save.clicked.connect(self.save_as_rule)
        bar.addWidget(self.btn_translate)
        bar.addWidget(self.btn_save)
        bar.addStretch(1)
        pine_card._lay.addLayout(bar)

        self.pine_out = QLabel("把 Pine 脚本贴进上面,点「翻译」看结果。")
        self.pine_out.setTextFormat(Qt.RichText)
        self.pine_out.setWordWrap(True)
        self.pine_out.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.pine_out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.pine_out.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;"
                                    "font-family:'Microsoft YaHei UI';")
        self.pine_scroll = QScrollArea()
        self.pine_scroll.setWidgetResizable(True)
        self.pine_scroll.setFrameShape(QFrame.NoFrame)
        self.pine_scroll.setMinimumHeight(76)
        self.pine_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.pine_scroll.viewport().setStyleSheet("background:transparent;")
        holder = QWidget()
        holder.setStyleSheet("background:transparent;")
        holder_lay = QVBoxLayout(holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)
        holder_lay.setSpacing(0)
        holder_lay.addWidget(self.pine_out)
        holder_lay.addStretch(1)
        self.pine_scroll.setWidget(holder)
        pine_card.add(self.pine_scroll, 0)
        right.add(pine_card, 1, 0, min_h=230)

    def _field(self, caption: str) -> QLabel:
        """加一个「小标题 + 值」字段,返回值标签。"""
        cap = QLabel(caption)
        cap.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        self.detail_lay.addWidget(cap)
        val = QLabel("—")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val.setStyleSheet(f"color:{TEXT};")
        self.detail_lay.addWidget(val)
        return val

    # ------------------------------------------------------------ 取数
    def refresh(self) -> None:
        for t in list(self._tasks):
            if getattr(t, "task_name", "") == "strategy" and t.isRunning():
                return
        self.btn_refresh.setEnabled(False)
        self.left_card.set_hint("读取中…")

        def job():
            return {"builtin": strategy_mod.list_strategies(),
                    "rules": rules_mod.list_rules()}

        def ok(data):
            self.btn_refresh.setEnabled(True)
            self._fill(data or {})

        def err(e):
            self.btn_refresh.setEnabled(True)
            self.left_card.set_hint("读取失败")
            self.d_desc.setText(f"读取策略库失败:{e}")
            self.status.emit(f"策略库读取失败:{str(e).splitlines()[0][:160]}")

        r = TaskRunner(job, name="strategy")
        r.task_name = "strategy"
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn_refresh.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _fill(self, data: dict) -> None:
        builtin = list(data.get("builtin") or [])
        user = list(data.get("rules") or [])
        rows: list[dict] = []
        for s in builtin:
            rows.append({"name": str(s.get("name") or ""), "kind": "内置截面",
                         "raw": s, "source": "builtin"})
        for r in user:
            rows.append({"name": str(r.get("name") or ""), "kind": "用户规则",
                         "raw": r, "source": "rule"})
        self._rows = rows
        self.left_card.set_hint(f"{len(builtin)} 内置 · {len(user)} 规则")

        keep = self._current_name()
        self.tbl.blockSignals(True)
        self.tbl.setRowCount(len(rows))
        for i, it in enumerate(rows):
            set_cell(self.tbl, i, 0, it["name"], align="left", bold=True)
            set_cell(self.tbl, i, 1, it["kind"], align="left",
                     color=ACCENT if it["source"] == "builtin" else WARN)
            set_cell(self.tbl, i, 2, self._summary(it), align="left", color=TEXT_DIM)
        self.tbl.blockSignals(False)
        self._select_name(keep or (rows[0]["name"] if rows else ""))
        self.status.emit(f"策略库已刷新:{len(builtin)} 个内置策略 + {len(user)} 条用户规则")

    @staticmethod
    def _summary(it: dict) -> str:
        raw = it["raw"]
        if it["source"] == "builtin":
            return _fmt_params(raw.get("params"))[:72]
        buy = str(raw.get("buy") or "").strip() or "空"
        sell = str(raw.get("sell") or "").strip()
        text = f"买:{buy}" + (f"  卖:{sell}" if sell else "  · 无卖点")
        return text[:72]

    def _current_name(self) -> str:
        i = self.tbl.currentRow()
        if 0 <= i < len(self._rows):
            return self._rows[i]["name"]
        return ""

    def _select_name(self, name: str) -> None:
        row = next((i for i, it in enumerate(self._rows) if it["name"] == name), -1)
        if row < 0 and self._rows:
            row = 0
        self._selected = row
        if row < 0:
            self.tbl.clearSelection()
            self._show_empty()
            return
        self.tbl.selectRow(row)
        self.tbl.setCurrentCell(row, 0)
        self._fill_detail(self._rows[row])

    def _show_empty(self) -> None:
        self.d_name.setText("策略库是空的")
        self.d_kind.setText("")
        self.d_desc.setText("没有读到内置策略,也没有用户规则")
        self.d_buy.setText("—")
        self.d_sell.setText("—")
        self.d_params.setText("—")

    # ------------------------------------------------------------ 详情
    def _on_select(self) -> None:
        i = self.tbl.currentRow()
        if 0 <= i < len(self._rows):
            self._selected = i
            self._fill_detail(self._rows[i])

    def _fill_detail(self, it: dict) -> None:
        raw = it["raw"]
        self._rule = raw          # 「编译计划」按当前选中这条来展开
        self.d_name.setText(it["name"] or "—")
        if it["source"] == "builtin":
            self.d_kind.setText(f"{it['kind']} · 键名 {it['name']} · "
                                "由回测/自动交易按 name 调 build()")
            self.d_desc.setText(str(raw.get("desc") or "—"))
            self.d_buy.setText("截面策略:每日横截面排名选股,没有单票买点表达式")
            self.d_sell.setText("截面策略:按 n_long / exit_buffer 换仓,没有单票卖点表达式")
            self.d_rule_tip.setText("表达式引擎 v3 · 点右上「表达式手册」看全部算子")
            self.d_params.setText(_fmt_params(raw.get("params")) or "—")
        else:
            desc = str(raw.get("desc") or "").strip() or "—"
            hold = _fmt_num(raw.get("max_hold"))
            until = "持有到卖点" if raw.get("hold_until_sell", True) else "每日重判买点"
            self.d_kind.setText(f"{it['kind']} · 策略键 rule:{it['name']} · "
                                f"最多同时持 {hold} 只 · {until}")
            self.d_desc.setText(desc)
            buy = str(raw.get("buy") or "").strip()
            sell = str(raw.get("sell") or "").strip()
            self.d_buy.setText(buy or "(空,这条规则不能用于回测/自动交易)")
            if sell:
                self.d_sell.setText(sell)
            else:
                self.d_sell.setText("(空 —— 没有卖点,买入后会一直持有;"
                                    "要限天数请在卖点里写 hold_bars(买点, 卖点) > N)")
            self.d_rule_tip.setText(rules_mod.describe_rule(buy, sell))
            self.d_params.setText(f"最多同时持 {hold} 只 · hold_until_sell="
                                  f"{_fmt_num(bool(raw.get('hold_until_sell', True)))}")

    def _on_double_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._rows):
            name = self._rows[row]["name"]
            self.status.emit(f"已选中 {name}")

    # ------------------------------------------------------------ Pine
    def translate_pine(self) -> dict | None:
        src = self.pine_edit.toPlainText().strip()
        if not src:
            self._render_pine_error("没有可翻译的脚本:先把 Pine 脚本贴进上面的输入框")
            self.status.emit("先贴一段 Pine 脚本")
            self._pine_rep = None
            return None
        try:
            rep = pine_mod.translate(src)
        except Exception as e:  # noqa: BLE001
            self._pine_rep = None
            self._render_pine_error(f"翻译器异常:{e}")
            self.status.emit(f"Pine 翻译失败:{e}")
            return None
        self._pine_rep = rep
        self._pine_src = src
        self._render_pine(rep)
        if rep.get("ok"):
            buy = rep.get("buy") or "(无)"
            sell = rep.get("sell") or "(无)"
            self.status.emit(f"Pine 翻译成功 · 买点 {buy[:40]} · 卖点 {sell[:40]}")
        else:
            self.status.emit(f"Pine 翻译失败:{str(rep.get('error') or '').splitlines()[0][:150]}")
        return rep

    def show_dsl_help(self) -> None:
        """弹出表达式手册(引擎自己产出的函数表,不会和代码脱节)。"""
        try:
            from .dsl_help import DslHelpDialog
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"表达式手册打不开:{e}")
            return
        dlg = DslHelpDialog(self)
        dlg.exec()
        self._dsl_dlg = dlg

    def show_plan(self) -> None:
        """当前选中规则的编译计划:优化前后节点数、求值顺序、哪些子式被复用。"""
        rule = getattr(self, "_rule", None)
        if not rule:
            self.status.emit("先在左栏选一条规则,再点编译计划")
            return
        buy = str(rule.get("buy") or "")
        sell = str(rule.get("sell") or "")
        if not buy and not sell:
            self.status.emit("这条规则没有买卖表达式")
            return
        chunks = []
        for tag, expr in (("买点", buy), ("卖点", sell)):
            if not expr:
                continue
            try:
                prog = rules_mod.dsl.compile_expr(expr)
                chunks.append(f"【{tag}】{expr}\n{prog.plan()}")
            except Exception as e:  # noqa: BLE001
                chunks.append(f"【{tag}】{expr}\n  编译失败:{e}")
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("编译计划")
        dlg.resize(760, 560)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit(dlg)
        box.setReadOnly(True)
        box.setStyleSheet("font-family:Consolas,'Cascadia Mono',monospace;"
                          "font-size:12px;")
        box.setPlainText("\n\n".join(chunks))
        lay.addWidget(box, 1)
        b = QDialogButtonBox(QDialogButtonBox.Close, dlg)
        b.rejected.connect(dlg.reject)
        lay.addWidget(b)
        dlg.exec()
        self._plan_dlg = dlg

    def _render_pine(self, rep: dict) -> None:
        esc = html.escape
        if rep.get("ok"):
            parts = [f'<div style="color:{ACCENT};font-weight:600;">'
                     f'翻译成功 · 共 {int(rep.get("lines") or 0)} 行</div>']
            for tag, expr, col in (("买点", rep.get("buy"), UP),
                                   ("卖点", rep.get("sell"), DOWN)):
                text = esc(str(expr)) if expr else "(没有翻出)"
                parts.append(f'<div style="color:{TEXT_DIM};">{tag}</div>'
                             f'<div style="color:{col};'
                             f'font-family:Consolas,\'Cascadia Mono\',monospace;">'
                             f'{text}</div>')
            for n in (rep.get("notes") or []):
                parts.append(f'<div style="color:{WARN};">· {esc(str(n))}</div>')
            self.pine_out.setStyleSheet(
                f"color:{TEXT};font-size:12px;font-family:'Microsoft YaHei UI';")
            self.pine_out.setText("".join(parts))
        else:
            self._render_pine_error(str(rep.get("error") or "翻译失败(没有给出原因)"),
                                    notes=rep.get("notes") or [])

    def _render_pine_error(self, error: str, notes: list | None = None,
                           title: str = "翻译失败") -> None:
        """出错/不能存:原因原文一字不改地摆出来,不吞。"""
        esc = html.escape
        body = esc(str(error)).replace("\n", "<br>")
        parts = [f'<div style="color:{WARN};font-weight:600;">{esc(title)}</div>',
                 f'<div style="color:{TEXT};">{body}</div>']
        for n in (notes or []):
            parts.append(f'<div style="color:{TEXT_DIM};">· {esc(str(n))}</div>')
        self.pine_out.setStyleSheet(
            f"color:{TEXT};font-size:12px;font-family:'Microsoft YaHei UI';")
        self.pine_out.setText("".join(parts))

    def save_as_rule(self) -> None:
        rep = self._pine_rep
        if not rep or not rep.get("ok"):
            why = (rep or {}).get("error") or "还没有可保存的翻译结果,先点「翻译」"
            self._render_pine_error(f"不能存:翻译没成功 —— {why}", title="不能存为规则")
            self.status.emit("翻译没成功,不能存为规则")
            return
        if self.pine_edit.toPlainText().strip() != self._pine_src:
            self._render_pine_error("脚本在翻译之后又改过了,现在存下去会存成旧翻译的结果。"
                                    "请重新点「翻译」再存。", title="不能存为规则")
            self.status.emit("脚本改过了,先重新翻译再存")
            return
        default = self._guess_name() or "Pine 策略"
        name, ok = QInputDialog.getText(self, "存为规则", "规则名称(同名会覆盖):",
                                        text=default)
        if not ok:
            return
        name = name.strip()
        if not name:
            self.status.emit("规则名不能为空")
            return
        self._save_rule(name, rep)

    def _save_rule(self, name: str, rep: dict) -> bool:
        """把翻译结果落到策略库;失败把原因摆到 label 和状态栏。"""
        try:
            rule = pine_mod.to_rule(name, rep)
            rules_mod.save_rule(rule)
        except Exception as e:  # noqa: BLE001
            self._render_pine_error(f"存为规则失败:{e}")
            self.status.emit(f"存为规则失败:{e}")
            return False
        self.pine_out.setStyleSheet(
            f"color:{TEXT};font-size:12px;font-family:'Microsoft YaHei UI';")
        self.pine_out.setText(
            f'<div style="color:{ACCENT};font-weight:600;">已存为规则 · {html.escape(name)}</div>'
            f'<div style="color:{TEXT_DIM};">买点</div>'
            f'<div style="color:{UP};font-family:Consolas,monospace;">'
            f'{html.escape(str(rep.get("buy") or "(无)"))}</div>'
            f'<div style="color:{TEXT_DIM};">卖点</div>'
            f'<div style="color:{DOWN};font-family:Consolas,monospace;">'
            f'{html.escape(str(rep.get("sell") or "(无)"))}</div>'
            f'<div style="color:{TEXT_MUT};">回测/自动交易里按 rule:{html.escape(name)} 调用</div>')
        self.refresh()
        self.status.emit(f"已存为规则:{name}")
        return True

    def _guess_name(self) -> str:
        m = _PINE_NAME.search(self.pine_edit.toPlainText())
        if m:
            return m.group(1).strip()
        return ""

    # ------------------------------------------------------------ 收尾
    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:  # noqa: BLE001
                pass
        self._tasks.clear()
