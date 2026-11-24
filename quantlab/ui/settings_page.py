# -*- coding: utf-8 -*-
"""设置页:行情刷新、数据目录、界面与免责声明。

公开版不含券商/同花顺接入、加密货币与云端凭据;自动交易只走本机模拟盘。
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..config import APP_DIR, USER_DIR, cfg
from ..version import __version__
from .. import APP_TITLE
from .common import Card
from .theme import TEXT_DIM

DEFAULT_REPO = "https://github.com/Gray2006/ashare-quant"


class SettingsPage(QWidget):
    status = Signal(str)
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = cfg()
        self._update_info: dict = {}
        self._up_job = None
        self._build()
        self.load()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(10)

        card = Card("行情")
        form = QFormLayout()
        self.refresh = QDoubleSpinBox()
        self.refresh.setRange(0.5, 30.0)
        self.refresh.setSingleStep(0.5)
        self.refresh.setDecimals(1)
        self.refresh.setSuffix(" 秒")
        form.addRow("行情刷新间隔", self.refresh)
        self.offline = QCheckBox("离线模式(只用本地缓存,不发请求)")
        form.addRow("", self.offline)
        card._lay.addLayout(form)
        lay.addWidget(card)

        card2 = Card("数据与目录")
        form2 = QFormLayout()
        self.data_edit = QLineEdit(str(USER_DIR / "quantlab.db"))
        self.data_edit.setReadOnly(True)
        form2.addRow("本地数据库", self.data_edit)
        row = QHBoxLayout()
        btn_open = QPushButton("打开数据目录")
        btn_open.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(USER_DIR))))
        btn_app = QPushButton("打开程序目录")
        btn_app.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR))))
        row.addWidget(btn_open)
        row.addWidget(btn_app)
        row.addStretch(1)
        form2.addRow("", row)
        card2._lay.addLayout(form2)
        lay.addWidget(card2)

        card_fb = Card("数据回流(可选,默认关闭)")
        fb_form = QFormLayout()
        self.fb_on = QCheckBox("开启数据回流(只发脱敏后的回测/决策样本)")
        self.fb_on.toggled.connect(self._toggle_feedback)
        fb_form.addRow("", self.fb_on)
        self.fb_url = QLineEdit()
        self.fb_url.setPlaceholderText("https://你的服务端地址")
        fb_form.addRow("服务端地址", self.fb_url)
        self.fb_key = QLineEdit()
        self.fb_key.setEchoMode(QLineEdit.Password)
        self.fb_key.setPlaceholderText("服务端发给你的访问 key")
        fb_form.addRow("访问 key", self.fb_key)
        fb_row = QHBoxLayout()
        btn_flush = QPushButton("立即上传")
        btn_flush.clicked.connect(self._flush_feedback)
        btn_test = QPushButton("测试连接")
        btn_test.clicked.connect(self._test_feedback)
        btn_what = QPushButton("会发什么?")
        btn_what.clicked.connect(self._explain_feedback)
        fb_row.addWidget(btn_flush)
        fb_row.addWidget(btn_test)
        fb_row.addWidget(btn_what)
        fb_row.addStretch(1)
        self.fb_queue = QLabel("")
        self.fb_queue.setStyleSheet(f"color:{TEXT_DIM};")
        fb_row.addWidget(self.fb_queue)
        fb_form.addRow("", fb_row)
        card_fb._lay.addLayout(fb_form)
        lay.addWidget(card_fb)

        # ---------------------------------------------------------- 意见反馈
        card_note = Card("意见反馈")
        n_form = QFormLayout()
        self.note_text = QPlainTextEdit()
        self.note_text.setPlaceholderText("哪里不对、想要什么功能、报错原文……写清楚就行")
        self.note_text.setFixedHeight(110)
        n_form.addRow("内容", self.note_text)
        self.note_contact = QLineEdit()
        self.note_contact.setPlaceholderText("可选,方便回你")
        n_form.addRow("联系方式", self.note_contact)
        self.note_env = QCheckBox("附上版本与系统信息(不含自选股、持仓、路径)")
        self.note_env.setChecked(True)
        n_form.addRow("", self.note_env)
        n_row = QHBoxLayout()
        btn_note_save = QPushButton("保存到本地")
        btn_note_save.setObjectName("Primary")
        btn_note_save.clicked.connect(self._submit_note)
        btn_note_copy = QPushButton("复制成 Issue 正文")
        btn_note_copy.clicked.connect(self._copy_note)
        btn_note_open = QPushButton("打开收件箱目录")
        btn_note_open.clicked.connect(self._open_notes)
        for b in (btn_note_save, btn_note_copy, btn_note_open):
            n_row.addWidget(b)
        n_row.addStretch(1)
        self.note_hint = QLabel("")
        self.note_hint.setStyleSheet(f"color:{TEXT_DIM};")
        n_row.addWidget(self.note_hint)
        n_form.addRow("", n_row)
        card_note._lay.addLayout(n_form)
        lay.addWidget(card_note)

        # -------------------------------------------------------------- 更新
        card_up = Card("更新")
        u_form = QFormLayout()
        self.up_url = QLineEdit(str(self.cfg.get("update.repo_url", "") or DEFAULT_REPO))
        self.up_url.setPlaceholderText("https://github.com/用户名/仓库名")
        u_form.addRow("GitHub 链接", self.up_url)
        u_row = QHBoxLayout()
        self.btn_up_check = QPushButton("检查更新")
        self.btn_up_check.clicked.connect(self._check_update)
        self.btn_up_do = QPushButton("下载并更新")
        self.btn_up_do.setEnabled(False)
        self.btn_up_do.clicked.connect(self._apply_update)
        self.btn_up_open = QPushButton("打开发布页")
        self.btn_up_open.clicked.connect(self._open_release)
        for b in (self.btn_up_check, self.btn_up_do, self.btn_up_open):
            u_row.addWidget(b)
        u_row.addStretch(1)
        u_form.addRow("", u_row)
        self.up_hint = QLabel(f"当前版本 v{__version__}")
        self.up_hint.setWordWrap(True)
        self.up_hint.setStyleSheet(f"color:{TEXT_DIM};")
        u_form.addRow("", self.up_hint)
        self.up_notes = QPlainTextEdit()
        self.up_notes.setReadOnly(True)
        self.up_notes.setFixedHeight(170)
        self.up_notes.setVisible(False)
        self.up_notes.setPlaceholderText("")
        u_form.addRow(self.up_notes)
        card_up._lay.addLayout(u_form)
        lay.addWidget(card_up)

        card3 = Card("关于")
        info = QLabel(
            f"{APP_TITLE} v{__version__}\n\n"
            "本程序只做 A 股公开行情展示、技术分析、条件选股与历史回测,"
            "并带一个本机模拟盘用于练习与复盘;模拟盘只在本地撮合,"
            "自动交易也只走模拟盘,不接券商、不同花顺下单、不发任何真单,"
            "也不构成投资建议。\n"
            "行情来自公开网页接口,可能延迟或中断;回测结果仅代表历史,不代表未来。\n"
            "默认不上传任何数据;只有你在上面手动开启「数据回流」后,"
            "才会发送脱敏后的回测/决策样本。")
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{TEXT_DIM};")
        card3._lay.addWidget(info)
        lay.addWidget(card3)
        lay.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        bar = QHBoxLayout()
        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color:{TEXT_DIM};")
        bar.addWidget(self.hint, 1)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("Primary")
        btn_save.clicked.connect(self.save)
        bar.addWidget(btn_save)
        outer.addLayout(bar)

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        self.refresh.setValue(float(self.cfg.get("refresh_seconds.quote", 1.0)))
        self.offline.setChecked(bool(self.cfg.get("offline", False)))
        try:
            from .. import cloud
            d = cloud._defaults()
        except Exception:  # noqa: BLE001
            d = {}
        self.fb_url.setText(str(self.cfg.get("cloud.base_url", "")
                                or d.get("base_url") or ""))
        self.fb_key.setText(str(self.cfg.get("cloud.key", "")
                                or d.get("public_key") or ""))
        on = bool(self.cfg.get("feedback.enabled", False))
        self.fb_on.blockSignals(True)
        self.fb_on.setChecked(on)
        self.fb_on.blockSignals(False)
        self._refresh_queue()

    def save(self) -> None:
        self.cfg.set("refresh_seconds.quote", float(self.refresh.value()), save=True)
        self.cfg.set("offline", bool(self.offline.isChecked()), save=True)
        self._save_feedback()
        self.hint.setText("已保存")
        self.status.emit("设置已保存")
        self.settings_changed.emit()

    # ------------------------------------------------------------ 数据回流
    def _save_feedback(self) -> None:
        on = bool(self.fb_on.isChecked())
        self.cfg.set("cloud.enabled", on)
        self.cfg.set("cloud.base_url", self.fb_url.text().strip().rstrip("/"))
        self.cfg.set("cloud.key", self.fb_key.text().strip())
        self.cfg.set("feedback.enabled", on, save=True)
        try:
            from .. import feedback
            feedback.stop()
            if on:
                feedback.start()
        except Exception:  # noqa: BLE001
            pass
        self._refresh_queue()

    def _toggle_feedback(self, on: bool) -> None:
        if not on:
            self._refresh_queue()
            return
        if bool(self.cfg.get("feedback.consent", False)):
            return
        text = (
            "开启后会把这台电脑上的脱敏样本发到你填的服务端,用于改进模型。\n\n"
            "会发:回测的收益率/回撤/夏普等比例指标、策略名与参数、版本号。\n"
            "不发:持仓金额、账户/券商信息、API key、密码、地址、手机号。\n\n"
            "默认关闭;随时可以关,关掉后一行都不发。\n\n"
            "确认开启吗?")
        ok = QMessageBox.question(self, "数据回流", text,
                                  QMessageBox.Yes | QMessageBox.No,
                                  QMessageBox.No)
        if ok != QMessageBox.Yes:
            self.fb_on.blockSignals(True)
            self.fb_on.setChecked(False)
            self.fb_on.blockSignals(False)
            return
        self.cfg.set("feedback.consent", True, save=True)

    def _test_feedback(self) -> None:
        try:
            from .. import cloud
            self._save_feedback()
            r = cloud.health()
            self.hint.setText(f"连接正常:{str(r)[:120]}")
        except Exception as e:  # noqa: BLE001
            self.hint.setText(f"连接失败:{str(e)[:120]}")

    def _explain_feedback(self) -> None:
        QMessageBox.information(
            self, "会发什么",
            "发送的字段:回测指标(收益率/回撤/夏普/胜率等比例值)、"
            "策略名与数值参数、程序版本号、事件时间。\n\n"
            "不发送:金额绝对值、账户号、券商、API key、密码、地址、"
            "手机号、身份证。\n\n"
            "上传前会再过滤一遍敏感键名;关闭开关后不写本地队列、不发包。")

    def _flush_feedback(self) -> None:
        try:
            from .. import feedback
            r = feedback.flush(force=True)
            if r.get("error"):
                self.hint.setText(f"上传失败:{r['error']}")
            else:
                self.hint.setText(f"已上传 {r.get('sent', 0)} 条,剩余 "
                                  f"{r.get('left', 0)} 条")
        except Exception as e:  # noqa: BLE001
            self.hint.setText(f"上传失败:{str(e)[:90]}")
        self._refresh_queue()

    def _refresh_queue(self) -> None:
        try:
            from .. import feedback
            st = feedback.state()
            self.fb_queue.setText(
                f"本地待上传 {st.get('queued', 0)} 条"
                if st.get("enabled") else "未开启")
        except Exception:  # noqa: BLE001
            self.fb_queue.setText("")

    # ------------------------------------------------------------ 意见反馈
    def _submit_note(self) -> None:
        try:
            from .. import notes
            path = notes.save(self.note_text.toPlainText(),
                              self.note_contact.text(),
                              self.note_env.isChecked())
            self.note_hint.setText(f"已存到 {path.name}(共 {notes.count()} 条)")
            self.status.emit("反馈已保存到本地")
        except Exception as e:  # noqa: BLE001
            self.note_hint.setText(f"没存上:{str(e)[:80]}")

    def _copy_note(self) -> None:
        from .. import notes
        body = self.note_text.toPlainText().strip()
        if not body:
            self.note_hint.setText("先写点内容")
            return
        QGuiApplication.clipboard().setText(
            notes.build_text(body, self.note_contact.text(), self.note_env.isChecked()))
        self.note_hint.setText("已复制,粘到 GitHub Issue 就行")

    def _open_notes(self) -> None:
        from .. import notes
        try:
            notes.open_dir()
        except Exception as e:  # noqa: BLE001
            self.note_hint.setText(f"打不开目录:{str(e)[:60]}")

    # ---------------------------------------------------------------- 更新
    def _check_update(self) -> None:
        from .. import updater
        from ..engine.hub import TaskRunner

        url = self.up_url.text().strip()
        self._update_info = {}
        self.btn_up_check.setEnabled(False)
        self.btn_up_do.setEnabled(False)
        self.up_notes.setVisible(False)
        self.up_notes.setPlainText("")
        self.up_hint.setText("正在问 GitHub …")
        self.cfg.set("update.repo_url", url, save=True)
        job = TaskRunner(lambda: updater.check(url))
        job.succeeded.connect(self._update_ok)
        job.failed.connect(self._update_fail)
        job.finished.connect(lambda: self.btn_up_check.setEnabled(True))
        self._up_job = job
        job.start()

    def _update_ok(self, info) -> None:
        from .. import updater
        info = info if isinstance(info, dict) else {}
        self._update_info = info
        latest = str(info.get("version") or "")
        remote = info.get("tag") or latest or "?"
        newer = updater.is_newer(latest, __version__)
        if newer:
            self.up_hint.setText(
                f"有新版本 {remote}(当前 v{__version__})。点「下载并更新」")
            self.btn_up_do.setEnabled(True)
        else:
            self.up_hint.setText(f"已经是最新的 v{__version__}(远端 {remote})")
            self.btn_up_do.setEnabled(False)
        self._show_notes(info.get("notes") or "")

    def _show_notes(self, text: str) -> None:
        """把 release 说明摊在界面上,太长就截断,全文在发布页。"""
        text = (text or "").strip()
        if not text:
            self.up_notes.setVisible(False)
            self.up_notes.setPlainText("")
            return
        limit = 1800
        shown = text if len(text) <= limit else text[:limit].rstrip() + "\n\n…(全文见发布页)"
        self.up_notes.setPlainText(shown)
        self.up_notes.setVisible(True)

    def _update_fail(self, msg) -> None:
        self.up_hint.setText(f"检查失败:{str(msg).splitlines()[0][:120]}")

    def _open_release(self) -> None:
        url = (self._update_info or {}).get("html_url") or self.up_url.text().strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _apply_update(self) -> None:
        from .. import updater

        info = self._update_info or {}
        if not info:
            return
        if updater.is_frozen():
            QMessageBox.information(
                self, "更新", "打包版请到发布页手动下载新版安装包,别在程序里覆盖自己。")
            return
        ok = QMessageBox.question(
            self, "更新",
            f"将下载 {info.get('tag') or info.get('version')} 并覆盖程序文件。\n"
            "会保留 config.json、data/、.venv/。\n"
            "完成后程序自动退出,3 秒后重启。继续吗?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ok != QMessageBox.Yes:
            return
        from ..config import APP_DIR
        self.btn_up_do.setEnabled(False)
        self.up_hint.setText("下载中 …(看进度条没动是正常的,大包要一会儿)")
        from ..engine.hub import TaskRunner
        job = TaskRunner(lambda: self._do_update(info, str(APP_DIR)))
        job.succeeded.connect(self._update_staged)
        job.failed.connect(self._update_fail)
        self._up_job = job
        job.start()

    @staticmethod
    def _do_update(info: dict, app_dir: str) -> str:
        from .. import updater
        zip_path = updater.download(info)
        src = updater.extract(zip_path)
        return str(updater.stage(src, app_dir))

    def _update_staged(self, bat) -> None:
        from .. import updater
        try:
            updater.apply_now(bat)
        except Exception as e:  # noqa: BLE001
            self.up_hint.setText(f"启动更新脚本失败:{str(e)[:90]}")
            self.btn_up_do.setEnabled(True)
            return
        self.up_hint.setText("文件已就绪,程序马上退出,3 秒后自动重启")
        self.status.emit("更新已就绪,正在重启")
        QApplication.instance().quit()

    def apply_update_result(self, _res=None) -> None:  # 兼容旧调用
        return
