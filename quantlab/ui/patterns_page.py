# -*- coding: utf-8 -*-
"""形态页:左边是实测频率,右边是形态词典,底下是最近命中的形态。

这一页的立场:形态书给的是别人的样本。所以先在自己这块数据上数一遍
「出现过几次、之后 1/3/5/10 日涨了多少」,再和全样本基准比。
次数少于 10 的不下结论——那多半是噪声。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractScrollArea, QComboBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from ..data.kline import get_kline
from ..data.store import store
from ..engine import patterns
from ..engine.hub import TaskRunner
from .board import Board
from .common import Card, StatCard, label, pnl_color, set_cell, table
from .theme import DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP

BIAS_COLOR = {"看涨": UP, "看跌": DOWN, "中性": FLAT}


class PatternPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rep: dict | None = None
        self._bars: list[dict] = []
        self._codes: list[str] = []
        self._build()
        self.reload_codes()
        QTimer.singleShot(700, self.refresh)

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("pattern", cols=[1.0], rows=[0.12, 0.56, 0.32])
        root.addWidget(board)

        top = Board("pattern.top", cols=[0.24, 0.20, 0.24, 0.20, 0.12], rows=[1.0])
        self.st_bars = StatCard("样本", "--", "日 K 根数")
        self.st_hits = StatCard("出现过形态", "--", "次数 ≥1 的形态数")
        self.st_best = StatCard("最强超额", "--", "5 日上涨概率 − 基准")
        self.st_base = StatCard("基准 5 日", "--", "全样本随机一天的上涨占比")
        for i, s in enumerate((self.st_bars, self.st_hits, self.st_best, self.st_base)):
            top.add(s, 0, i, min_w=140, min_h=84)

        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.code = QComboBox()
        self.code.setEditable(True)
        self.code.setMinimumWidth(96)
        self.code.lineEdit().setPlaceholderText("代码")
        row.addWidget(self.code, 1)
        bl.addLayout(row)
        self.btn = QPushButton("重算")
        self.btn.setObjectName("Primary")
        self.btn.clicked.connect(self.refresh)
        bl.addWidget(self.btn)
        top.add(box, 0, 4, min_w=120, min_h=84)
        board.add(top, 0, 0, min_h=88)

        mid = Board("pattern.mid", cols=[0.58, 0.42], rows=[1.0])
        left = Card("形态实测", hint="次数 <10 的仅供参考")
        self.tbl = table(["形态", "方向", "次数", "1日", "3日", "5日", "10日", "5日超额"],
                         widths=[104, 48, 52, 58, 58, 58, 58, 78], stretch_col=0)
        self.tbl.setObjectName("PatternTable")
        # 表格行数会随数据变,但版面不能被行数牵着走:让 sizeHint 恒定,
        # 超出就滚动。否则命中多的时候底部会抢走上面的高度。
        self.tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tbl.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        self.tbl.cellClicked.connect(self._on_row)
        left.add(self.tbl, 1)
        self.hint = label("选一只股票点「重算」", dim=True, size=11)
        left.add(self.hint)
        mid.add(left, 0, 0, min_w=480, min_h=200)

        right = Card("形态词典")
        self.dict_pick = QComboBox()
        for p in patterns.PATTERNS:
            self.dict_pick.addItem(f"{p['name']} · {p['bias']}", p["key"])
        self.dict_pick.currentIndexChanged.connect(self._on_pick)
        right.add(self.dict_pick)
        self.dict_txt = QPlainTextEdit()
        self.dict_txt.setReadOnly(True)
        right.add(self.dict_txt, 1)
        mid.add(right, 0, 1, min_w=280, min_h=200)
        board.add(mid, 1, 0, min_h=220)

        bot = Board("pattern.bot", cols=[0.46, 0.54], rows=[1.0])
        rec = Card("最近命中", hint="最近 15 根 K 线内触发的形态")
        self.rec_tbl = table(["日期", "形态", "方向", "收盘"],
                             widths=[92, 110, 56, 76], stretch_col=1)
        self.rec_tbl.setObjectName("PatternRecent")
        self.rec_tbl.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.rec_tbl.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)
        rec.add(self.rec_tbl, 1)
        bot.add(rec, 0, 0, min_w=380, min_h=150)

        how = Card("这些数字怎么读")
        self.how = QPlainTextEdit()
        self.how.setReadOnly(True)
        self.how.setPlainText(_HOWTO)
        how.add(self.how, 1)
        bot.add(how, 0, 1, min_w=360, min_h=150)
        board.add(bot, 2, 0, min_h=150)

    # ------------------------------------------------------------ 数据
    def reload_codes(self) -> None:
        cur = self.code.currentText().strip()
        codes = [w["code"] for w in store().get_watchlist()] or ["600519", "000001"]
        self._codes = codes
        self.code.blockSignals(True)
        self.code.clear()
        for c in codes:
            self.code.addItem(c)
        if cur:
            self.code.setCurrentText(cur)
        self.code.blockSignals(False)
        if not cur:
            self.code.setCurrentText(codes[0])

    def refresh(self) -> None:
        code = (self.code.currentText() or "").strip()
        if not code:
            self.status.emit("先填一个代码")
            return
        self.btn.setEnabled(False)
        self.hint.setText(f"取 {code} 的日 K 并统计形态…")

        def job():
            bars, src = get_kline(code, 101, 300)
            if not bars:
                raise RuntimeError(f"取不到 {code} 的 K 线({src})")
            return bars, src, patterns.measure(bars), patterns.scan(bars, 15)

        def ok(res):
            bars, src, rep, hits = res
            self.btn.setEnabled(True)
            self._bars, self._rep = bars, rep
            self._render(rep, hits, code, src)

        def err(e):
            self.btn.setEnabled(True)
            self.hint.setText(f"失败:{e}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        r.finished.connect(lambda: self.btn.setEnabled(True))
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def refresh_now(self) -> None:
        self.refresh()

    def _render(self, rep: dict, hits: list[dict], code: str, src: str) -> None:
        if not rep.get("ok"):
            self.hint.setText(rep.get("reason") or "样本不足")
            return
        hzs = rep["horizons"]
        n_hit = sum(1 for r in rep["rows"] if r["n"])
        self.st_bars.set(f"{rep['bars']}", hint=f"{rep['from'][:10]} ~ {rep['to'][:10]}")
        self.st_hits.set(f"{n_hit}", hint=f"共 {len(rep['rows'])} 个可识别")

        best, best_edge = None, None
        for r in rep["rows"]:
            e = patterns.edge(r, hzs[-2], rep["base"])   # 默认看 5 日
            if r["n"] >= 10 and e is not None and (best_edge is None or e > best_edge):
                best, best_edge = r, e
        if best is not None:
            self.st_best.set(f"{best['name']} {best_edge:+.0f}pp",
                             pnl_color(best_edge) if best_edge else FLAT,
                             hint=f"{best['n']} 次样本")
        else:
            self.st_best.set("--", hint="没有样本 ≥10 的形态")
        b5 = rep["base"][hzs[-2]]["up"]
        self.st_base.set(f"{b5 * 100:.1f}%" if b5 is not None else "--",
                         hint=f"{rep['base'][hzs[-2]]['n']} 个观测")

        rows = sorted([r for r in rep["rows"] if r["n"]],
                      key=lambda r: (-r["n"], r["name"]))
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            col = BIAS_COLOR.get(r["bias"], TEXT)
            set_cell(self.tbl, i, 0, r["name"], col, bold=True, align="left")
            set_cell(self.tbl, i, 1, r["bias"], col)
            set_cell(self.tbl, i, 2, str(r["n"]))
            for j, hz in enumerate(hzs[:4]):
                v = r["h"].get(hz)
                pct = v["up"] * 100 if v else 0
                set_cell(self.tbl, i, 3 + j, f"{pct:.0f}%",
                         UP if pct >= 55 else (DOWN if pct <= 45 else TEXT_DIM))
            e = patterns.edge(r, hzs[-2], rep["base"])
            txt = "—" if e is None or r["n"] < 10 else f"{e:+.0f}pp"
            set_cell(self.tbl, i, 7, txt,
                     (UP if e > 0 else DOWN) if (e is not None and r["n"] >= 10) else TEXT_MUT)

        self.rec_tbl.setRowCount(len(hits))
        for i, h in enumerate(hits[:60]):
            set_cell(self.rec_tbl, i, 0, h["date"][5:10], align="left")
            set_cell(self.rec_tbl, i, 1, h["name"], BIAS_COLOR.get(h["bias"], TEXT),
                     bold=True, align="left")
            set_cell(self.rec_tbl, i, 2, h["bias"], BIAS_COLOR.get(h["bias"], TEXT))
            set_cell(self.rec_tbl, i, 3, f"{h['close']:.2f}")

        if self.dict_pick.count() and not self.dict_txt.toPlainText():
            self._on_pick(0)
        self.hint.setText(f"{code} · 来源 {src} · {rep['bars']} 根 · "
                          f"命中 {len(hits)} 处")
        self.status.emit(f"{code} 形态统计完成")

    def _on_row(self, row: int, _col: int) -> None:
        item = self.tbl.item(row, 0)
        if item is None:
            return
        key = next((p["key"] for p in patterns.PATTERNS if p["name"] == item.text()), None)
        if key:
            idx = self.dict_pick.findData(key)
            if idx >= 0:
                self.dict_pick.setCurrentIndex(idx)

    def _on_pick(self, _i: int) -> None:
        key = self.dict_pick.currentData()
        if key:
            self.dict_txt.setPlainText(patterns.describe(key))

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass


_HOWTO = """三个数要一起看,单看一个会误判。

1) 次数 —— 样本量。少于 10 次的形态,胜率掉到 30% 也可能只是运气。
   次数多但胜率贴着基准的,说明这条判据在你的市场上没有信息量。

2) 上涨占比 —— 出现该形态后 h 日的收盘价高于当天的比例。
   它不等于「赚钱概率」:没有扣手续费、没有考虑滑点、也没算你
   能不能在信号出现当天买到。

3) 5 日超额(pp)—— 形态的上涨占比减去全样本基准。这是最有用的
   一列:基准本身就有 45~55%,只看「60% 上涨」会高估形态。
   超额长期为正、且样本足够,才谈得上「这个形态有边缘」。

别做的事:拿一个超额 +20pp、样本只有 7 次的形态去重仓。
那是把噪声当规律,也是量化里最常见的亏钱方式。
"""
