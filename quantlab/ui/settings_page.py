# -*- coding: utf-8 -*-
"""设置页:行情刷新、数据目录、界面与免责声明。

公开版不含交易通道、券商/同花顺接入、加密货币与云端凭据。
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..config import APP_DIR, USER_DIR, cfg
from ..version import __version__
from .. import APP_TITLE
from .common import Card
from .theme import TEXT_DIM


class SettingsPage(QWidget):
    status = Signal(str)
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = cfg()
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
        btn_what = QPushButton("会发什么?")
        btn_what.clicked.connect(self._explain_feedback)
        fb_row.addWidget(btn_flush)
        fb_row.addWidget(btn_what)
        fb_row.addStretch(1)
        self.fb_queue = QLabel("")
        self.fb_queue.setStyleSheet(f"color:{TEXT_DIM};")
        fb_row.addWidget(self.fb_queue)
        fb_form.addRow("", fb_row)
        card_fb._lay.addLayout(fb_form)
        lay.addWidget(card_fb)

        card3 = Card("关于")
        info = QLabel(
            f"{APP_TITLE} v{__version__}\n\n"
            "本程序只做 A 股公开行情展示、技术分析、条件选股与历史回测,"
            "不接券商、不同花顺下单、不提供任何交易或投资建议。\n"
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
        self.fb_url.setText(str(self.cfg.get("cloud.base_url", "") or ""))
        self.fb_key.setText(str(self.cfg.get("cloud.key", "") or ""))
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

    def apply_update_result(self, _res=None) -> None:  # 兼容旧调用
        return
