# -*- coding: utf-8 -*-
"""Gray 启动入口。

    python run.py

首次运行会生成 config.json 与 data/quantlab.db。
"""
from __future__ import annotations

import os
import sys
import traceback


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    check_only = "--check" in sys.argv[1:]
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
    except ImportError as e:
        print("缺少 PySide6,请先安装依赖:\n  pip install -r requirements.txt\n", e)
        return 2

    from quantlab.ui.main_window import MainWindow
    from quantlab import APP_TITLE

    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("ashare-quant")          # 内部标识,影响 Qt 的应用目录,别改
    app.setApplicationDisplayName(APP_TITLE)
    app.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)

    if check_only:
        # 打包自检:验证 Qt/pyqtgraph/pandas 在本包里能被导入,关键页能实例化。
        # 刻意不建 MainWindow(会起后台线程)、也不建 PlotWidget 后硬退 ——
        # 这两种都会在退出时随机崩(0xC0000409/0xC0000005),是自检方式的问题,
        # 不代表程序坏了;正常 GUI 打开再关掉是干净的。
        try:
            import pandas  # noqa: F401
            import pyqtgraph  # noqa: F401

            from quantlab.ui.quote_page import QuotePage
            from quantlab.ui.settings_page import SettingsPage

            q = QuotePage()
            s = SettingsPage()
            print("OK: Qt/pyqtgraph/pandas 已加载,行情页与设置页可实例化", flush=True)
            q.deleteLater()
            s.deleteLater()
            app.processEvents()
            return 0
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 1

    if "--smoke" in sys.argv[1:]:
        # 打包冒烟:真建主窗口 → 21 页逐页切一遍 → 立刻硬退。
        # 走真实启动路径,能验出「打包后某页崩 / 某个 dll 被裁没了」。
        # 打包是窗口版(console=False),没有 stdout,结果写文件给外部读。
        import pathlib

        base = pathlib.Path(sys.executable if getattr(sys, "frozen", False) else __file__)
        out = base.parent / "_smoke.txt"
        try:
            win = MainWindow()
            win.showNormal()
            app.processEvents()
            for i in range(win.pages.count()):
                win.pages.setCurrentIndex(i)
                app.processEvents()
            out.write_text(f"OK {win.pages.count()} 页全建过", encoding="utf-8")
            MainWindow._exit_now(0)
        except Exception:  # noqa: BLE001
            try:
                out.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
            MainWindow._exit_now(1)
        return 1

    try:
        win = MainWindow()
        win.showNormal()
        win.raise_()
        win.activateWindow()
        rc = app.exec()
        # 正常路径在 CloseEvent 里就 os._exit 了,这里只是兜底
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        return rc
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, f"{APP_TITLE} 启动失败", traceback.format_exc(limit=6))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
