# -*- coding: utf-8 -*-
"""自动交易页:定时调度 + 计划预览 + 自定义规则策略编辑。

两条路都走 engine.pilot.AutoPilot:
  * 自动:开关打开后线程常驻,到窗口期自己跑;
  * 手动:「立即执行」按钮 force 一次。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from ..broker.sim import SimGateway
from ..config import cfg
from ..engine import pilot, rules as rules_mod
from .import_dialog import ImportDialog
from .reverse_dialog import ReverseDialog
from .research_dialog import ResearchDialog
from ..engine.hub import TaskRunner
from ..utils import fmt_price, parse_hhmm
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import BORDER, DOWN, TEXT, TEXT_DIM, UP, WARN

POOLS = [("全市场", "all"), ("沪深300", "hs300"), ("中证500", "zz500"),
         ("中证1000", "zz1000"), ("上证50", "sz50"), ("按成交额", "amount"),
         ("自选股", "watchlist")]


class RuleDialog(QDialog):
    """新建/编辑一条自定义规则策略。"""

    def __init__(self, rule: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("策略编辑器")
        self.setMinimumSize(620, 460)
        self.setSizeGripEnabled(True)
        self._rule = rule or {}
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(self._rule.get("name") or "")
        self.name.setPlaceholderText("给策略起个名字,例:缩量回踩")
        self.desc = QLineEdit(self._rule.get("desc") or "")
        self.desc.setPlaceholderText("一句话说明(可空)")
        self.buy = QPlainTextEdit(self._rule.get("buy") or "close > ma(20)")
        self.buy.setFixedHeight(64)
        self.sell = QPlainTextEdit(self._rule.get("sell") or "close < ma(20)")
        self.sell.setFixedHeight(64)
        self.max_hold = QSpinBox()
        self.max_hold.setRange(0, 50)
        self.max_hold.setValue(int(self._rule.get("max_hold") or 5))
        self.max_hold.setToolTip("最多同时持有几只;0 表示不限")
        form.addRow("名称", self.name)
        form.addRow("说明", self.desc)
        form.addRow("买入条件", self.buy)
        form.addRow("卖出条件", self.sell)
        form.addRow("最多持仓", self.max_hold)
        root.addLayout(form)

        hint = QLabel(
            "可用:close open high low volume amount · ma(n) ema(n) vol_ma(n) pct(n) "
            "rsi(n) atr(n) · macd_dif/macd_dea/macd_hist · kdj_k/kdj_d/kdj_j · "
            "boll_up/boll_mid/boll_low · ref(x,n) hhv(x,n) llv(x,n) cross(a,b) · "
            "支持 and/or/not 与比较、四则运算")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        root.addWidget(hint)

        self.example = QComboBox()
        self.example.addItem("插入示例…", "")
        for ex in rules_mod.EXAMPLES:
            self.example.addItem(ex["name"], ex["name"])
        self.example.currentIndexChanged.connect(self._insert_example)
        self.test_out = QLabel("")
        self.test_out.setWordWrap(True)
        self.test_out.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        row = QHBoxLayout()
        row.addWidget(self.example)
        b_test = QPushButton("用茅台日线试跑")
        b_test.clicked.connect(self._test)
        row.addWidget(b_test)
        row.addStretch(1)
        root.addLayout(row)
        root.addWidget(self.test_out)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

    def _insert_example(self) -> None:
        name = self.example.currentData()
        if not name:
            return
        for ex in rules_mod.EXAMPLES:
            if ex["name"] == name:
                self.buy.setPlainText(ex["buy"])
                self.sell.setPlainText(ex["sell"])
                if not self.name.text():
                    self.name.setText(ex["name"])
                if not self.desc.text():
                    self.desc.setText(ex.get("desc", ""))
                break

    def _test(self) -> None:
        ok, err = rules_mod.validate(self.buy.toPlainText().strip())
        if not ok:
            self.test_out.setText(f"买入条件有误:{err}")
            return
        if self.sell.toPlainText().strip():
            ok2, err2 = rules_mod.validate(self.sell.toPlainText().strip())
            if not ok2:
                self.test_out.setText(f"卖出条件有误:{err2}")
                return
        self.test_out.setText("计算中…")

        code = default_code()
        if not code:
            try:
                from ..data.store import store
                row = store().conn.execute(
                    "select code from kline_cache where klt=101 and fqt=1 "
                    "order by bar_time desc limit 1").fetchone()
                code = row[0] if row else ""
            except Exception:  # noqa: BLE001
                code = ""
        if not code:
            self.test_out.setText("没有可用的样本股:先在行情页加自选")
            return

        def job():
            from ..data import kline as kl
            from ..engine import indicators as ind
            bars, _ = kl.get_kline(code, 101, 300)
            df = ind.bars_to_df(bars)
            b = rules_mod.evaluate(self.buy.toPlainText().strip(), df)
            s = (rules_mod.evaluate(self.sell.toPlainText().strip(), df)
                 if self.sell.toPlainText().strip() else None)
            return len(df), int(b.sum()), int(s.sum()) if s is not None else None, bool(b[-5:].any())

        runner = TaskRunner(job)
        runner.succeeded.connect(lambda r: self.test_out.setText(
            f"{code} {r[0]} 根日线:买点 {r[1]} 次" +
            (f",卖点 {r[2]} 次" if r[2] is not None else "") +
            ("  ·  最近 5 日内有买点" if r[3] else "  ·  近 5 日无买点")))
        runner.failed.connect(lambda e: self.test_out.setText(f"执行失败:{e}"))
        runner.start()
        self._runner = runner

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺名称", "给策略起个名字")
            return
        buy = self.buy.toPlainText().strip()
        ok, err = rules_mod.validate(buy)
        if not ok:
            QMessageBox.warning(self, "买入条件有误", err)
            return
        sell = self.sell.toPlainText().strip()
        if sell:
            ok, err = rules_mod.validate(sell)
            if not ok:
                QMessageBox.warning(self, "卖出条件有误", err)
                return
        try:
            rules_mod.save_rule({"name": name, "desc": self.desc.text().strip(),
                                 "buy": buy, "sell": sell,
                                 "max_hold": self.max_hold.value(),
                                 "hold_until_sell": True})
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()


class AutoPage(QWidget):
    status = Signal(str)

    def __init__(self, gateway: SimGateway | None = None, parent=None):
        # 第一个位置参数是网关(main_window 用 cls(self.gw) 注入)。
        # 但老写法 AutoPage(parent) 也传位置参数 —— 传进来的是 QWidget 就按 parent 处理,
        # 否则会把父控件当成网关,后面 self.gw.account() 直接炸。
        if gateway is not None and isinstance(gateway, QWidget):
            parent, gateway = gateway, None
        super().__init__(parent)
        self.cfg = cfg()
        self.gw = gateway or SimGateway()
        self._sim_gw = self.gw
        self._ths_gw = None
        self._live_checked = False
        self._pilot: pilot.AutoPilot | None = None
        self._tasks: list[TaskRunner] = []
        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self.load_rules_list()
        self.refresh_log()

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("auto", cols=[1.0], rows=[0.24, 0.09, 0.40, 0.27])
        root.addWidget(board)

        ctrl = Card("调度设置")
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)

        self.strategy = QComboBox()
        self.pool = QComboBox()
        for text, key in POOLS:
            self.pool.addItem(text, key)
        self.n_stocks = QSpinBox()
        self.n_stocks.setRange(10, 500)
        self.n_hold = QSpinBox()
        self.n_hold.setRange(1, 20)
        self.per_pct = QDoubleSpinBox()
        self.per_pct.setRange(0.02, 1.0)
        self.per_pct.setSingleStep(0.02)
        self.per_pct.setDecimals(2)
        self.dry = QCheckBox("演练(不下单)")
        self.win_start = QLineEdit()
        self.win_end = QLineEdit()
        self.check_sec = QSpinBox()
        self.check_sec.setRange(10, 600)
        self.check_sec.setSuffix(" 秒")
        # 运行模式:每日一次 / 按间隔连续 / 每天指定次数
        self.mode = QComboBox()
        self.mode.addItem("每日一次(窗口内首次)", "daily")
        self.mode.addItem("按间隔连续(窗口内反复)", "interval")
        self.mode.addItem("每天指定次数", "count")
        self.max_runs = QSpinBox()
        self.max_runs.setRange(0, 200)
        self.max_runs.setSpecialValueText("不限")
        self.max_runs.setToolTip("0 = 不限次;窗口内每次检查都可能执行")
        self.max_orders = QSpinBox()
        self.max_orders.setRange(0, 9999)
        self.max_orders.setSpecialValueText("不限")
        self.max_orders.setToolTip("每日下单笔数上限;0 = 不限,全自动下完为止")
        # 模拟账户:下单落到哪个账户
        self.account = QComboBox()
        self.account.setToolTip("自动交易写入哪个模拟账户")
        # 自定义股票池:填代码就用这些,不填才走上面的池
        self.codes = QLineEdit()
        self.codes.setPlaceholderText("自定义代码,逗号分隔(留空用上面选的池)")
        # 止损止盈(%):0 = 关
        self.stop_loss = QDoubleSpinBox()
        self.stop_loss.setRange(0, 50)
        self.stop_loss.setDecimals(1)
        self.stop_loss.setSuffix(" %")
        self.stop_loss.setToolTip("浮亏超过该比例自动卖出;0 = 关")
        self.take_profit = QDoubleSpinBox()
        self.take_profit.setRange(0, 200)
        self.take_profit.setDecimals(1)
        self.take_profit.setSuffix(" %")
        self.take_profit.setToolTip("浮盈超过该比例自动卖出;0 = 关")
        # 私有学习:分批下单 + 决策记忆 + 参数迭代。不对外,只在本机生效。
        self.sl_on = QCheckBox("启用")
        self.sl_on.setToolTip("分批建仓/减仓,记住每次决策,按历史表现调参数。\n"
                              "关闭时行为与原来完全一致(一次性按目标仓位下单)")
        # 成本摊薄(做T):不是独立策略,只是持仓上顺手压成本
        self.cost_avg = QCheckBox("成本摊薄")
        self.cost_avg.setToolTip(
            "对已有底仓的票做价差,赚到的差价等于把持仓成本压下来。\n"
            "不规定先买还是先卖:浮盈到阈值先卖、回落买回;浮亏到阈值先买、反弹卖出。\n"
            "不限轮次,一轮平掉后继续按同样规则判断;没到阈值就不动,不动才是常态。\n"
            "硬约束只有 T+1 —— 当天买进的份额当天不能卖。")
        self.cost_avg_band = QDoubleSpinBox()
        self.cost_avg_band.setRange(0.5, 30)
        self.cost_avg_band.setDecimals(1)
        self.cost_avg_band.setSuffix(" %")
        self.cost_avg_band.setToolTip("现价相对持仓成本涨到这个幅度才高抛")
        self.cost_avg_take = QDoubleSpinBox()
        self.cost_avg_take.setRange(0.3, 15)
        self.cost_avg_take.setDecimals(1)
        self.cost_avg_take.setSuffix(" %")
        self.cost_avg_take.setToolTip("高抛之后,现价回落到这个幅度才买回")

        rows = [
            ("策略", self.strategy, "股票池", self.pool),
            ("扫描只数", self.n_stocks, "目标持仓", self.n_hold),
            ("单票比例", self.per_pct, "检查间隔", self.check_sec),
            ("执行窗口", self._win_row(), None, None),
            ("运行模式", self.mode, "每日次数", self.max_runs),
            ("每日笔数上限", self.max_orders, "模拟账户", self.account),
            ("止损", self.stop_loss, "止盈", self.take_profit),
            ("自定义股票池", self.codes, "私有学习", self.sl_on),
            ("成本摊薄", self.cost_avg, "高抛阈值", self.cost_avg_band),
            ("回补幅度", self.cost_avg_take, None, None),
        ]
        # 两列各 4 组:单列 8 行会把卡片顶到 400 高,挤掉下面的计划/日志
        # 注意是 (行, 列) = (i % 4, i // 4) —— 反了会铺成 16 列,
        # 第 3、7 组就落到没有 stretch 的列上,被压到 100px 宽,控件溢出卡片。
        for i, (l1, w1, l2, w2) in enumerate(rows):
            r, c = i % 4, i // 4
            g.addWidget(QLabel(l1), r, c * 4)
            g.addWidget(w1, r, c * 4 + 1)
            if l2:
                g.addWidget(QLabel(l2), r, c * 4 + 2)
                g.addWidget(w2, r, c * 4 + 3)
        for col in (1, 5):
            g.setColumnStretch(col, 1)
        ctrl._lay.addLayout(g)

        # 资金守卫:回撤闸 + 动态系数。
        # 不塞进上面的 QGridLayout —— 那套是 4 行×2 列的固定算法(i%4, i//4),
        # 再加两项会落到第 3 列,而第 3 列没有 stretch,控件会被压到溢出卡片。
        gl = QHBoxLayout()
        gl.setSpacing(6)
        self.auto_mode = QCheckBox("全自动")
        self.auto_mode.setToolTip(
            "仓位、止盈止损、回撤上限全部按个股波动率和账户回撤自动算\n"
            "(engine/autopilot.py,确定性规则,每个数字可在下面看到来源)\n"
            "勾上后右边这几项由它接管,不用手填")
        self.auto_mode.toggled.connect(self._on_auto_mode)
        gl.addWidget(self.auto_mode)
        gl.addSpacing(10)
        self.max_dd = QDoubleSpinBox()
        self.max_dd.setRange(0, 90)
        self.max_dd.setDecimals(1)
        self.max_dd.setSuffix(" %")
        self.max_dd.setToolTip("账户权益从最高点回撤超过该比例就按右边动作处理;0 = 关")
        self.dd_action = QComboBox()
        from ..engine.adaptive import ACTIONS as _DD_ACTIONS
        for text, key in _DD_ACTIONS:
            self.dd_action.addItem(text, key)
        self.btn_dd_reset = QPushButton("重置回撤闸")
        self.btn_dd_reset.setToolTip("手动清掉「已触发」状态(峰值也会归零重算)")
        self.btn_dd_reset.clicked.connect(self._reset_guard)
        self.adaptive = QCheckBox("动态调整仓位")
        self.adaptive.setToolTip("按最近几次调仓的权益变化率把仓位系数调高或调低")
        self.adapt_win = QSpinBox()
        self.adapt_win.setRange(4, 60)
        self.adapt_win.setSuffix(" 次")
        self.adapt_win.setToolTip("参与计算的样本数,越多越稳、越少越灵")
        gl.addWidget(QLabel("最大回撤"))
        gl.addWidget(self.max_dd)
        gl.addWidget(QLabel("动作"))
        gl.addWidget(self.dd_action)
        gl.addWidget(self.btn_dd_reset)
        gl.addSpacing(14)
        gl.addWidget(self.adaptive)
        gl.addWidget(QLabel("样本窗口"))
        gl.addWidget(self.adapt_win)
        gl.addStretch(1)
        self.guard_hint = QLabel("")
        self.guard_hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        gl.addWidget(self.guard_hint)
        ctrl._lay.addLayout(gl)

        # 组合权重 + 拆单:对标市面量化的组合层和执行层
        wl = QHBoxLayout()
        wl.setSpacing(6)
        self.weight_mode = QComboBox()
        self.weight_mode.addItem("等权(默认)", "equal")
        self.weight_mode.addItem("风险平价", "risk_parity")
        self.weight_mode.addItem("均值方差", "mean_variance")
        self.weight_mode.addItem("最大分散化", "max_div")
        self.weight_mode.setToolTip("组合权重方案:等权 / 风险平价 / 均值方差 / 最大分散化")
        self.exec_mode = QComboBox()
        self.exec_mode.addItem("不拆单", "none")
        self.exec_mode.addItem("TWAP 时间加权", "twap")
        self.exec_mode.addItem("VWAP 成交量加权", "vwap")
        self.exec_mode.setToolTip("大单拆单方式:TWAP 均分 / VWAP 按成交量分布")
        wl.addWidget(QLabel("权重方案"))
        wl.addWidget(self.weight_mode)
        wl.addSpacing(14)
        wl.addWidget(QLabel("拆单"))
        wl.addWidget(self.exec_mode)
        wl.addStretch(1)
        ctrl._lay.addLayout(wl)

        self.auto_hint = QLabel("")
        self.auto_hint.setWordWrap(True)
        self.auto_hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        ctrl._lay.addWidget(self.auto_hint)

        bl = QHBoxLayout()
        self.btn_save = QPushButton("保存设置")
        self.btn_save.clicked.connect(self.save_settings)
        self.btn_now = QPushButton("立即执行一次")
        self.btn_now.clicked.connect(self.run_now)
        self.btn_report = QPushButton("今日学习报告")
        self.btn_report.setToolTip("看今天学了什么:决策、回填、参数有没有动")
        self.btn_report.clicked.connect(self.show_report)
        self.btn_cap = QPushButton("资金方案")
        self.btn_cap.setToolTip("按账户现有资金算:买几只、每只多少、首笔下多少")
        self.btn_cap.clicked.connect(self.show_capital_plan)
        self.btn_auto = QPushButton("启动自动交易")
        self.btn_auto.setObjectName("Primary")
        self.btn_auto.setCheckable(True)
        self.btn_auto.toggled.connect(self.toggle_auto)
        self.gw_kind = QComboBox()
        self.gw_kind.addItem("模拟盘", "sim")
        self.gw_kind.addItem("同花顺(真钱)", "ths")
        self.gw_kind.setToolTip("同花顺走界面自动下单,先跑 tools/probe_ths.py 校准")
        self.gw_kind.currentIndexChanged.connect(self._on_gw_kind)
        bl.addWidget(self.dry)
        bl.addWidget(QLabel("通道"))
        bl.addWidget(self.gw_kind)
        bl.addStretch(1)
        bl.addWidget(self.btn_save)
        bl.addWidget(self.btn_now)
        bl.addWidget(self.btn_cap)
        bl.addWidget(self.btn_report)
        bl.addWidget(self.btn_auto)
        ctrl._lay.addLayout(bl)
        self.gw_hint = QLabel("")
        self.gw_hint.setWordWrap(True)
        self.gw_hint.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        ctrl._lay.addWidget(self.gw_hint)
        board.add(ctrl, 0, 0, min_h=214)

        stats = Board("auto.stats", cols=[1.0 / 6] * 6, rows=[1.0])
        self.st_state = StatCard("自动状态", "未启动", "打开开关后常驻")
        self.st_next = StatCard("下次窗口", "--", "到点自动算计划")
        self.st_strategy = StatCard("当前策略", "--", "")
        self.st_last = StatCard("最近一次", "尚未执行", "")
        self.st_dd = StatCard("当前回撤", "--", "峰值未记录")
        self.st_mult = StatCard("仓位系数", "1.00", "动态调整未开启")
        for i, s in enumerate((self.st_state, self.st_next, self.st_strategy,
                               self.st_last, self.st_dd, self.st_mult)):
            stats.add(s, 0, i, min_w=120, min_h=70)
        board.add(stats, 1, 0, min_h=76)

        body = Board("auto.body", cols=[0.56, 0.44], rows=[1.0])
        # 左:计划
        left = Card("本次计划")
        self.cap_lbl = label("资金方案:还没算", dim=True, size=11)
        self.cap_lbl.setWordWrap(True)
        left.add(self.cap_lbl)
        self.plan_tbl = table(["代码", "名称", "方向", "数量", "价格", "状态"],
                              widths=[64, 96, 54, 70, 78, 140], stretch_col=5)
        left.add(self.plan_tbl, 1)
        self.plan_msg = QLabel("还没有计划")
        self.plan_msg.setWordWrap(True)
        self.plan_msg.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;")
        left.add(self.plan_msg)
        body.add(left, 0, 0, min_w=380, min_h=180)

        # 右:自定义策略
        right = Card("我的策略(按历史走势)")
        self.rule_list = QListWidget()
        self.rule_list.setMinimumWidth(240)
        right.add(self.rule_list, 2)
        rb = QHBoxLayout()
        b_new = QPushButton("新建")
        b_new.clicked.connect(self.new_rule)
        b_edit = QPushButton("编辑")
        b_edit.clicked.connect(self.edit_rule)
        b_del = QPushButton("删除")
        b_del.clicked.connect(self.del_rule)
        b_res = QPushButton("自主研究")
        b_res.setObjectName("Primary")
        b_res.setToolTip("拿这只股票的历史走势,枚举形态模板自动找参数")
        b_res.clicked.connect(self.open_research)
        b_rev = QPushButton("反向推算")
        b_rev.setToolTip("从这只票的历史走势倒推:哪些条件之后涨得多")
        b_rev.clicked.connect(self.open_reverse)
        b_imp = QPushButton("导入策略")
        b_imp.setToolTip("粘贴别人写的买卖策略,自动拆成买点和卖点")
        b_imp.clicked.connect(self.open_import)
        for b in (b_new, b_edit, b_del, b_res, b_rev, b_imp):
            rb.addWidget(b)
        right.add(self._row_widget(rb))
        right.add(label("条件写在日线上,买点进场、卖点出场,可直接接自动交易", dim=True))
        body.add(right, 0, 1, min_w=320, min_h=180)
        board.add(body, 2, 0, min_h=200)

        logc = Card("执行日志")
        self.log_tbl = table(["时间", "策略", "摘要"], widths=[132, 130, 700], stretch_col=2)
        logc.add(self.log_tbl, 1)
        board.add(logc, 3, 0, min_h=140)

        self._load_settings()

    def _row_widget(self, lay):
        w = QWidget()
        w.setLayout(lay)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        return w

    def _win_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        # 60px 装不下 "15:00"+内边距(会溢出容器),给足;用最小宽不锁死
        for ed in (self.win_start, self.win_end):
            ed.setMinimumWidth(74)
            ed.setMaximumWidth(96)
        lay.addWidget(self.win_start)
        lay.addWidget(QLabel("至"))
        lay.addWidget(self.win_end)
        lay.addStretch(1)
        return w

    # ------------------------------------------------------------ 设置
    def _on_gw_kind(self) -> None:
        self._sync_gateway()
        self._refresh_gw_hint()

    def _sync_gateway(self) -> None:
        """按通道选网关:模拟盘复用 main_window 建的那个,同花顺懒建。"""
        kind = self.gw_kind.currentData()
        if kind == "ths":
            if self._ths_gw is None:
                from ..broker.ths import THSGateway
                self._ths_gw = THSGateway(self.cfg)
            self.gw = self._ths_gw
        else:
            self.gw = self._sim_gw
        self._reload_accounts()

    def _refresh_gw_hint(self) -> None:
        kind = self.gw_kind.currentData()
        if kind != "ths":
            self.gw_hint.setText("模拟盘:本地撮合,规则与回测一致,不碰真钱")
            return
        dry_ths = bool(self.cfg.get("broker.ths.dry_run", True))
        cash = float(self.cfg.get("broker.ths.cash", 0) or 0)
        bits = ["同花顺通道:真钱 · 先跑 python tools/probe_ths.py scan 校准窗口"]
        bits.append(f"演练={'开' if dry_ths else '关(会真发单)'}")
        if cash <= 0:
            bits.append("可用资金未填(设置页填「同花顺可用资金」,否则买不进)")
        self.gw_hint.setText("  |  ".join(bits))

    def _load_settings(self) -> None:
        c = self.cfg
        self._reload_strategies()
        key = c.get("autotrade.strategy", "momentum20")
        i = self.strategy.findData(key)
        self.strategy.setCurrentIndex(max(0, i))
        pool = c.get("autotrade.pool", "hs300")
        j = self.pool.findData(pool)
        self.pool.setCurrentIndex(max(0, j))
        self.n_stocks.setValue(int(c.get("autotrade.n_stocks", 60)))
        self.n_hold.setValue(int(c.get("autotrade.n_hold", 5)))
        self.per_pct.setValue(float(c.get("autotrade.per_position_pct", 0.18)))
        self.dry.setChecked(bool(c.get("autotrade.dry_run", True)))
        self.win_start.setText(parse_hhmm(c.get("autotrade.window_start"), "14:55"))
        self.win_end.setText(parse_hhmm(c.get("autotrade.window_end"), "15:05"))
        self.check_sec.setValue(int(c.get("autotrade.check_seconds", 30)))
        runs = int(c.get("autotrade.max_runs_per_day", 1) or 0)
        self.max_runs.setValue(runs)
        self.max_orders.setValue(int(c.get("autotrade.max_orders_per_day", 0) or 0))
        mode = "daily" if runs == 1 else ("interval" if runs == 0 else "count")
        i = self.mode.findData(mode)
        self.mode.setCurrentIndex(max(0, i))
        self.stop_loss.setValue(float(c.get("autotrade.stop_loss_pct", 0) or 0))
        self.take_profit.setValue(float(c.get("autotrade.take_profit_pct", 0) or 0))
        self.cost_avg.setChecked(bool(c.get("autotrade.cost_avg_enabled", False)))
        self.cost_avg_band.setValue(float(c.get("autotrade.cost_avg_band_pct", 5.0) or 5.0))
        self.cost_avg_take.setValue(float(c.get("autotrade.cost_avg_take_pct", 2.0) or 2.0))
        self.max_dd.setValue(float(c.get("autotrade.max_drawdown_pct", 0) or 0) * 100)
        k = self.dd_action.findData(c.get("autotrade.dd_action", "stop_new"))
        self.dd_action.setCurrentIndex(max(0, k))
        self.adaptive.setChecked(bool(c.get("autotrade.adaptive_enabled", False)))
        self.adapt_win.setValue(int(c.get("autotrade.adaptive_window", 10) or 10))
        self.auto_mode.setChecked(bool(c.get("autotrade.auto_mode", False)))
        self._on_auto_mode(self.auto_mode.isChecked())
        self.codes.setText(str(c.get("autotrade.codes", "") or ""))
        self.sl_on.setChecked(bool(c.get("selflearn.enabled", False)))
        wm = self.weight_mode.findData(c.get("autotrade.weight_mode", "equal"))
        self.weight_mode.setCurrentIndex(max(0, wm))
        em = self.exec_mode.findData(c.get("autotrade.exec_mode", "none"))
        self.exec_mode.setCurrentIndex(max(0, em))
        gk = c.get("autotrade.gateway", "sim")
        k = self.gw_kind.findData(gk)
        self.gw_kind.blockSignals(True)
        self.gw_kind.setCurrentIndex(max(0, k))
        self.gw_kind.blockSignals(False)
        self._sync_gateway()
        self._refresh_gw_hint()

    def _reload_strategies(self) -> None:
        cur = self.strategy.currentData()
        self.strategy.clear()
        for text, key in pilot.strategy_choices():
            self.strategy.addItem(text, key)
        if cur:
            i = self.strategy.findData(cur)
            if i >= 0:
                self.strategy.setCurrentIndex(i)

    def show_report(self) -> None:
        """今日学习报告。没有决策也照样出 —— 说明今天没动,也是信息。"""
        import os

        from PySide6.QtWidgets import (QDialog, QHBoxLayout, QPlainTextEdit,
                                       QPushButton, QVBoxLayout)

        from ..config import USER_DIR
        from ..engine import selflearn as sl

        acct = self.account.currentText() or "default"
        strat = self.strategy.currentData() or ""
        # 优先读云端:本地下线期间云端也在跑,那份报告更全
        md, src, day = "", "本地", ""
        try:
            from .. import cloud
            if cloud.enabled():
                d = cloud.learn_report()
                md = d.get("markdown") or ""
                src, day = "云端", str(d.get("day") or "")
        except Exception:  # noqa: BLE001
            md = ""
        if not md:
            rep = sl.daily_report(acct, strat)
            md = sl.render_markdown(rep)
            sl.save_report(rep)
            day = str(rep.get("day") or "")

        dlg = QDialog(self)
        dlg.setWindowTitle(f"自主学习报告 · {day} · {src}")
        dlg.resize(780, 640)
        lay = QVBoxLayout(dlg)
        ed = QPlainTextEdit(md)
        ed.setReadOnly(True)
        lay.addWidget(ed)
        row = QHBoxLayout()
        b_open = QPushButton("打开报告目录")
        b_open.clicked.connect(
            lambda: os.startfile(str(USER_DIR / "reports")))
        row.addWidget(b_open)
        row.addStretch(1)
        b_ok = QPushButton("关闭")
        b_ok.clicked.connect(dlg.accept)
        row.addWidget(b_ok)
        lay.addLayout(row)
        dlg.exec()

    def show_capital_plan(self) -> None:
        """按账户现有资金算一套方案:买几只、每只多少、首笔下多少。"""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

        from ..engine import capital_plan as cp

        try:
            acct = self.gw.account()
            equity = float(acct.get("total") or acct.get("cash") or 0)
            cash = float(acct.get("cash") or 0)
            p = cp.plan(
                equity, cash,
                per_pct=float(self.per_pct.value()),
                max_order=float(self.cfg.get("risk.max_order_amount", 50000) or 50000),
                n_hold=int(self.n_hold.value()),
                cash_floor_pct=float(self.cfg.get("autotrade.cash_floor_pct", 0.10) or 0),
            )
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "资金方案", f"算不出来:{e}")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("资金方案")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        ed = QPlainTextEdit(cp.render(p))
        ed.setReadOnly(True)
        lay.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def save_settings(self) -> None:
        c = self.cfg
        for k, v in (("strategy", self.strategy.currentData()),
                     ("pool", self.pool.currentData()),
                     ("n_stocks", self.n_stocks.value()),
                     ("n_hold", self.n_hold.value()),
                     ("per_position_pct", round(self.per_pct.value(), 3)),
                     ("dry_run", self.dry.isChecked()),
                     ("window_start", parse_hhmm(self.win_start.text(), "14:55")),
                     ("window_end", parse_hhmm(self.win_end.text(), "15:05")),
                     ("check_seconds", self.check_sec.value()),
                     ("max_runs_per_day", self._runs_value()),
                     ("max_orders_per_day", self.max_orders.value()),
                     ("stop_loss_pct", round(self.stop_loss.value(), 1)),
                     ("take_profit_pct", round(self.take_profit.value(), 1)),
                     ("cost_avg_enabled", self.cost_avg.isChecked()),
                     ("cost_avg_band_pct", round(self.cost_avg_band.value(), 1)),
                     ("cost_avg_take_pct", round(self.cost_avg_take.value(), 1)),
                     ("max_drawdown_pct", round(self.max_dd.value() / 100, 4)),
                     ("dd_action", self.dd_action.currentData() or "stop_new"),
                     ("adaptive_enabled", self.adaptive.isChecked()),
                     ("adaptive_window", self.adapt_win.value()),
                     ("auto_mode", self.auto_mode.isChecked()),
                     ("codes", self.codes.text().strip()),
                     ("gateway", self.gw_kind.currentData() or "sim"),
                     ("account", self.account.currentText() or "default"),
                     ("weight_mode", self.weight_mode.currentData() or "equal"),
                     ("exec_mode", self.exec_mode.currentData() or "none")):
            c.set(f"autotrade.{k}", v)
        # 私有学习开关:分批 + 记忆 + 迭代,默认关
        c.set("selflearn.enabled", self.sl_on.isChecked())
        c.save()
        self.status.emit("调度设置已保存")
        self._refresh_stats()

    def _reload_accounts(self) -> None:
        cur = self.cfg.get("autotrade.account", "default")
        self.account.clear()
        try:
            for name in self.gw.list_accounts():
                self.account.addItem(name)
        except Exception:
            self.account.addItem("default")
        i = self.account.findText(str(cur))
        self.account.setCurrentIndex(max(0, i))

    def _code_list(self) -> list[str]:
        raw = self.codes.text().strip()
        if not raw:
            return []
        return [x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()]

    def _runs_value(self) -> int:
        """运行模式 → 每天执行次数(0 = 不限)。"""
        mode = self.mode.currentData()
        if mode == "interval":
            return 0
        if mode == "daily":
            return 1
        return self.max_runs.value()

    # ------------------------------------------------------------ 启停
    def toggle_auto(self, on: bool) -> None:
        if on:
            self.save_settings()
            if not self._check_live():
                self.btn_auto.blockSignals(True)
                self.btn_auto.setChecked(False)
                self.btn_auto.blockSignals(False)
                return
            gw = self.gw
            if getattr(gw, "is_live", False):
                ok, msg = gw.connect()
                self.status.emit(msg)
                if not ok:
                    QMessageBox.warning(self, "没连上同花顺", msg)
                    self.btn_auto.blockSignals(True)
                    self.btn_auto.setChecked(False)
                    self.btn_auto.blockSignals(False)
                    return
            self._pilot = pilot.AutoPilot(gw, self)
            self._pilot.plan_ready.connect(self._on_plan)
            self._pilot.log.connect(self._on_log)
            self._pilot.status.connect(self.status.emit)
            self._pilot.tick.connect(self._on_tick)
            self._pilot.start()
            self.btn_auto.setText("停止自动交易")
            self.cfg.set("autotrade.enabled", True, save=True)
            self.status.emit("自动交易已启动")
        else:
            if self._pilot:
                try:
                    self._pilot.stop()
                except Exception:
                    pass
                self._pilot = None
            self.btn_auto.setText("启动自动交易")
            self.cfg.set("autotrade.enabled", False, save=True)
            self.status.emit("自动交易已停止")
        self._refresh_stats()

    def _check_live(self) -> bool:
        """走同花顺且关了演练时,启动前强确认一次(每次启动都问)。"""
        if self.gw_kind.currentData() != "ths" or self.dry.isChecked():
            return True
        if bool(self.cfg.get("broker.ths.dry_run", True)):
            return True      # 界面层还是演练,不会真发
        cash = float(self.cfg.get("broker.ths.cash", 0) or 0)
        ret = QMessageBox.warning(
            self, "启动真钱自动交易",
            f"通道:同花顺(界面自动下单)\n"
            f"演练:关 · 可用资金:{cash:,.0f}\n"
            f"单票比例:{self.per_pct.value():.0%} · 最多持仓:{self.n_hold.value()} 只\n\n"
            "开启后到执行窗口会真的往券商提交委托。程序不会自动重试下单,\n"
            "但界面自动化的失败(焦点被抢、弹窗)只能靠事后核对。\n\n确定启动?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return ret == QMessageBox.Yes

    def run_now(self) -> None:
        self.save_settings()
        if self._pilot and self._pilot.isRunning():
            self._pilot.run_now()
            self.status.emit("已请求立即执行")
            return
        if not self._check_live():
            return
        gw = self.gw
        self.btn_now.setEnabled(False)
        self.plan_msg.setText("计算中…")
        conf = {
            "strategy": self.strategy.currentData(), "pool": self.pool.currentData(),
            "n_stocks": self.n_stocks.value(), "n_hold": self.n_hold.value(),
            "per_position_pct": self.per_pct.value(), "dry_run": self.dry.isChecked(),
            "max_orders_per_day": self.max_orders.value(),
            "codes": self._code_list(),
            "stop_loss": self.stop_loss.value() / 100,
            "take_profit": self.take_profit.value() / 100,
            "cost_avg_enabled": self.cost_avg.isChecked(),
            "cost_avg_band": self.cost_avg_band.value() / 100,
            "cost_avg_take": self.cost_avg_take.value() / 100,
            "max_drawdown": self.max_dd.value() / 100,
            "dd_action": self.dd_action.currentData() or "stop_new",
            "adaptive": self.adaptive.isChecked(),
            "adaptive_window": self.adapt_win.value(),
            "auto_mode": self.auto_mode.isChecked(),
            "account": self.account.currentText(),
            "exec_mode": self.exec_mode.currentData() or "none",
            "weight_mode": self.weight_mode.currentData() or "equal",
        }

        def job():
            from ..engine.autotrader import AutoTrader
            if getattr(gw, "is_live", False):
                okc, msgc = gw.connect()
                if not okc:
                    raise RuntimeError(msgc)
            if conf.get("account"):
                try:
                    gw.switch_account(conf["account"])
                except Exception:
                    pass
            at = AutoTrader(gw, strategy_name=conf["strategy"], pool=conf["pool"],
                            n_stocks=conf["n_stocks"], n_hold=conf["n_hold"],
                            per_position_pct=conf["per_position_pct"],
                            dry_run=conf["dry_run"],
                            max_orders_per_day=conf["max_orders_per_day"],
                            codes=conf["codes"] or None,
                            stop_loss=conf["stop_loss"],
                            take_profit=conf["take_profit"],
                            max_drawdown=conf.get("max_drawdown"),
                            dd_action=conf.get("dd_action"),
                            adaptive=conf.get("adaptive"),
                            adaptive_window=conf.get("adaptive_window"),
                            auto_mode=conf.get("auto_mode"),
                            exec_mode=conf.get("exec_mode") or "none",
                            weight_mode=conf.get("weight_mode") or "equal",
                            cost_avg=conf.get("cost_avg_enabled"),
                            cost_avg_band=conf.get("cost_avg_band"),
                            cost_avg_take=conf.get("cost_avg_take"),
                            account_name=conf.get("account") or "default")
            return at.run_once()

        def ok(plan):
            self._on_plan(plan)
            self.btn_now.setEnabled(True)

        def err(e):
            self.plan_msg.setText(f"执行失败:{e}")
            self.btn_now.setEnabled(True)

        def done():
            self.btn_now.setEnabled(True)

        self._run(job, ok, "runnow", on_err=err, on_done=done)

    # ------------------------------------------------------------ 回调
    def _on_plan(self, plan) -> None:
        rows = []
        for code, _qty, name in plan.target:
            rows.append((code, name, "目标", "", "", "入选"))
        for x in plan.sells:
            rows.append((x.get("code"), x.get("name"), "卖出", str(x.get("qty")),
                         fmt_price(x.get("price")), "演练" if plan.dry_run else x.get("status", "")))
        for x in plan.buys:
            rows.append((x.get("code"), x.get("name"), "买入", str(x.get("qty")),
                         fmt_price(x.get("price")), "演练" if plan.dry_run else x.get("status", "")))
        for x in plan.skipped:
            rows.append((x.get("code"), "", "跳过", "", "", str(x.get("reason"))[:60]))
        self.plan_tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            col = UP if r[2] == "买入" else (DOWN if r[2] == "卖出" else TEXT_DIM)
            set_cell(self.plan_tbl, i, 0, str(r[0]), align="left")
            set_cell(self.plan_tbl, i, 1, str(r[1] or ""), align="left")
            set_cell(self.plan_tbl, i, 2, r[2], col, bold=(r[2] in ("买入", "卖出")))
            set_cell(self.plan_tbl, i, 3, r[3])
            set_cell(self.plan_tbl, i, 4, r[4])
            set_cell(self.plan_tbl, i, 5, str(r[5]), align="left")
        self.plan_msg.setText(plan.summary() + ("  ·  " + plan.message if plan.message else ""))
        cap = getattr(plan, "capital", None) or {}
        if cap:
            from ..engine import capital_plan as cp
            self.cap_lbl.setText(cp.one_line(cap)
                                 + ("  ·  " + ";".join(cap.get("warnings") or [])
                                    if cap.get("warnings") else ""))
        self.st_last.set(plan.summary()[:28])
        self.refresh_log()

    def _on_log(self, text: str) -> None:
        self.plan_msg.setText(text)
        self.refresh_log()

    def _on_tick(self, st: dict) -> None:
        self.st_next.set(st.get("next_run", "--"), hint=f"最近执行 {st.get('last_run_day') or '无'}")
        self.st_state.set("运行中", UP, hint=f"{st.get('next_check_in', 0)} 秒后再检查")

    def _tick(self) -> None:
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        running = bool(self._pilot and self._pilot.isRunning())
        self.st_state.set("运行中" if running else "未启动",
                          UP if running else TEXT_DIM,
                          hint="到窗口自动执行" if running else "打开开关后常驻")
        if self.btn_auto.isChecked() != running:
            self.btn_auto.blockSignals(True)
            self.btn_auto.setChecked(running)
            self.btn_auto.setText("停止自动交易" if running else "启动自动交易")
            self.btn_auto.blockSignals(False)
        runs = self._runs_value()
        freq = "不限次" if runs <= 0 else (f"每天 {runs} 次" if runs > 1 else "每天 1 次")
        orders = self.max_orders.value()
        self.st_next.set(f"{self.win_start.text()}~{self.win_end.text()}",
                         hint=f"{freq} · 笔数{'不限' if orders <= 0 else orders}")
        text = self.strategy.currentText()
        self.st_strategy.set(text.split("·")[-1][:16], hint=text.split("·")[0])
        self._refresh_guard()

    def _refresh_guard(self) -> None:
        """把回撤闸与仓位系数的当前状态刷到卡片上。"""
        try:
            from ..engine.adaptive import AdaptiveSizer, DrawdownGuard
            acct = self.account.currentText() or "default"
            auto = self.auto_mode.isChecked()
            limit = self.max_dd.value() / 100
            if auto:
                # 全自动时阈值和动作都不看下拉:阈值取配置(没填按 10%),
                # 动作由 autopilot 的阶梯决定。这里必须按实际生效的口径显示,
                # 否则会拿一个被置灰的控件值骗人。
                try:
                    _v = float(self.cfg.get("autotrade.max_drawdown_pct", 0) or 0)
                except Exception:  # noqa: BLE001
                    _v = 0.0
                limit = _v if _v > 0 else 0.10
                action_text = "全自动阶梯(1×减半 / 1.5×停手 / 2×清仓)"
            else:
                action_text = self.dd_action.currentText()
            g = DrawdownGuard(acct)
            eq = 0.0
            try:
                eq = float(self.gw.account().get("total") or 0)
            except Exception:  # noqa: BLE001
                eq = 0.0
            if eq > 0:
                rep = g.peek(eq, limit, self.dd_action.currentData() or "stop_new")
                dd = float(rep.get("dd") or 0)
                trig = bool(rep.get("triggered"))
                tag = "全自动" if auto else ""
                if limit <= 0:
                    self.st_dd.set(f"{dd:.2%}", TEXT_DIM, "未启用(上限 0)")
                elif trig:
                    self.st_dd.set(f"{dd:.2%}", UP, f"已触发,上限 {limit:.1%}{tag}")
                elif dd >= limit * 0.7:
                    self.st_dd.set(f"{dd:.2%}", WARN, f"接近上限 {limit:.1%}{tag}")
                else:
                    self.st_dd.set(f"{dd:.2%}", None, f"上限 {limit:.1%}{tag}")
            else:
                self.st_dd.set("--", TEXT_DIM, "取不到账户权益")

            sz = AdaptiveSizer(self.strategy.currentData() or "", acct).summary()
            m = float(sz.get("multiplier") or 1.0)
            if not self.adaptive.isChecked():
                self.st_mult.set("1.00", TEXT_DIM, "动态调整未开启")
            else:
                col = UP if m > 1.0 else (DOWN if m < 1.0 else None)
                nm = int(sz.get("n_moved") or 0)
                if nm < 4:
                    # 没真正动过的样本不能算胜率,否则「一直空仓」会显示成 0% 胜率
                    self.st_mult.set(f"{m:.2f}", col, f"有效样本 {nm} 次,不足 4 次")
                else:
                    self.st_mult.set(f"{m:.2f}", col,
                                     f"有效样本 {nm} 次 · 胜率 {sz.get('win', 0):.0%}")
            self.guard_hint.setText(
                f"回撤上限 {limit:.1%} · 动作 {action_text} · "
                f"系数 {m:.2f}({sz.get('reason') or '无'})" if limit > 0
                else f"回撤控制未启用 · 系数 {m:.2f}")
        except Exception as e:  # noqa: BLE001
            self.guard_hint.setText(f"守卫状态刷新失败:{str(e)[:60]}")

    def _on_auto_mode(self, on: bool) -> None:
        """全自动:参数由 pilot 算,手填的几项置灰(不隐藏,让人看得见被接管了什么)。"""
        for w in (self.per_pct, self.stop_loss, self.take_profit, self.max_dd,
                  self.dd_action, self.adaptive, self.adapt_win):
            w.setEnabled(not on)
        hint = getattr(self, "auto_hint", None)
        if hint is None:      # _build 阶段就被 setChecked 触发时还没建好,跳过
            return
        if on:
            # 回撤基准:界面填 0 时全自动按默认 10% 执行,这里必须写出来,
            # 否则「最大回撤 0%(关)」和实际行为对不上
            dd = float(self.cfg.get("autotrade.max_drawdown_pct", 0) or 0)
            dd_txt = (f"{dd:.0%}" if dd > 0 else "10%(默认,界面填 0 时生效)")
            hint.setText(
                "全自动:仓位按个股日波动算(目标单票日波动 "
                f"{float(self.cfg.get('autotrade.risk_budget', 0.015)):.1%}),"
                f"止损 = ATR 倍数,止盈按盈亏比,回撤基准 {dd_txt} 分四档降杠杆")
        else:
            hint.setText("手动:仓位/止盈止损/回撤都用上面填的值")

    def _reset_guard(self) -> None:
        try:
            from ..engine.adaptive import AdaptiveSizer, DrawdownGuard
            acct = self.account.currentText() or "default"
            DrawdownGuard(acct).reset()
            self._refresh_guard()
            self.status.emit("回撤闸已重置(峰值归零,下次运行重新记)")
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"重置失败:{e}")

    # ------------------------------------------------------------ 日志
    def refresh_log(self) -> None:
        from ..data.store import store
        try:
            rows = store().signals(60)
        except Exception:
            rows = []
        rows = [r for r in rows if r.get("source") == "autotrader"]
        self.log_tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            import datetime as dt
            ts = dt.datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%m-%d %H:%M:%S")
            set_cell(self.log_tbl, i, 0, ts, align="left")
            set_cell(self.log_tbl, i, 1, str(r.get("name") or ""), align="left")
            reason = str(r.get("reason") or "")
            set_cell(self.log_tbl, i, 2, reason[:160], align="left")

    # ------------------------------------------------------------ 规则
    def load_rules_list(self) -> None:
        self.rule_list.clear()
        for r in rules_mod.list_rules():
            item = QListWidgetItem(f"{r['name']}  ·  {r['buy'][:34]}")
            item.setData(Qt.UserRole, r["name"])
            item.setToolTip(r.get("desc") or r["buy"])
            self.rule_list.addItem(item)
        self._reload_strategies()

    def _selected_rule(self):
        item = self.rule_list.currentItem()
        if not item:
            return None
        return rules_mod.get_rule(item.data(Qt.UserRole))

    def new_rule(self) -> None:
        dlg = RuleDialog(parent=self)
        if dlg.exec():
            self.load_rules_list()
            self.status.emit("策略已保存")

    def edit_rule(self) -> None:
        r = self._selected_rule()
        if not r:
            self.status.emit("先选中一条策略")
            return
        dlg = RuleDialog(r, parent=self)
        if dlg.exec():
            self.load_rules_list()
            self.status.emit("策略已更新")

    def del_rule(self) -> None:
        r = self._selected_rule()
        if not r:
            self.status.emit("先选中一条策略")
            return
        if QMessageBox.question(self, "删除策略", f"确定删除「{r['name']}」?") != QMessageBox.Yes:
            return
        rules_mod.delete_rule(r["name"])
        self.load_rules_list()
        self.status.emit("已删除")

    def open_research(self) -> None:
        dlg = ResearchDialog(self.gw, parent=self)
        if dlg.exec():
            self.load_rules_list()
            self.status.emit("研究结果已保存为策略")

    def open_reverse(self) -> None:
        dlg = ReverseDialog(parent=self)
        if dlg.exec():
            self.load_rules_list()
            self.status.emit("反推结论已存为策略")

    def open_import(self) -> None:
        dlg = ImportDialog(parent=self)
        if dlg.exec():
            self.load_rules_list()
            self.status.emit("导入的策略已保存")

    # ------------------------------------------------------------ 线程
    def _run(self, fn, on_ok, tag: str, on_err=None, on_done=None) -> None:
        r = TaskRunner(fn)
        r.succeeded.connect(on_ok)
        if on_err:
            r.failed.connect(on_err)
        else:
            r.failed.connect(lambda e: self.status.emit(f"{tag} 失败:{e}"))
        if on_done:
            r.finished.connect(on_done)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def stop(self) -> None:
        if self._pilot:
            try:
                self._pilot.stop()
            except Exception:
                pass
            self._pilot = None
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
