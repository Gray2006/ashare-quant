# -*- coding: utf-8 -*-
"""走势预判页:概率条 + 判定 + 因子贡献 + 原因 + 多周期 + 走查校验。

这一页只做两件事:把「未来 N 日涨/平/跌的概率」摆出来,把「为什么」摊开。
概率是历史实测频率折算的,走查命中率和基准也一并显示,不藏。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import forecast as fc
from ..engine.hub import TaskRunner
from ..utils import fmt_price
from .board import Board
from .common import Card, label, set_cell, table
from .theme import BORDER, DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP

VERDICT_COLOR = {"看多": UP, "偏多": "#f0883e", "中性": FLAT,
                 "偏空": "#58a6ff", "看空": DOWN}


class ProbBar(QWidget):
    """三段概率条:涨(红)/ 平(灰)/ 跌(绿)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._p = (0.34, 0.32, 0.34)
        self._labels = ("涨", "平", "跌")
        self.setMinimumHeight(46)
        self.setToolTip("涨 / 平 / 跌 的概率,宽度按概率分配")

    def set(self, up: float, flat: float, down: float) -> None:
        tot = max(up + flat + down, 1e-9)
        self._p = (up / tot, flat / tot, down / tot)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        rad = min(9.0, h / 2)
        cols = (QColor(UP), QColor(FLAT), QColor(DOWN))
        x = 0.0
        for i, frac in enumerate(self._p):
            seg = max(0.0, min(w - x, w * frac))
            if seg > 0.5:
                p.setPen(Qt.NoPen)
                p.setBrush(cols[i])
                p.drawRoundedRect(QRectF(x, 0, seg, h), rad, rad)
                txt = f"{self._labels[i]} {self._p[i] * 100:.0f}%"
                if seg > 58:
                    p.setPen(QColor("#ffffff"))
                    f = QFont(p.font())
                    f.setBold(True)
                    f.setPointSize(10)
                    p.setFont(f)
                    p.drawText(QRectF(x, 0, seg, h), Qt.AlignCenter, txt)
            x += seg
        if 0 < x < w:                      # 概率没填满时补齐底色,别留黑边
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(BORDER))
            p.drawRoundedRect(QRectF(x, 0, w - x, h), rad, rad)


class ForecastPage(QWidget):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rep: dict | None = None
        self._rows: list[dict] = []
        self._scan_kind = "index"
        self._build()
        QTimer.singleShot(900, self.refresh)

    # ------------------------------------------------------------ 构建
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        board = Board("forecast", cols=[1.0], rows=[0.078, 0.325, 0.315, 0.282])
        root.addWidget(board)

        # 控制条
        bar = QFrame()
        bar.setObjectName("Card")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 10, 16, 10)
        bl.setSpacing(10)

        bl.addWidget(label("标的", dim=True))
        self.target = QComboBox()
        self.target.setEditable(True)
        self.target.setMinimumWidth(210)
        self.target.setToolTip("选指数,或直接输 6 位代码(个股)")
        self._fill_targets()
        bl.addWidget(self.target)

        bl.addWidget(label("周期", dim=True))
        self.horizon = QComboBox()
        for h in fc.HORIZONS:
            self.horizon.addItem(f"{h} 个交易日", h)
        self.horizon.setCurrentIndex(fc.HORIZONS.index(fc.DEFAULT_HORIZON))
        self.horizon.setMinimumWidth(130)
        self.horizon.currentIndexChanged.connect(lambda _i: self.refresh())
        bl.addWidget(self.horizon)

        self.btn_run = QPushButton("开始预判")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.refresh)
        bl.addWidget(self.btn_run)

        self.btn_scan = QPushButton("扫自选")
        self.btn_scan.setToolTip("把自选股批量算一遍,按上涨概率排序")
        self.btn_scan.clicked.connect(self.scan_watchlist)
        bl.addWidget(self.btn_scan)

        self.btn_copy = QPushButton("复制报告")
        self.btn_copy.clicked.connect(self.copy_report)
        bl.addWidget(self.btn_copy)
        bl.addStretch(1)
        self.hint = QLabel("首次要拉 60 只日线建池子,约 30 秒;之后都走缓存")
        self.hint.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        bl.addWidget(self.hint)
        board.add(bar, 0, 0, min_h=56)

        # 第一行:概率 / 判定 / 校验
        top = Board("forecast.top", cols=[0.38, 0.26, 0.36], rows=[1.0])
        c_prob = Card("方向概率", hint="历史实测频率,不是拍脑袋")
        self.probbar = ProbBar()
        c_prob.add(self.probbar)
        self.lbl_probs = label("等数据", dim=True, size=12)
        self.lbl_probs.setWordWrap(True)
        c_prob.add(self.lbl_probs)
        top.add(c_prob, 0, 0, min_w=330, min_h=150)

        c_ver = Card("判定")
        self.lbl_verdict = QLabel("--")
        self.lbl_verdict.setStyleSheet(f"color:{TEXT};font-size:26px;font-weight:700;")
        c_ver.add(self.lbl_verdict)
        self.lbl_advice = label("", dim=True, size=12)
        self.lbl_advice.setWordWrap(True)
        c_ver.add(self.lbl_advice)
        self.lbl_conf = label("", dim=True, size=11)
        c_ver.add(self.lbl_conf)
        top.add(c_ver, 0, 1, min_w=210, min_h=150)

        c_bt = Card("走查校验", hint="只用当时之前的数据预测,不偷看未来")
        self.bt_tbl = table(["项目", "数值"], widths=[104, 82], stretch_col=1)
        c_bt.add(self.bt_tbl, 1)
        top.add(c_bt, 0, 2, min_w=230, min_h=150)
        board.add(top, 1, 0, min_h=150)

        # 第二行:因子贡献 / 原因
        mid = Board("forecast.mid", cols=[0.53, 0.47], rows=[1.0])
        c_fac = Card("因子贡献", hint="权重方向由历史相关系数决定,顺势还是逆势看数据")
        self.fac_tbl = table(["因子", "当前值", "权重", "贡献", "依据"],
                             widths=[64, 62, 56, 56, 330], stretch_col=4)
        c_fac.add(self.fac_tbl, 1)
        mid.add(c_fac, 0, 0, min_w=430, min_h=170)

        c_why = Card("原因")
        self.why_tbl = table(["方向", "内容"], widths=[46, 380], stretch_col=1)
        c_why.add(self.why_tbl, 1)
        mid.add(c_why, 0, 1, min_w=300, min_h=170)
        board.add(mid, 2, 0, min_h=170)

        # 第三行:多周期 / 相似状态 / 一览
        bot = Board("forecast.bot", cols=[0.34, 0.30, 0.36], rows=[1.0])
        c_h = Card("多周期概览", hint="同一套分数在不同持有期上的概率")
        self.h_tbl = table(["周期", "涨", "平", "跌", "中位", "横盘带"],
                           widths=[50, 48, 48, 48, 56, 62], stretch_col=None)
        c_h.add(self.h_tbl, 1)
        bot.add(c_h, 0, 0, min_w=280, min_h=160)

        c_sim = Card("相似历史状态", hint="这只标的自己在相近分数之后的结果")
        self.sim_tbl = table(["日期", "分数", "之后涨跌"], widths=[92, 66, 74])
        c_sim.add(self.sim_tbl, 1)
        bot.add(c_sim, 0, 1, min_w=230, min_h=160)

        c_pool = Card("池子一览", hint="双击一行看它自己的预判")
        self.pool_tbl = table(["代码", "名称", "现价", "涨概率", "判定"],
                              widths=[66, 92, 68, 66, 58])
        self.pool_tbl.cellDoubleClicked.connect(self._on_pool_double)
        c_pool.add(self.pool_tbl, 1)
        bot.add(c_pool, 0, 2, min_w=300, min_h=160)
        board.add(bot, 3, 0, min_h=160)

        # 行高收一点,让走查和分桶那些行不用滚动就能全看见
        # (bt_tbl 现在是最多 7 行,26 装不下,24 刚好)
        self.bt_tbl.verticalHeader().setDefaultSectionSize(24)
        self.pool_tbl.verticalHeader().setDefaultSectionSize(26)
        for t in (self.fac_tbl, self.h_tbl, self.sim_tbl):
            t.verticalHeader().setDefaultSectionSize(25)

    def _fill_targets(self) -> None:
        self.target.clear()
        for code, name in fc.INDEX_LIST:
            self.target.addItem(f"指数 · {name}", (code, "index"))
        for code in cfg().watchlist:
            self.target.addItem(f"个股 · {code}", (code, "stock"))
        self.target.setCurrentIndex(0)

    def _picked(self) -> tuple[str, str, str]:
        """返回 (代码, 类型, 名称)。手输的代码一律按个股处理。"""
        data = self.target.currentData()
        text = str(self.target.currentText() or "").strip()
        if isinstance(data, (tuple, list)) and len(data) == 2:
            code, kind = str(data[0]), str(data[1])
            if not text or code in text:
                name = fc.INDEX_NAMES.get(code, code) if kind == "index" else code
                return code, kind, name
        raw = "".join(ch for ch in text if ch.isdigit())
        code = raw[:6] if len(raw) >= 6 else ""
        if not code:
            code = cfg().watchlist[0] if cfg().watchlist else "600519"
        return code, "stock", code

    # ------------------------------------------------------------ 预判
    def refresh(self) -> None:
        code, kind, name = self._picked()
        try:
            h = int(self.horizon.currentData() or fc.DEFAULT_HORIZON)
        except Exception:
            h = fc.DEFAULT_HORIZON
        self.btn_run.setEnabled(False)
        self.hint.setText(f"取数中:{name}({code})·{h} 日…")
        self.status.emit(f"预判中:{code}")

        def job():
            rep = fc.forecast(code, horizon=h, kind=kind, name=name,
                              days=fc.PANEL_DAYS)
            rows = fc.market_rows(horizon=h)
            return {"rep": rep, "rows": rows}

        def ok(pack):
            self.btn_run.setEnabled(True)
            self._on_report(pack["rep"])
            self._scan_kind = "index"
            self._fill_rows(pack["rows"], "四个主要指数")
            self.hint.setText("指数默认不叠加修正;个股会叠加资金流与估值")

        def err(e):
            self.btn_run.setEnabled(True)
            self.hint.setText(str(e).splitlines()[0][:90])
            self.status.emit(f"预判失败:{str(e)[:60]}")

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def scan_watchlist(self) -> None:
        codes = list(cfg().watchlist)
        if not codes:
            return
        self.btn_scan.setEnabled(False)
        self.hint.setText(f"扫自选 {len(codes)} 只…")
        try:
            h = int(self.horizon.currentData() or fc.DEFAULT_HORIZON)
        except Exception:
            h = fc.DEFAULT_HORIZON

        def job():
            return fc.batch(codes, horizon=h)

        def ok(rows):
            self.btn_scan.setEnabled(True)
            self._scan_kind = "stock"
            self._fill_rows(rows, f"自选 {len(rows)} 只(按上涨概率排序)")
            self.hint.setText("自选扫描只出纯统计概率,不含资金/估值修正")

        def err(e):
            self.btn_scan.setEnabled(True)
            self.hint.setText(str(e)[:90])

        r = TaskRunner(job)
        r.succeeded.connect(ok)
        r.failed.connect(err)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    # ------------------------------------------------------------ 渲染
    def _on_report(self, rep: dict) -> None:
        self._rep = rep
        pa = rep["probs_adj"]
        self.probbar.set(pa["up"], pa["flat"], pa["down"])

        ci = rep["ci"]
        shift_txt = ""
        if rep.get("shift"):
            shift_txt = f",修正 {rep['shift']:+.2f} logit"
        self.lbl_probs.setText(
            f"涨 {pa['up'] * 100:.1f}% · 平 {pa['flat'] * 100:.1f}% · 跌 {pa['down'] * 100:.1f}%\n"
            f"横盘带 ±{rep['threshold'] * 100:.2f}%(±0.5σ)· 池化样本 {rep['samples']} 个 / "
            f"{rep['pool']} 只 · 区间 [{ci[0] * 100:.0f}%, {ci[1] * 100:.0f}%]{shift_txt}")

        self.lbl_verdict.setText(rep["verdict"])
        self.lbl_verdict.setStyleSheet(
            f"color:{VERDICT_COLOR.get(rep['verdict'], TEXT)};font-size:26px;font-weight:700;")
        self.lbl_advice.setText(rep["advice"])
        bt = rep.get("backtest") or {}
        self.lbl_conf.setText(
            f"置信 {rep['confidence']} · 分数 {rep['score']:+.2f}"
            f" · 现价 {fmt_price(rep['price'])}({rep['date']})")

        # 走查:命中率一定和基准并排显示,别只挑好看的数字
        rows = []
        if bt:
            rows = [
                ("走查样本", f"{bt['n']} 次", TEXT),
                ("三分类命中", f"{bt['hit'] * 100:.1f}%(多数类 {bt['hit_naive'] * 100:.1f}%)", TEXT),
                ("方向命中", f"{bt['dir_hit'] * 100:.1f}%(猜涨 {bt['dir_base'] * 100:.1f}%)",
                 UP if bt['dir_hit'] > bt['dir_base'] + 0.005 else TEXT_DIM),
                ("Brier 技能", f"{bt['skill'] * 100:+.2f}%", UP if bt['skill'] > 0 else DOWN),
                ("校准误差", f"{bt['calib_err'] * 100:.1f}%", TEXT),
            ]
            sm = bt.get("strong") or {}
            if sm.get("n"):
                better = sm["hit"] - sm["base"] >= 0.02
                rows.append((f"强信号(≥{sm['edge']:.2f})",
                             f"{sm['n']} 次 {sm['hit'] * 100:.1f}%(基率 {sm['base'] * 100:.1f}%)",
                             UP if better else TEXT_DIM))
            rows.append(("池化样本", f"{rep['samples']} / {rep['pool']} 只", TEXT_DIM))
        else:
            rows = [("走查样本", "不足", TEXT_DIM)]
        self.bt_tbl.setRowCount(len(rows))
        for i, (k, v, col) in enumerate(rows):
            set_cell(self.bt_tbl, i, 0, k, align="left")
            set_cell(self.bt_tbl, i, 1, v, col, align="left")

        # 因子
        facs = rep["factors"]
        self.fac_tbl.setRowCount(len(facs))
        for i, f in enumerate(facs):
            has = f.get("has", True)
            set_cell(self.fac_tbl, i, 0, f["name"], align="left", bold=True)
            if has:
                set_cell(self.fac_tbl, i, 1, f"{f['value']:+.2f}",
                         UP if f["value"] > 0 else (DOWN if f["value"] < 0 else TEXT))
            else:
                set_cell(self.fac_tbl, i, 1, "无数据", TEXT_MUT)
            w = f"{f['weight']:+.3f}" if abs(f["weight"]) > 1e-4 else "—"
            set_cell(self.fac_tbl, i, 2, w, TEXT_DIM)
            if has:
                set_cell(self.fac_tbl, i, 3, f"{f['contrib']:+.2f}",
                         UP if f["contrib"] > 0.02 else (DOWN if f["contrib"] < -0.02 else TEXT))
            else:
                set_cell(self.fac_tbl, i, 3, "—", TEXT_MUT)
            tag = "" if f["follow"] == "顺势" else f"【{f['follow']}】"
            set_cell(self.fac_tbl, i, 4, tag + f["detail"][:70],
                     TEXT_MUT if not has else TEXT, False, "left")

        # 原因
        why = [("看多", x, UP) for x in rep["reasons_up"]]
        why += [("看空", x, DOWN) for x in rep["reasons_down"]]
        if not why:
            why = [("提示", "暂无明显信号,概率接近历史基准", TEXT_DIM)]
        self.why_tbl.setRowCount(len(why))
        for i, (kind, text, col) in enumerate(why):
            set_cell(self.why_tbl, i, 0, kind, col, bold=True, align="left")
            set_cell(self.why_tbl, i, 1, str(text)[:150], align="left")

        # 多周期
        hs = rep.get("horizons") or []
        self.h_tbl.setRowCount(len(hs))
        for i, x in enumerate(hs):
            cur = x["h"] == rep["horizon"]
            set_cell(self.h_tbl, i, 0, f"{x['h']} 日", align="left", bold=cur)
            set_cell(self.h_tbl, i, 1, f"{x['up'] * 100:.0f}%", UP, bold=cur)
            set_cell(self.h_tbl, i, 2, f"{x['flat'] * 100:.0f}%", FLAT, bold=cur)
            set_cell(self.h_tbl, i, 3, f"{x['down'] * 100:.0f}%", DOWN, bold=cur)
            set_cell(self.h_tbl, i, 4, f"{x['p50'] * 100:+.1f}%", TEXT)
            set_cell(self.h_tbl, i, 5, f"±{x['thr'] * 100:.1f}%", TEXT_DIM)

        # 相似状态(只显示最近 5 条,再多要滚动,没必要)
        sims = (rep.get("similar") or [])[:5]
        self.sim_tbl.setRowCount(len(sims))
        for i, s in enumerate(sims):
            set_cell(self.sim_tbl, i, 0, s["date"], align="left")
            set_cell(self.sim_tbl, i, 1, f"{s['score']:+.2f}", TEXT_DIM)
            set_cell(self.sim_tbl, i, 2, f"{s['ret']:+.2f}%", UP if s["ret"] > 0 else DOWN)

        self.status.emit(
            f"{rep['name']} 未来 {rep['horizon']} 日:涨 {pa['up'] * 100:.0f}% / "
            f"平 {pa['flat'] * 100:.0f}% / 跌 {pa['down'] * 100:.0f}% · {rep['verdict']}")

    def _fill_rows(self, rows: list[dict], title: str) -> None:
        self._rows = rows
        self.pool_tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            up = r.get("up")
            set_cell(self.pool_tbl, i, 0, str(r.get("code", "")), align="left")
            set_cell(self.pool_tbl, i, 1, str(r.get("name", ""))[:8], align="left")
            set_cell(self.pool_tbl, i, 2, fmt_price(r.get("price")) if r.get("price") else "-")
            set_cell(self.pool_tbl, i, 3, f"{up * 100:.1f}%" if up is not None else "-",
                     UP if (up or 0) > 0.4 else (DOWN if up is not None and up < 0.3 else TEXT))
            v = str(r.get("verdict", ""))
            set_cell(self.pool_tbl, i, 4, v[:6], VERDICT_COLOR.get(v, TEXT), align="left")
        self.hint.setText(title)

    def _on_pool_double(self, row: int, _col: int) -> None:
        from .common import row_key
        code = row_key(self.pool_tbl, row)
        r = next((x for x in self._rows
                  if str(x.get("code") or "") == code), None)
        if r is not None:
            code = str(r.get("code") or "")
            if not code:
                return
            kind = "index" if code in fc.INDEX_NAMES and self._scan_kind == "index" else "stock"
            for i in range(self.target.count()):
                data = self.target.itemData(i)
                if isinstance(data, (tuple, list)) and str(data[0]) == code:
                    self.target.setCurrentIndex(i)
                    break
            else:
                self.target.setEditText(code)
            if kind == "index":
                self.target.setCurrentText(f"指数 · {fc.INDEX_NAMES.get(code, code)}")
            self.refresh()

    def copy_report(self) -> None:
        if not self._rep:
            return
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(fc.text(self._rep))
            self.status.emit("预判报告已复制到剪贴板")
            self.hint.setText("报告已复制")
        except Exception as e:  # noqa: BLE001
            self.hint.setText(f"复制失败:{e}")

    def stop(self) -> None:
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(800)
            except Exception:
                pass
