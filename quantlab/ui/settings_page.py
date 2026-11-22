# -*- coding: utf-8 -*-
"""设置页:行情刷新、数据目录、界面与免责声明。

公开版不含交易通道、券商/同花顺接入、加密货币与云端凭据。
"""
from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

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

        card3 = Card("关于")
        info = QLabel(
            f"{APP_TITLE} v{__version__}\n\n"
            "本程序只做 A 股公开行情展示、技术分析、条件选股与历史回测,"
            "不接券商、不同花顺下单、不提供任何交易或投资建议。\n"
            "行情来自公开网页接口,可能延迟或中断;回测结果仅代表历史,不代表未来。\n"
            "数据与配置只存在本机,不上传。")
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

    def save(self) -> None:
        self.cfg.set("refresh_seconds.quote", float(self.refresh.value()), save=True)
        self.cfg.set("offline", bool(self.offline.isChecked()), save=True)
        self.hint.setText("已保存")
        self.status.emit("设置已保存")
        self.settings_changed.emit()

    def apply_update_result(self, _res=None) -> None:  # 兼容旧调用
        return
