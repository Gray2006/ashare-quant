# -*- coding: utf-8 -*-
"""涨幅预选页:每天收盘后扫一遍全市场,给几只「接下来几天可能涨得多一点」的票,
并把这一批连同因子贡献一起落档、回传、到期回填。

页面故意把「beta 超额」和「中性化超额」并排放:
memory/quantlab-alpha-verdict.md 的结论是这套因子在样本外的优势来自 beta 暴露,
所以不给中性化数字就等于骗人。判读一栏由 engine.alpha_pick 算,页面只显示。
"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ..config import cfg
from ..engine import alpha_pick as ap
from ..engine import pick_strategy as ps
from ..engine.hub import TaskRunner
from ..utils import fmt_amount, fmt_pct, pct_color
from .board import Board
from .common import Card, StatCard, label, set_cell, table
from .theme import DOWN, FLAT, TEXT, TEXT_DIM, TEXT_MUT, UP, WARN

HORIZONS = [1, 3, 5, 10, 20]
TOPS = [3, 5, 10, 15, 20, 30, 50]
STRATEGIES = [("steady", {"label": "稳健"}), ("attack", {"label": "进攻"})]


class PickPage(QWidget):
    """每日涨幅预选 + 学习回传。"""

    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRunner] = []
        self._rep: dict = {}
        self._build()
        QTimer.singleShot(900, self.refresh)

    # ------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        board = Board("pick", cols=[1.0], rows=[0.13, 0.52, 0.35])
        root.addWidget(board)

        # ---- 顶栏:关键数字 + 操作
        top = Board("pick.top", cols=[0.11, 0.11, 0.11, 0.12, 0.11, 0.11, 0.16, 0.17],
                    rows=[1.0])
        self.st_day = StatCard("数据日", "--", "本地最后一根")
        self.st_pool = StatCard("池子", "--", "成交额前 N")
        self.st_exc = StatCard("超额", "--", "同分位 · 原始分")
        self.st_neu = StatCard("中性化超额", "--", "剔除 beta 之后")
        self.st_up = StatCard("上行频率", "--", "同分位历史")
        self.st_fac = StatCard("生效因子", "--", "|t|≥1 且 |IC|≥0.004")
        self.st_verdict = StatCard("判读", "--", "这批能不能信")
        for i, s in enumerate((self.st_day, self.st_pool, self.st_exc, self.st_neu,
                               self.st_up, self.st_fac, self.st_verdict)):
            top.add(s, 0, i, min_w=104, min_h=80)

        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(5)
        row = QHBoxLayout()
        row.setSpacing(5)
        self.prof = QComboBox()
        for k, pf in STRATEGIES:
            self.prof.addItem(pf["label"], k)
        self.prof.setToolTip(
            "稳健:低波动抗跌,冲 2%,拿 5~10 日\n"
            "进攻:高波动冲 5%,但跌 5% 的概率也一起放大 —— 两个概率都摆出来")
        self.prof.currentIndexChanged.connect(lambda *_: self.refresh())
        row.addWidget(self.prof)
        self.hz = QComboBox()
        for h in HORIZONS:
            self.hz.addItem(f"{h} 日", h)
        self.hz.setCurrentIndex(HORIZONS.index(5))
        self.hz.setToolTip("往后看几个交易日判对错")
        row.addWidget(self.hz)
        self.topn = QComboBox()
        for n in TOPS:
            self.topn.addItem(f"{n} 只", n)
        self.topn.setCurrentIndex(TOPS.index(5))
        self.topn.setToolTip("要几只候选。数量越多,后面那些的分数越靠近,越像随手挑的")
        row.addWidget(self.topn)
        self.onlyu = QComboBox()
        self.onlyu.addItem("全部", False)
        self.onlyu.addItem("只要今天涨的", True)
        self.onlyu.setToolTip(
            "这套排序讲的是「谁比谁强」,不看大盘涨跌。\n"
            "普跌那天排在前面的,往往就是跌得少的票(看着像看跌,其实是抗跌)。\n"
            "勾这个只留当日上涨的")
        row.addWidget(self.onlyu)
        bl.addLayout(row)
        self.btn_scan = QPushButton("重扫并记录")
        self.btn_scan.setObjectName("Primary")
        self.btn_scan.setToolTip("扫一遍全市场 → 落档 pick_runs → 每只票记进预测表并回传")
        self.btn_scan.clicked.connect(self.scan_and_record)
        self.btn_show = QPushButton("只看不记")
        self.btn_show.setToolTip("只扫一遍看看结果,不落档也不回传")
        self.btn_show.clicked.connect(self.refresh)
        self.btn_review = QPushButton("回填到期")
        self.btn_review.setToolTip(
            "次日收盘验证:用下一个交易日的**收盘价**判对错并回传。\n"
            "同时按验证结果微调该档的门槛(样本不足 40 条不动)")
        self.btn_review.clicked.connect(self.do_verify)
        self.btn_verify = self.btn_review
        bl.addWidget(self.btn_scan)
        bl.addWidget(self.btn_show)
        bl.addWidget(self.btn_review)
        top.add(box, 0, 7, min_w=120, min_h=80)
        board.add(top, 0, 0, min_h=84)

        # ---- 中部:候选表 + 因子表
        mid = Board("pick.mid", cols=[0.70, 0.30], rows=[1.0])
        cand = Card("今日候选", hint="稳健=低波动抗跌 / 进攻=高波动冲 5% · 两个档都摆出亏损概率")
        self.tbl = table(["#", "代码", "名称", "现价", "今日", "5日", "成交额",
                          "中性分", "原始分", "Beta", "预期涨跌", "超额", "主要因子"],
                         widths=[26, 66, 76, 62, 58, 58, 74, 60, 60, 46, 74, 66, 160],
                         stretch_col=12)
        cand.add(self.tbl, 1)
        self.cand_msg = label("", dim=True, size=11)
        self.cand_msg.setWordWrap(True)
        cand.add(self.cand_msg)
        mid.add(cand, 0, 0, min_w=560, min_h=260)

        right = Board("pick.right", cols=[1.0], rows=[0.56, 0.44])
        fac = Card("当期因子权重", hint="走查 RankIC · 负号=反向用")
        self.fac_tbl = table(["因子", "IC", "t", "权重"],
                             widths=[96, 66, 52, 60], stretch_col=0)
        fac.add(self.fac_tbl, 1)
        right.add(fac, 0, 0, min_w=300, min_h=160)
        note = Card("怎么读这一页")
        self.note = QPlainTextEdit()
        self.note.setReadOnly(True)
        note.add(self.note, 1)
        right.add(note, 1, 0, min_w=300, min_h=140)
        mid.add(right, 0, 1, min_w=300, min_h=260)
        board.add(mid, 1, 0, min_h=260)

        # ---- 底部:持有期实测 + 历史批次 + 学习回传
        bot = Board("pick.bot", cols=[0.34, 0.33, 0.33], rows=[1.0])
        hold = Card("拿几天划算", hint="扣掉手续费之后 · 历史同分位实测")
        self.hold_tbl = table(["持有", "样本", "毛涨幅", "扣费后", "盈利概率", "超额"],
                              widths=[44, 44, 62, 62, 66, 58], stretch_col=2)
        hold.add(self.hold_tbl, 1)
        self.hold_msg = label("", dim=True, size=11)
        self.hold_msg.setWordWrap(True)
        hold.add(self.hold_msg)
        bot.add(hold, 0, 0, min_w=310, min_h=150)

        runs = Card("历史批次", hint="每次「重扫并记录」一行")
        self.run_tbl = table(["时间", "数据日", "池子", "超额", "t", "中性化 t", "判读"],
                             widths=[92, 82, 52, 62, 46, 66, 150], stretch_col=6)
        runs.add(self.run_tbl, 1)
        bot.add(runs, 0, 1, min_w=380, min_h=150)

        lrn = Card("次日收盘验证", hint="下一交易日收盘价判对错 · 结果反哺调参")
        self.lrn_tbl = table(["日期", "代码", "次日收益", "达标", "胜", "最高", "最低"],
                             widths=[80, 66, 68, 46, 40, 62, 62], stretch_col=2)
        lrn.add(self.lrn_tbl, 1)
        self.lrn_msg = label("", dim=True, size=11)
        self.lrn_msg.setWordWrap(True)
        lrn.add(self.lrn_msg)
        bot.add(lrn, 0, 2, min_w=380, min_h=150)
        board.add(bot, 2, 0, min_h=150)

    # ------------------------------------------------------------ 任务
    def _busy(self) -> bool:
        # 先把已经不跑了的清掉。失败/正常结束的 TaskRunner 会留在列表里,
        # 不清理的话 _busy() 永远为真,页面就再也点不动了。
        self._tasks = [t for t in self._tasks if t.isRunning()]
        return bool(self._tasks)

    def _run(self, fn, ok, btn=None) -> None:
        # 上一轮还在跑就拒绝再来一次。以前只是把按钮灰掉,
        # 但灰掉的是「这一个」按钮,别的按钮照样能点,点了就把这次结果吞掉。
        if self._busy():
            self.status.emit("上一次还没跑完,等它结束再点")
            return
        btns = [b for b in (self.btn_show, self.btn_scan, self.btn_review) if b]
        for b in btns:
            b.setEnabled(False)

        def done(*a):
            for b in btns:
                b.setEnabled(True)
            # 跑完立刻把已结束的清掉,别等下一次 _busy() 才发现
            self._tasks = [t for t in self._tasks if t.isRunning()]

        r = TaskRunner(fn)
        r.succeeded.connect(ok)
        r.failed.connect(self._err)
        r.finished.connect(done)
        self._tasks = [t for t in self._tasks if t.isRunning()]
        self._tasks.append(r)
        r.start()

    def _err(self, msg: str) -> None:
        self.cand_msg.setText(f"失败:{str(msg)[:160]}")
        self.status.emit(f"涨幅预选失败:{str(msg)[:80]}")

    def _horizon(self) -> int:
        return int(self.hz.currentData() or 5)

    def _profile(self) -> str:
        return str(self.prof.currentData() or "steady")

    def _top(self) -> int:
        return int(self.topn.currentData() or 5)

    def _only_up(self) -> bool:
        return bool(self.onlyu.currentData())

    def _account(self) -> str:
        return str(cfg().get("autotrade.account", "default") or "default")

    def refresh(self) -> None:
        h, n, up = self._horizon(), self._top(), self._only_up()
        prof = self._profile()

        def job():
            # 公开版只做本地扫描:行情/选股全部在本机算,不连任何云端。
            rep = ps.scan_profile(prof, days=ps.ap.DAYS, top=n,
                                  max_stocks=ps.ap.MAX_STOCKS)
            # 用户在页面上选了持有期,以页面为准(策略档只提供默认值)
            if rep.get("ok") and h != rep.get("horizon"):
                rep = ap.scan(horizon=h, top=n, only_up=up)
            src = "local"
            runs = ap.recent_runs(limit=12)
            lrn = ap.learn_stats(self._account()) if rep.get("ok") else {}
            # 持有期实测要单独算一遍因子,耗时和 scan 同量级;
            # 算失败也不能把整页拖死,所以单独兜住,给空表而不是抛。
            try:
                hold = ap.hold_report()
            except Exception:  # noqa: BLE001
                hold = {}
            try:
                vst = ps.verify_stats(prof)
            except Exception:  # noqa: BLE001
                vst = {}
            return {"rep": rep, "runs": runs, "learn": lrn, "src": src,
                    "hold": hold, "vst": vst, "profile": prof}

        self._run(job, self._render, self.btn_show)

    def scan_and_record(self) -> None:
        h, n, up = self._horizon(), self._top(), self._only_up()
        prof = self._profile()

        def job():
            rep = ps.scan_profile(prof, top=n)
            # 落档时把档位与目标写进 note,次日验证靠它认出是哪一套策略
            note = f"{prof}|tgt={ps.PROFILES[prof]['target']:.2f}|手动重扫"
            out = {"rep": rep, "saved": 0, "rec": {}, "profile": prof}
            if rep.get("ok"):
                out["saved"] = ap.save_run(rep, note=note)
                out["rec"] = ps.record_profile(rep, account=self._account())
            out["runs"] = ap.recent_runs(limit=12)
            out["learn"] = ap.learn_stats(self._account()) if rep.get("ok") else {}
            try:
                out["hold"] = ap.hold_report()
            except Exception:  # noqa: BLE001
                out["hold"] = {}
            try:
                out["vst"] = ps.verify_stats(prof)
            except Exception:  # noqa: BLE001
                out["vst"] = {}
            return out

        self._run(job, self._render, self.btn_scan)

    def do_verify(self) -> None:
        """次日收盘验证 + 参数反哺。这两件事一起做,验证完立刻调参。"""
        prof = self._profile()
        acc = self._account()

        def job():
            lr = ps.verify_next_day(account=acc)
            tn = ps.tune(prof)
            vst = ps.verify_stats(prof)
            runs = ap.recent_runs(limit=12)
            lrn = ap.learn_stats(acc)
            return {"rep": self._rep or {"ok": False}, "runs": runs, "learn": lrn,
                    "verify": {"local": lr}, "tune": tn, "vst": vst,
                    "profile": prof}

        self._run(job, self._render, self.btn_verify)

    def do_review(self) -> None:
        acc = self._account()

        def job():
            res = ap.review(account=acc)
            runs = ap.recent_runs(limit=12)
            lrn = ap.learn_stats(acc)
            rep = self._rep or {"ok": False}
            return {"rep": rep, "runs": runs, "learn": lrn, "review": res}

        self._run(job, self._render, self.btn_review)

    # ------------------------------------------------------------ 渲染
    def _render(self, d: dict) -> None:
        rep = d.get("rep") or {}
        if rep.get("ok"):
            self._rep = rep
        self._render_rep(self._rep)
        self._render_hold(d.get("hold") or {})
        self._render_verify(d.get("vst") or {})
        self._render_runs(d.get("runs") or [])
        self._render_learn(d.get("learn") or {})
        rec = d.get("rec") or {}
        rev = d.get("review") or {}
        bits = []
        if d.get("saved"):
            bits.append(f"批次 #{d['saved']} 已落档")
        if rec.get("ids"):
            bits.append(f"{len(rec['ids'])} 条预测已记录并回传")
        if rec.get("failed"):
            bits.append(f"{len(rec['failed'])} 条记录失败")
        if rev:
            bits.append(f"回填 {rev.get('reviewed', 0)}/{rev.get('due', 0)} 条")
        if bits:
            self.status.emit(" · ".join(bits))
        elif self._rep.get("ok"):
            self.status.emit(f"涨幅预选:{len(self._rep.get('rows') or [])} 只候选")

    def _render_rep(self, rep: dict) -> None:
        if not rep:
            return
        if not rep.get("ok"):
            self.cand_msg.setText(str(rep.get("why") or "没有结果"))
            self.st_verdict.set("—", FLAT, hint=str(rep.get("why") or "")[:30])
            return
        self.st_day.set(str(rep.get("day") or "--"), None,
                        hint=(f"{rep.get('elapsed', 0)} 秒"))
        self.st_pool.set(str(rep.get("universe") or 0), None,
                         hint=f"可买 {rep.get('n_cand', 0)} 只")
        ex = float(rep.get("excess") or 0)
        self.st_exc.set(f"{ex:+.2%}", UP if ex > 0 else DOWN,
                        hint=f"t={rep.get('excess_t', 0):+.2f}")
        neu = float(rep.get("excess_neu") or 0)
        nt = float(rep.get("excess_neu_t") or 0)
        self.st_neu.set(f"{neu:+.2%}", UP if neu > 0 and nt > 0 else DOWN,
                        hint=f"t={nt:+.2f}")
        pu = float(rep.get("prob_up") or 0)
        self.st_up.set(f"{pu:.0%}", UP if pu >= 0.5 else DOWN, hint="同分位为正的比例")
        self.st_fac.set(str(rep.get("n_factors") or 0), None,
                        hint=f"{rep.get('n_factors', 0)} 个因子给权重")
        v = str(rep.get("verdict") or "")
        good = "仍有正超额" in v
        self.st_verdict.set(v[:10] or "--", UP if good else (WARN if "beta" in v else DOWN),
                            hint=v)

        rows = rep.get("rows") or []
        cloud = False  # 公开版只有本地扫描
        self.tbl.setRowCount(len(rows))
        for i, r in enumerate(rows):
            set_cell(self.tbl, i, 0, str(i + 1), TEXT_MUT)
            set_cell(self.tbl, i, 1, str(r.get("code") or ""), TEXT, align="left")
            set_cell(self.tbl, i, 2, str(r.get("name") or "—"), TEXT, align="left")
            # 云端没存现价/当日涨幅/5 日涨幅:显示 — 而不是 0.00,0 会被当成真数据
            px = r.get("price")
            set_cell(self.tbl, i, 3, f"{float(px):.2f}" if px else "—")
            chg = r.get("chg")
            set_cell(self.tbl, i, 4, fmt_pct((chg or 0) * 100 if chg is not None else None),
                     pct_color(chg))
            r5 = r.get("ret5")
            set_cell(self.tbl, i, 5, fmt_pct((r5 or 0) * 100 if r5 is not None else None),
                     pct_color(r5))
            set_cell(self.tbl, i, 6, fmt_amount(r.get("amount")), TEXT_DIM)
            sc = r.get("score")
            set_cell(self.tbl, i, 7, f"{float(sc):.3f}" if sc is not None else "—",
                     UP if (sc or 0) > 0 else DOWN, bold=True)
            sr = r.get("score_raw")
            set_cell(self.tbl, i, 8, f"{float(sr):.3f}" if sr is not None else "—", TEXT_DIM)
            bt = r.get("beta")
            set_cell(self.tbl, i, 9, f"{float(bt):.2f}" if bt is not None else "—", TEXT_DIM)
            # 预期涨跌(绝对)和超额(相对基准)是两件事,分列显示
            ea = r.get("exp_abs")
            if ea is None:
                ea = rep.get("exp_abs")
            set_cell(self.tbl, i, 10, f"{float(ea):+.2%}" if ea is not None else "—",
                     UP if (ea or 0) > 0 else DOWN, bold=True)
            set_cell(self.tbl, i, 11, f"{float(r.get('exp') or 0):+.2%}",
                     UP if float(r.get("exp") or 0) > 0 else DOWN)
            fnames = " · ".join(str(f.get("name") or "") for f in (r.get("factors") or []))
            set_cell(self.tbl, i, 12, fnames or "—", TEXT_DIM, align="left")

        src = "云端批次" if cloud else "本地实时扫描"
        ea = rep.get("exp_abs")
        bm = rep.get("bench")
        self.cand_msg.setText(
            f"{len(rows)} 只 · 来源 {src} · 数据日 {rep.get('day')} · "
            f"看 {rep.get('horizon')} 个交易日 · "
            f"这批同分位历史平均 {float(ea or 0):+.2%}"
            + (f",同期全市场 {float(bm or 0):+.2%}" if bm is not None else "")
            + " · 「预期涨跌」是绝对口径,「超额」是减掉全市场之后,两个别混看"
            + ("(云端批次不含现价,按「重扫并记录」在本地算)" if cloud else ""))

        ics = rep.get("ic") or {}
        wts = rep.get("weights") or {}
        keys = sorted(ics, key=lambda k: -abs(float(ics[k].get("ic") or 0)))
        self.fac_tbl.setRowCount(len(keys))
        for i, k in enumerate(keys):
            st = ics[k]
            wt = float(wts.get(k) or 0)
            ic = float(st.get("ic") or 0)
            set_cell(self.fac_tbl, i, 0, ap.FACTOR_LABEL.get(k, k), TEXT, align="left")
            set_cell(self.fac_tbl, i, 1, f"{ic:+.4f}", UP if ic > 0 else DOWN)
            set_cell(self.fac_tbl, i, 2, f"{float(st.get('t') or 0):+.2f}",
                     TEXT_MUT if abs(float(st.get("t") or 0)) < 2 else TEXT)
            set_cell(self.fac_tbl, i, 3, f"{wt:+.3f}" if wt else "—",
                     TEXT if wt else TEXT_MUT)

        self.note.setPlainText(_readme(rep))

    def _render_hold(self, hr: dict) -> None:
        """持有期实测表:一行一个持有天数,最优那行标出来。"""
        rows = (hr or {}).get("table") or []
        self.hold_tbl.setRowCount(len(rows))
        best = (hr or {}).get("best") or {}
        best_h = best.get("hold")
        for i, r in enumerate(rows):
            h = int(r.get("hold") or 0)
            is_best = (h == best_h)
            net = float(r.get("net") or 0)
            set_cell(self.hold_tbl, i, 0, f"{h} 日" + (" ★" if is_best else ""),
                     TEXT if is_best else TEXT_MUT, bold=is_best)
            set_cell(self.hold_tbl, i, 1, str(r.get("n") or 0), TEXT_DIM)
            set_cell(self.hold_tbl, i, 2, f"{float(r.get('gross') or 0):+.2%}",
                     UP if float(r.get('gross') or 0) > 0 else DOWN)
            set_cell(self.hold_tbl, i, 3, f"{net:+.2%}",
                     UP if net > 0 else DOWN, bold=is_best)
            wn = float(r.get("win_net") or 0)
            set_cell(self.hold_tbl, i, 4, f"{wn:.0%}",
                     UP if wn >= 0.5 else DOWN)
            set_cell(self.hold_tbl, i, 5, f"{float(r.get('excess') or 0):+.2%}",
                     TEXT_DIM)
        if not rows:
            self.hold_msg.setText("持有期还没算出来(点「只看不记」重算)")
            return
        t1 = (hr or {}).get("t1") or {}
        best_txt = ""
        if best:
            best_txt = (f"最长划算的是 {best.get('hold')} 日:扣费后 "
                        f"{float(best.get('net') or 0):+.2%},"
                        f"盈利概率 {float(best.get('win_net') or 0):.0%}")
        self.hold_msg.setText(
            (f"{best_txt}。" if best_txt else "")
            + f"次日卖出扣费后 {float(t1.get('net') or 0):+.2%}"
            + ("(能赚)" if (hr or {}).get("t1_ok") else "(盖不住手续费,别隔天卖)")
            + "。手续费按来回 0.16% 算。")

    def _render_verify(self, vst: dict) -> None:
        """次日收盘验证的结果:胜率 / 命中率 / 均值。没有就说没有。"""
        n = int((vst or {}).get("n") or 0)
        if not n:
            self.lrn_msg.setText(
                "次日验证:还没有可验证的样本。收盘后点「回填到期」,"
                "用下一个交易日的收盘价判对错。")
            self.lrn_tbl.setRowCount(0)
            return
        self.lrn_msg.setText(
            f"次日验证 {n} 条:收盘胜率 {float(vst.get('win') or 0):.0%}"
            f" · 达标 {float(vst.get('hit') or 0):.0%}"
            f" · 平均 {float(vst.get('mean') or 0):+.2%}"
            f"(扣费 {float(vst.get('net') or 0):+.2%})"
            f" · 最好 {float(vst.get('best') or 0):+.2%}"
            f" / 最差 {float(vst.get('worst') or 0):+.2%}")
        # 逐日汇总:一天一行,这是「反哺」看的原始材料
        by = (vst or {}).get("by_day") or []
        self.lrn_tbl.setRowCount(len(by))
        for i, r in enumerate(by):
            set_cell(self.lrn_tbl, i, 0, str(r.get("day") or ""), TEXT_DIM, align="left")
            set_cell(self.lrn_tbl, i, 1, f"{int(r.get('n') or 0)} 条", TEXT_DIM)
            m = float(r.get("mean") or 0)
            set_cell(self.lrn_tbl, i, 2, f"{m:+.2%}", UP if m > 0 else DOWN)
            w = float(r.get("win") or 0)
            set_cell(self.lrn_tbl, i, 3, f"{w:.0%}", UP if w >= 0.5 else DOWN)
            set_cell(self.lrn_tbl, i, 4, "胜" if w >= 0.5 else "负",
                     UP if w >= 0.5 else DOWN)
            set_cell(self.lrn_tbl, i, 5, "—", TEXT_MUT)
            set_cell(self.lrn_tbl, i, 6, "—", TEXT_MUT)

    def _render_runs(self, runs: list[dict]) -> None:
        self.run_tbl.setRowCount(len(runs))
        for i, r in enumerate(runs):
            import datetime as dt
            ts = float(r.get("ts") or 0)
            t = dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "—"
            set_cell(self.run_tbl, i, 0, t, TEXT_DIM, align="left")
            set_cell(self.run_tbl, i, 1, str(r.get("day") or ""), TEXT_DIM, align="left")
            set_cell(self.run_tbl, i, 2, str(r.get("universe") or 0))
            ex = float(r.get("excess") or 0)
            set_cell(self.run_tbl, i, 3, f"{ex:+.2%}", UP if ex > 0 else DOWN)
            set_cell(self.run_tbl, i, 4, f"{float(r.get('excess_t') or 0):+.2f}", TEXT_DIM)
            nt = float(r.get("excess_neu_t") or 0)
            set_cell(self.run_tbl, i, 5, f"{nt:+.2f}",
                     UP if nt >= 2 else (WARN if nt > 0 else DOWN))
            set_cell(self.run_tbl, i, 6, str(r.get("verdict") or ""), TEXT_DIM, align="left")

    def _render_learn(self, lrn: dict) -> None:
        """因子层的拖后腿提示。表格和主结论归 _render_verify,这里只补一句,
        不覆盖验证信息 —— 两个都在 lrn_msg 上写,后写的会盖掉前面的。"""
        fs = (lrn or {}).get("factors") or []
        worst = [f for f in fs if float(f.get("acc") or 1) < 0.5][:2]
        if not worst:
            return
        tail = " · 拖后腿:" + "、".join(
            f"{f.get('name')}({float(f.get('acc') or 0):.0%})" for f in worst)
        self.lrn_msg.setText(self.lrn_msg.text() + tail)


    def stop(self) -> None:
        """退出时等任务收尾。扫描一次要十几秒,不等待就会 QThread 报错。"""
        for t in list(self._tasks):
            try:
                if t.isRunning():
                    t.wait(3000)
            except Exception:  # noqa: BLE001
                pass
        self._tasks = [t for t in self._tasks if t.isRunning()]


def _from_cloud(d: dict) -> dict:
    """把 /v1/pick/latest 的形状转成 ap.scan() 的返回形状。

    云端批次里存了完整 rows(现价/分数/因子),直接透传;只有老批次没存 rows
    时才退回「只有代码和参考幅度」的窄行 —— 那种情况页面会显示 — 而不是 0。
    """
    rows = []
    for x in d.get("rows") or []:
        if not isinstance(x, dict):
            continue
        rows.append({
            "code": str(x.get("code") or ""),
            "name": str(x.get("name") or ""),
            "price": x.get("price"),
            "chg": x.get("chg"),
            "ret5": x.get("ret5"),
            "amount": x.get("amount"),
            "score": x.get("score"),
            "score_raw": x.get("score_raw"),
            "beta": x.get("beta"),
            "sigma": x.get("sigma"),
            "exp": x.get("exp"),
            "exp_abs": x.get("exp_abs"),
            "prob_up": x.get("prob_up") or d.get("prob_up"),
            "main_in": x.get("main_in"),
            "factors": x.get("factors") or [],
            "pick_verdict": x.get("pick_verdict") or "",
        })
    ic = d.get("ic") or {}
    return {
        "ok": True,
        "day": d.get("day"),
        "horizon": 5,
        "universe": d.get("universe") or 0,
        "n_cand": d.get("universe") or 0,
        "rows": rows,
        "weights": d.get("weights") or {},
        "ic": ic,
        "top_mean": d.get("top_mean") or 0,
        "bench": d.get("bench") or 0,
        "excess": d.get("excess") or 0,
        "excess_t": d.get("excess_t") or 0,
        "excess_neu": d.get("excess_neu") or 0,
        "excess_neu_t": d.get("excess_neu_t") or 0,
        "prob_up": d.get("prob_up") or 0,
        "exp_abs": d.get("exp_abs"),
        "verdict": d.get("verdict") or "",
        "elapsed": d.get("elapsed") or 0,
        "n_factors": len(d.get("weights") or {}),
        "src": "cloud",
    }


def _readme(rep: dict) -> str:
    """把这一页的口径写在页面上,别让人自己猜。"""
    lines = [
        "一、这里不预测涨停,只做候选清单",
        "   分数是「当期显著因子的横截面百分位加权和」,权重取自走查 RankIC。",
        "   因子 |t|<1 或 |IC|<0.004 的直接不给权重,不拿噪声稀释信号。",
        "",
        "二、为什么有时候整张表都是跌的",
        "   这套排序排的是「谁比谁强」,不减大盘 —— 它不知道今天该不该买。",
        "   全市场普跌那天,分数最高的自然就是跌得少的那些,看着像看跌,其实是抗跌。",
        "   当期权重若偏向 低波动 / 低价格位置 / 负的均线多头,结果必然偏「超跌且安静」。",
        "   想只要上涨的就切上面那个「只要今天涨的」;想知道会不会真涨,看「预期涨跌」。",
        "",
        "三、两个数字别混看",
        f"   预期涨跌 {float(rep.get('exp_abs') or 0):+.2%}"
        f"(全市场同期 {float(rep.get('bench') or 0):+.2%})"
        " —— 绝对口径,这批票自己涨多少",
        f"   超额 {float(rep.get('excess') or 0):+.2%}"
        f"(t={float(rep.get('excess_t') or 0):+.2f}) —— 减掉全市场之后还多出来多少",
        f"   中性化超额 {float(rep.get('excess_neu') or 0):+.2%}"
        f"(t={float(rep.get('excess_neu_t') or 0):+.2f}) —— 再把 beta 也剔掉",
        "   大盘跌 2% 而它只跌 1%,超额 +1%、预期涨跌 −1% —— 两个都对,不矛盾。",
        "   已有定论(memory/quantlab-alpha-verdict.md):这套因子的样本外优势",
        "   主要来自 beta 暴露。中性化后 t<2 时,这批票买的就是大盘方向。",
        "",
        "四、数量怎么选",
        "   5 只以内是「排序最强的那几个」;20 只以后分数差距已经很小,",
        "   等于把一大批「差不多」的票都算进来,买哪只都行,也就不叫选股了。",
        "",
        "五、记录与学习",
        "   每只票都写进 sl_predictions(和走势预判同一张表),云端有回传事件",
        "   sl-prd-<id>;到期由「回填到期」按钮按 horizon 个交易日后的收盘价判对错,",
        "   再发 prediction_outcome。命中率与因子误导率在「学习」页看。",
    ]
    return "\n".join(lines)
