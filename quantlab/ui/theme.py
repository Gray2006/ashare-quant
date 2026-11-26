# -*- coding: utf-8 -*-
"""深色主题:颜色常量 + QSS。

配色走「石墨底 + 冷蓝强调」:背景近乎纯黑但带一点蓝,卡片比背景亮两档,
强调色只出现在选中态、主按钮和涨跌上,避免整屏花。

两条容易踩的坑,改 QSS 前先看:
  · QSpinBox / QDoubleSpinBox / QComboBox 内部的 QLineEdit 也会吃到
    QLineEdit 的 padding 和 border —— 外面一层、里面一层,文字被挤成半截。
    所以下面单独把内部编辑器清零(padding:0 + border:none + transparent)。
  · 占位符颜色 QSS 管不了,得用 QPalette.PlaceholderText,见 apply_palette()。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette

BG = "#0a0e13"        # 页面底色
BG2 = "#111821"       # 卡片
BG3 = "#18222d"       # 卡片内嵌块 / 输入框
BG4 = "#0c1218"       # 侧栏 / 顶栏
BORDER = "#1f2a38"
BORDER2 = "#33455c"   # hover 边框
TEXT = "#e6ecf5"
TEXT_DIM = "#94a2b4"
TEXT_MUT = "#6b7a8d"
ACCENT = "#4c8dff"
ACCENT2 = "#79adff"
ACCENT_DIM = "#1d3a67"
UP = "#f6465d"        # A 股:涨红
DOWN = "#2ebd85"      # 跌绿
FLAT = "#9aa5b5"
WARN = "#f0b90b"
GRID = "#18212e"
LINE = "#1b2532"
# 加密用国际惯例:涨绿跌红(与 A 股相反)
CRYPTO_UP = "#2ebd85"
CRYPTO_DOWN = "#f6465d"


# ---------------------------------------------------------------- 箭头图标
# QSS 里的 border 三角形在 Qt 上会画成一根短横,所以箭头必须是图片。
# 打包版不带外部资源,启动时把几个 SVG 落到临时目录,再让 QSS 引用。
# 尺寸按 2 倍画、QSS 里按 1 倍用 —— 150% 缩放下也不糊。
_SVG_TMPL = ('<svg xmlns="http://www.w3.org/2000/svg" width="{w2}" height="{h2}" '
             'viewBox="0 0 {w2} {h2}"><path d="{d}" fill="none" stroke="{c}" '
             'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_CHEVRON_D = "M{d} {t} L{m} {b} L{e} {t}"


def _icon_dir() -> Path:
    import tempfile
    for base in (Path(tempfile.gettempdir()) / "gray-quantlab-ui",
                 Path.home() / ".gray-quantlab-ui"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return base
        except OSError:
            continue
    return Path(".")


def _write_icon(name: str, body: str) -> str:
    path = _icon_dir() / name
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            path.write_text(body, encoding="utf-8")
    except OSError:
        pass
    return path.as_posix()


def _chevron(w: int, h: int, color: str, up: bool = False) -> str:
    """宽 w 高 h(2 倍像素),描边色 color 的 V 形箭头。"""
    w2, h2 = w * 2, h * 2
    m = w2 / 2.0
    pad = h2 * 0.22
    if up:
        d = f"M{pad} {h2 - pad} L{m} {pad} L{w2 - pad} {h2 - pad}"
    else:
        d = f"M{pad} {pad} L{m} {h2 - pad} L{w2 - pad} {pad}"
    return _SVG_TMPL.format(w2=w2, h2=h2, d=d, c=color, sw=max(2.0, h2 * 0.24))


ICON_DOWN = _write_icon("chevron-down.svg", _chevron(9, 6, TEXT_DIM))
ICON_DOWN_HI = _write_icon("chevron-down-hi.svg", _chevron(9, 6, TEXT))
ICON_UP = _write_icon("chevron-up.svg", _chevron(9, 6, TEXT_DIM, up=True))
ICON_UP_HI = _write_icon("chevron-up-hi.svg", _chevron(9, 6, TEXT, up=True))
ICON_DOWN_S = _write_icon("caret-down.svg", _chevron(7, 5, TEXT_DIM))
ICON_UP_S = _write_icon("caret-up.svg", _chevron(7, 5, TEXT_DIM, up=True))


QSS = f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
    outline: none;
}}
QWidget#Root {{
    background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1,
                stop:0 #0e1620, stop:0.35 #0a1017, stop:1 #070a0f);
}}
QFrame#Sidebar {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0.2,
                stop:0 #0a1017, stop:0.75 #0c141c, stop:1 #0e1721);
    border-right: 1px solid {BORDER};
}}
QFrame#TopBar {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #0e1620, stop:1 #0b1219);
    border: 1px solid {BORDER};
    border-top-color: #263445;
    border-radius: 10px;
}}
QFrame#Card {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #16202c, stop:1 #111a24);
    border: 1px solid {BORDER};
    border-top-color: #2a3a4d;
    border-radius: 12px;
}}
QFrame#Card:hover {{ border-color: {BORDER2}; border-top-color: #3a4f68; }}
QFrame#CardFlat {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 #17212d, stop:1 #121b25);
    border: 1px solid {BORDER};
    border-top-color: #27364a;
    border-radius: 10px;
}}
QFrame#CardHead {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {LINE};
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
}}
QLabel#AppName {{
    font-size: 14px; font-weight: 700; color: #ffffff;
}}
QLabel#AppSub {{ font-size: 11px; color: {TEXT_MUT}; }}
QLabel#PageTitle {{ font-size: 17px; font-weight: 600; color: #ffffff; }}
QLabel#PageHint {{ font-size: 10px; color: {TEXT_MUT}; }}
QLabel#CardTitle {{
    font-size: 12px; color: #b9c7d8; font-weight: 600; letter-spacing: 0.2px;
}}
QLabel#CardHint {{ font-size: 10px; color: {TEXT_MUT}; }}
QLabel#BigNum {{ font-size: 21px; font-weight: 600; }}
QLabel#BigNum[dim="true"] {{ color: {TEXT_DIM}; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#Mono {{ font-family: "Consolas", "Cascadia Mono", monospace; }}
QLabel#Section {{ font-size: 12px; color: {TEXT_MUT}; font-weight: 600; }}
QLabel#Clock {{ font-size: 11px; color: {TEXT_DIM}; font-family: "Consolas", monospace; }}
QLabel#Badge {{
    background: {ACCENT_DIM}; color: #cfe1ff; border-radius: 5px;
    padding: 1px 6px; font-size: 10px;
}}

QPushButton {{
    background: {BG3}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 13px; color: {TEXT}; min-height: 18px;
}}
QPushButton:hover {{ background: #1d2836; border-color: {BORDER2}; color: #ffffff; }}
QPushButton:pressed {{ background: #141d28; }}
QPushButton:focus {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #4d5866; background: #121821; border-color: #1a2431; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #5b9bff, stop:1 #3c7df0);
    border: 1px solid #6aa2ff; color: #ffffff; font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #74acff, stop:1 #4b89f8);
    border-color: #8dbcff;
}}
QPushButton#Primary:disabled {{ background: #23324a; border-color: #2b3d59; color: #7f8b9c; }}
QPushButton#Danger {{ background: #3a1620; border: 1px solid #7d2536; color: #ff9aa8; }}
QPushButton#Danger:hover {{ background: #4c1b28; border-color: #b02d45; color: #ffc2cb; }}
QPushButton#Success {{ background: #102b22; border: 1px solid #1c6b4f; color: #7ee2b8; }}
QPushButton#Success:hover {{ background: #14382c; border-color: #27a077; color: #a5f0cf; }}
QPushButton#Ghost {{
    background: transparent; border: 1px solid {BORDER}; color: {TEXT_DIM};
    padding: 5px 10px;
}}
QPushButton#Ghost:hover {{ color: {TEXT}; border-color: {BORDER2}; background: #131c26; }}
QPushButton#IconBtn {{
    background: transparent; border: 1px solid transparent; border-radius: 8px;
    color: {TEXT_DIM}; font-size: 13px; padding: 0;
}}
QPushButton#IconBtn:hover {{ background: #131c26; border-color: {BORDER}; color: {TEXT}; }}
QPushButton#IconBtn:pressed {{ background: #0f1720; }}

/* 侧栏导航:分组标题 + 条目。21 个页面必须能滚,所以外层是 QScrollArea。 */
QScrollArea#NavScroll, QWidget#NavHolder {{ background: transparent; border: none; }}
QScrollArea#PageScroll {{ background: transparent; border: none; }}
QScrollArea#PageScroll > QWidget > QWidget {{ background: transparent; }}
QPushButton#Nav {{
    text-align: left; padding: 7px 12px; border: 1px solid transparent;
    border-left: 3px solid transparent;
    border-radius: 8px; background: transparent; color: {TEXT_DIM}; font-size: 13px;
}}
QPushButton#Nav:hover {{ background: #16212d; color: {TEXT}; }}
QPushButton#Nav:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 #1e3a5c, stop:0.55 #17293f, stop:1 #121c27);
    border-left: 3px solid {ACCENT};
    color: #ffffff; font-weight: 600;
}}
QPushButton#NavGroup {{
    text-align: left; padding: 10px 6px 4px 6px; border: none; background: transparent;
    color: {TEXT_MUT}; font-size: 11px; font-weight: 600;
    letter-spacing: 1px;
}}
QPushButton#NavGroup:hover {{ color: {TEXT_DIM}; }}
QPushButton#NavGroup[folded="true"] {{ color: {TEXT_MUT}; }}

/* ---------------------------------------------------------------- 输入类
   注意:内层编辑器必须清零 padding/border,否则文字被压成半截。 */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {{
    background: #0d141d;
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    min-height: 20px;
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover,
QDoubleSpinBox:hover, QDateEdit:hover, QTimeEdit:hover {{ border-color: {BORDER2}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QTimeEdit:focus {{
    border-color: {ACCENT}; background: #101a26;
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
QDoubleSpinBox:disabled {{ color: #55606f; background: #0f151d; border-color: #17202b; }}
QSpinBox QLineEdit, QDoubleSpinBox QLineEdit, QTimeEdit QLineEdit,
QDateEdit QLineEdit, QComboBox QLineEdit {{
    background: transparent; border: none; border-radius: 0;
    padding: 0; margin: 0; min-height: 0; color: {TEXT};
}}
QComboBox::drop-down {{ border: none; width: 22px; background: transparent; }}
QComboBox::down-arrow {{
    image: url("{ICON_DOWN}"); width: 9px; height: 6px; margin-right: 8px;
}}
QComboBox::down-arrow:hover {{ image: url("{ICON_DOWN_HI}"); }}
QComboBox QAbstractItemView {{
    background: {BG2}; border: 1px solid {BORDER2}; border-radius: 8px;
    selection-background-color: #1d2f47; padding: 4px; outline: none;
}}
QComboBox QAbstractItemView::item {{ padding: 5px 8px; min-height: 20px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QTimeEdit::up-button,
QDateEdit::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; background: transparent; border: none; border-top-right-radius: 7px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button, QTimeEdit::down-button,
QDateEdit::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; background: transparent; border: none; border-bottom-right-radius: 7px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: #1c2a3a;
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QTimeEdit::up-arrow, QDateEdit::up-arrow {{
    image: url("{ICON_UP_S}"); width: 7px; height: 5px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QTimeEdit::down-arrow, QDateEdit::down-arrow {{
    image: url("{ICON_DOWN_S}"); width: 7px; height: 5px;
}}
/* 勾选框 / 单选点 / 箭头:交给 GrayStyle 画,这里只管间距和文字色。
   以前用 QSS 拼 border 三角形,Qt 会画成一根白短横。 */
QCheckBox, QRadioButton {{ spacing: 8px; color: {TEXT_DIM}; }}
QCheckBox:hover, QRadioButton:hover {{ color: {TEXT}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: #4d5866; }}

QTableWidget, QTableView {{
    background: {BG2}; border: 1px solid {BORDER}; border-radius: 10px;
    gridline-color: {GRID}; selection-background-color: #1d3350;
    selection-color: #ffffff; alternate-background-color: #0e151d;
}}
QTableWidget::item, QTableView::item {{ padding: 4px 10px; border: none; }}
QTableWidget::item:hover, QTableView::item:hover {{ background: #17212c; }}
QHeaderView::section {{
    background: #0f1720; color: #8fa1b5; border: none;
    border-bottom: 1px solid {BORDER}; padding: 9px 10px;
    font-size: 12px; font-weight: 600; letter-spacing: 0.4px;
}}
QHeaderView::section:hover {{ color: {TEXT}; background: #14202c; }}
QTableCornerButton::section {{ background: #0f1720; border: none; }}

QTabWidget::pane {{
    border: 1px solid {BORDER}; border-radius: 10px; background: {BG2}; top: -1px;
}}
QTabBar::tab {{
    background: transparent; color: {TEXT_DIM}; padding: 6px 13px; margin-right: 2px;
    border-bottom: 2px solid transparent; font-size: 12px;
}}
QTabBar::tab:hover {{ color: {TEXT}; background: #131d28; border-radius: 6px 6px 0 0; }}
QTabBar::tab:selected {{
    color: #ffffff; border-bottom: 2px solid {ACCENT};
    background: #16202c; border-radius: 6px 6px 0 0;
}}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2b3a4b; border-radius: 3px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: #3d5168; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2b3a4b; border-radius: 3px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: #3d5168; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QProgressBar {{
    background: #0d141d; border: 1px solid {BORDER}; border-radius: 5px;
    height: 8px; text-align: center;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3c7df0, stop:1 #79adff);
    border-radius: 4px;
}}

QListWidget {{ background: {BG2}; border: 1px solid {BORDER}; border-radius: 10px; outline: none; }}
QListWidget::item {{ padding: 8px 10px; border-bottom: 1px solid {LINE}; }}
QListWidget::item:selected {{ background: #1b2c44; color: #ffffff; }}
QListWidget::item:hover {{ background: #16202c; }}

QToolTip {{
    background: #16202c; color: {TEXT}; border: 1px solid {BORDER2};
    padding: 5px 7px; border-radius: 6px;
}}
QTextEdit, QPlainTextEdit {{
    background: #0d141d; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 6px; selection-background-color: {ACCENT};
}}
QGraphicsView {{ border: none; background: transparent; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: {ACCENT_DIM}; }}
QStatusBar {{
    background: {BG4}; color: {TEXT_MUT}; border-top: 1px solid {BORDER};
    font-size: 11px;
}}
QStatusBar::item {{ border: none; }}
QMenu {{
    background: {BG2}; border: 1px solid {BORDER2}; border-radius: 8px; padding: 5px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: #1d2f47; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
"""


def apply_palette(widget) -> None:
    """QSS 管不到的占位符/禁用色,用调色板补上。

    默认 PlaceholderText 是 50% 透明的前景色 —— 深底上等于看不见,
    设置页里一堆「未配置」「留空不改」的提示就是这么糊掉的。
    """
    pal = widget.palette()
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_MUT))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    widget.setPalette(pal)


def apply_app_palette(app=None) -> None:
    """给整个 App 设调色板:对话框、弹出菜单、新建的控件都跟着走。"""
    if app is None:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
    if app is None:
        return
    # 先换 style 再铺调色板 —— setStyle 会把 palette 重置成样式自带的,
    # 顺序反了禁用色和占位符会丢。
    try:
        from .style import GrayStyle
        if not isinstance(app.style(), GrayStyle):
            app.setStyle(GrayStyle())
    except Exception:  # noqa: BLE001
        pass
    pal = app.palette()
    pal.setColor(QPalette.PlaceholderText, QColor(TEXT_MUT))
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor("#0d141d"))
    pal.setColor(QPalette.AlternateBase, QColor("#0d141c"))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(BG3))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor("#16202c"))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor("#55606f"))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#55606f"))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#55606f"))
    app.setPalette(pal)
