# -*- coding: utf-8 -*-
"""离屏整窗截图:加载系统雅黑,切到指定页面,存 reports/_shot_<page>.png。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["GRAY_NO_HARD_EXIT"] = "1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtGui import QFont, QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
    if os.path.exists(f):
        QFontDatabase.addApplicationFont(f)
app.setFont(QFont("Microsoft YaHei UI", 10))

from quantlab.ui.main_window import MainWindow, NAV  # noqa: E402

win = MainWindow()
win.resize(1600, 940)
win.show()
for _ in range(20):
    app.processEvents()
    time.sleep(0.05)

idx_of = {k: i for i, (_, k, _) in enumerate(NAV)}
for name in sys.argv[1:] or ["quote"]:
    idx = idx_of.get(name)
    if idx is None:
        print("no page", name)
        continue
    win._go(idx)
    for _ in range(40):
        app.processEvents()
        time.sleep(0.05)
    out = os.path.join(ROOT, "reports", f"_shot_{name}.png")
    win.grab().save(out)
    print("saved", out)
win.close()
