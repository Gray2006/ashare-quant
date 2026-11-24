# -*- coding: utf-8 -*-
"""Gray 主窗口:分组侧栏导航 + 顶栏页面信息 + 各功能页。

界面用 Board 做二维分栏:每张卡片的四边都能拖,布局自动存盘。
导航按用途分成 看盘 / 研究 / 研判 / 交易 / 管理 五组,可折叠、可整体收起。
"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QPushButton, QScrollArea,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from ..config import cfg
from ..engine.hub import QuoteHub
from ..utils import SESSION_LABEL, fmt_pct, fmt_price, session_state
from ..version import __version__ as VERSION
from .backtest_page import BacktestPage
from .board import Board
from .forecast_page import ForecastPage
from .pick_page import PickPage
from .risk_page import RiskBandPage
from .health_page import HealthPage
from .help import HelpDialog
from .macro_page import MacroPage
from .patterns_page import PatternPage
from .quote_page import QuotePage
from .screen_page import ScreenPage
from .sector_page import SectorPage
from .settings_page import SettingsPage
from .ta_page import TAPage
from .grid_page import GridPage
from .lhb_page import LhbPage
from .limitup_page import LimitUpPage
from .hotspot_page import HotspotPage
from .company_page import CompanyPage
from .company_eval_page import CompanyEvalPage
from .strategy_page import StrategyPage
from .news_page import NewsPage
from .alert_page import AlertPage
from .theme import (ACCENT, BG4, BORDER, DOWN, FLAT, QSS, TEXT, TEXT_DIM,
                    TEXT_MUT, UP, apply_app_palette)
from .world_page import WorldPage

# 导航按「干什么」分组。公开版不含交易、加密、云端与私有学习页。
NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("看盘", [
        ("行情", "quote", "自选股实时报价 · 分时 / K线 / 资金流 / 新闻"),
        ("多图", "grid", "四图并排 · 十字线联动 · 同周期下标严格可比"),
        ("板块", "sector", "行业 / 概念 / 地域资金流 · 板块分时与成分股"),
        ("龙虎榜", "lhb", "每日上榜个股 · 买卖席位明细 · 净买入排序"),
        ("涨停", "limitup", "当日涨停池 · 连板梯队 · 炸板/跌停与情绪分析"),
        ("市场热点", "hotspot", "情绪 · 概念/行业涨幅榜 · 强势个股 · 热点快讯"),
        ("全球市场", "world", "美股港股商品汇率 · 国际新闻时政"),
        ("资讯", "news", "财经资讯与政策 · 分类过滤 · 双击看原文"),
        ("预警", "alert", "价格/涨跌幅条件预警 · 命中留痕"),
    ]),
    ("研究", [
        ("公司分析", "company", "F10 资料 · 主营构成 · 财务趋势 · 估值同业 · 股东事件"),
        ("公司评估", "company_eval", "财务 / 资金流 / 舆论 / 产品 / 风险,规则打分报告"),
        ("选股", "screen", "条件筛选 · 主力资金榜 · 板块资金流"),
        ("技术分析", "ta", "缠论结构 · 道氏趋势 · 智能资金 · 背离 · 共振投票"),
        ("形态", "pattern", "K线形态识别 · 出现之后的实测涨跌频率"),
        ("策略广场", "strategy", "内置策略 · 自定义规则 · Pine Script 导入"),
        ("回测", "backtest", "策略回测 · 净值曲线 · 成绩单与批量对比"),
    ]),
    ("研判", [
        ("大盘研判", "macro", "五维打分:指数趋势 / 广度 / 资金 / 外围 / 消息"),
        ("走势预判", "forecast", "涨/平/跌概率 + 因子贡献 + 走查命中率"),
        ("涨幅预选", "pick", "全市场快扫 · 每日候选 + 参考幅度"),
        ("风险档位", "risk", "横截面波动档位 · 建议仓位上限 · 走查校验"),
    ]),
    ("管理", [
        ("数据健康", "health", "数据源状态 · 熔断明细 · 各领域数据新鲜度"),
        ("设置", "settings", "刷新频率、风控硬闸、数据源与界面"),
    ]),
]

NAV: list[tuple[str, str, str]] = [item for _g, items in NAV_GROUPS for item in items]
NAV_GROUP_OF: dict[str, str] = {
    key: gname for gname, items in NAV_GROUPS for _n, key, _h in items}

SIDEBAR_W = 196      # 侧栏宽度(收起时这 196px 全给数据区)


from .. import APP_TITLE  # noqa: E402


def _build_stamp() -> str:
    """程序文件的时间戳,当成构建标记打在侧栏底部。

    打包版取 exe 自己的 mtime,源码运行取本文件 —— 用户报「改了没生效」时,
    看一眼左下角就知道他手上跑的是哪一版,不用猜。
    """
    import datetime
    import os
    import sys

    path = sys.executable if getattr(sys, "frozen", False) else __file__
    try:
        ts = os.path.getmtime(path)
    except OSError:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class IndexChip(QFrame):
    """顶栏指数块。"""

    def __init__(self, name: str = "--", parent=None):
        super().__init__(parent)
        self.setObjectName("CardFlat")
        self._chip_qss(FLAT)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 4, 9, 4)
        lay.setSpacing(0)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.name = QLabel(name)
        self.name.setStyleSheet(f"color:{TEXT_MUT};font-size:10px;")
        self.price = QLabel("-")
        self.price.setStyleSheet("font-size:12px;font-weight:600;")
        row.addWidget(self.name)
        row.addWidget(self.price)
        self.chg = QLabel("-")
        self.chg.setStyleSheet("font-size:10px;")
        row.addWidget(self.chg)
        row.addStretch(1)
        lay.addLayout(row)

    def _chip_qss(self, col: str) -> None:
        """左边一条 2px 涨跌色:四个指数块一眼看出红绿,不用逐个读数字。"""
        self.setStyleSheet(
            "QFrame#CardFlat { background:#0e1620; border:1px solid #1b2532;"
            f" border-left:2px solid {col}; border-radius:8px; }}")

    def set(self, name: str, price: float | None, chg: float | None) -> None:
        self.name.setText(name)
        self.price.setText(fmt_price(price))
        col = FLAT if not chg else (UP if chg > 0 else DOWN)
        self._chip_qss(col)
        self.price.setStyleSheet(f"font-size:12px;font-weight:600;color:{col};")
        self.chg.setText(fmt_pct(chg))
        self.chg.setStyleSheet(f"font-size:10px;color:{col};")


class MainWindow(QMainWindow):
    _update_found = Signal(object)

    # key → 页面类。加页只要 NAV_GROUPS 加一行 + 这里加一个构造器,漏了会断言报错。
    _BUILDERS: dict[str, type] = {
        "quote": QuotePage, "screen": ScreenPage, "sector": SectorPage,
        "lhb": LhbPage, "backtest": BacktestPage,
        "strategy": StrategyPage, "macro": MacroPage,
        "forecast": ForecastPage, "pick": PickPage, "risk": RiskBandPage,
        "pattern": PatternPage, "ta": TAPage, "grid": GridPage,
        "world": WorldPage, "news": NewsPage,
        "alert": AlertPage,
        "health": HealthPage, "settings": SettingsPage,
        "limitup": LimitUpPage, "hotspot": HotspotPage,
        "company": CompanyPage, "company_eval": CompanyEvalPage,
    }
    # 要网关实例的页(公开版没有交易,留空)
    _NEED_GW = ()
    # 启动就要在的页:首页 + 后台常驻的预警轮询 + 设置
    _EAGER = ("quote", "settings", "alert")

    def __init__(self):
        super().__init__()
        apply_app_palette()      # 占位符/禁用色:QSS 管不到,先铺调色板
        self.cfg = cfg()
        self._build()
        self._apply_geometry()
        self._start_hub()

        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()

    # ------------------------------------------------------------ 窗口
    def _apply_geometry(self) -> None:
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1200, 780)
        self.resize(1520, 960)
        geo = self.cfg.get("ui.window") or {}
        try:
            w, h = int(geo.get("w") or 0), int(geo.get("h") or 0)
            if w >= 1200 and h >= 780:
                self.resize(w, h)
            x, y = int(geo.get("x") or -1), int(geo.get("y") or -1)
            if x >= 0 and y >= 0:
                rect = QRect(x, y, self.width(), self.height())
                for scr in QApplication.screens():
                    if scr.availableGeometry().intersects(rect):
                        self.move(x, y)
                        break
        except Exception:
            pass

    def _save_geometry(self) -> None:
        try:
            self.cfg.set("ui.window", {"x": self.x(), "y": self.y(),
                                       "w": self.width(), "h": self.height()}, save=True)
        except Exception:
            pass

    # ------------------------------------------------------------ 启动
    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_sidebar())

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(14, 12, 14, 10)
        rv.setSpacing(10)
        rv.addWidget(self._build_topbar())

        self.pages = QStackedWidget()
        self.page_map: dict[str, int] = {}
        for idx, (_, key, _hint) in enumerate(NAV):
            self.page_map[key] = idx
        missing = [k for _n, k, _h in NAV if k not in self._BUILDERS]
        assert not missing, f"NAV 里的页面没有构造器:{missing}"
        # 页面按需构造:启动只建首页 + 后台必须常驻的预警页,其余切过去才建。
        # 先给每页放一个占位控件,保证 pages.count() 与 NAV 一致,
        # 换页/跳转/Ctrl+n 的下标全不用改;切到占位格时再换成真页面。
        self._built: dict[str, QWidget] = {}
        self._all_pages: list[QWidget] = []
        self._building = False
        for _name, key, _hint in NAV:
            ph = QWidget()
            ph.setObjectName(f"Placeholder_{key}")
            self.pages.addWidget(ph)
        rv.addWidget(self.pages, 1)
        outer.addWidget(right, 1)

        apply_app_palette()
        self.setStyleSheet(QSS)

        bar = QStatusBar()
        self.setStatusBar(bar)
        self.feed_label = QLabel("行情 --")
        self.feed_label.setStyleSheet(f"color:{TEXT_MUT};font-size:10px;"
                                      "font-family:Consolas;padding-right:10px;")
        bar.addPermanentWidget(self.feed_label)
        self._status("就绪 · 拖动任意分栏线调整卡片大小")

        # 换页时把目标页建出来(占位格换成真页面)
        self.pages.currentChanged.connect(self._on_page_changed)
        for key in self._EAGER:
            self._ensure_page(key)

        # 快捷键
        for i in range(min(len(NAV), 9)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda i=i: self._go(i))
        QShortcut(QKeySequence("F5"), self, activated=self._refresh_current)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self, activated=self._reset_layout)
        QShortcut(QKeySequence("F1"), self, activated=self._open_help)
        # 页面多到 21 个之后,翻页和按名字跳比记 Ctrl+n 实在
        QShortcut(QKeySequence("Ctrl+K"), self, activated=self._open_jump)
        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self._toggle_sidebar())
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=lambda: self._step_page(1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self,
                  activated=lambda: self._step_page(-1))
        QShortcut(QKeySequence("Ctrl+PgDown"), self, activated=lambda: self._step_page(1))
        QShortcut(QKeySequence("Ctrl+PgUp"), self, activated=lambda: self._step_page(-1))

        start = self.page_map.get(str(self.cfg.get("ui.start_page", "quote")), 0)
        self._go(start)

    # ------------------------------------------------ 页面按需构造
    def _ensure_page(self, key: str) -> QWidget | None:
        """取某页;没建过就现建,并把占位格换成它。

        启动时只建 _EAGER 里的页,其余等第一次切过去 —— 省启动时间和内存,
        页面自己的定时器/后台任务也不会在没人看的时候空转。
        """
        built = self.__dict__.get("_built")
        if built is None:
            return None
        page = built.get(key)
        if page is not None:
            return page
        cls = self._BUILDERS.get(key)
        if cls is None:
            return None
        page = cls()
        built[key] = page
        self._all_pages.append(page)
        self._wire_page(key, page)
        idx = self.page_map[key]
        ph = self.pages.widget(idx)
        was_current = self.pages.currentIndex() == idx
        self._building = True
        try:
            if ph is not None and ph is not page:
                self.pages.removeWidget(ph)
                ph.deleteLater()
            self.pages.insertWidget(idx, page)
            if was_current:
                self.pages.setCurrentIndex(idx)
        finally:
            self._building = False
        return page

    def _status(self, msg, timeout: int = 0) -> None:
        """状态栏统一入口:长消息截断,全文进 tooltip。

        行情源报错动辄一整段 HTTPSConnectionPool 堆栈,直接塞进状态栏
        会把整条底栏占满,还挤掉右边的行情延迟。
        """
        text = str(msg or "").replace("\n", " ").strip()
        bar = self.statusBar()
        if len(text) > 96:
            bar.setToolTip(text)
            text = text[:94].rstrip() + "…"
        else:
            bar.setToolTip("")
        bar.showMessage(text, timeout)

    def _wire_page(self, key: str, page) -> None:
        """页面信号接到主窗口。只有真造出来的页面才接线。"""
        if hasattr(page, "status"):
            page.status.connect(self._status)
        if key == "sector":
            page.open_stock.connect(self._goto_stock)
        elif key == "quote":
            page.open_sector.connect(self._goto_sector)
        elif key == "risk":
            page.goto_quote.connect(self._goto_stock)
        elif key == "settings":
            page.settings_changed.connect(self._apply_settings)

    def _on_page_changed(self, idx: int) -> None:
        if self._building or not (0 <= idx < len(NAV)):
            return
        self._ensure_page(NAV[idx][1])

    def __getattr__(self, name: str):
        """`win.xxx_page` 保持原用法:取到就返回,没建就先建。

        自检脚本、跳转逻辑都按 `属性名 = key + '_page'` 取页面,这里统一兜住,
        免得每处都写一次 _ensure_page。
        """
        if name.endswith("_page") and len(name) > 6:
            d = object.__getattribute__(self, "__dict__")
            built = d.get("_built")
            pmap = d.get("page_map") or {}
            key = name[:-5]
            if built is not None and key in pmap:
                return self._ensure_page(key)
        raise AttributeError(f"{type(self).__name__!s} 没有属性 {name}")

    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(SIDEBAR_W)
        self.side = side
        sv = QVBoxLayout(side)
        sv.setContentsMargins(12, 16, 12, 12)
        sv.setSpacing(0)

        # 品牌块:标记单独一行,下面才是完整软件名 —— 名字 14px 排一行
        # 在 196px 侧栏里放得下;跟 30px 的标记挤同一行就会溢出。
        brand = QVBoxLayout()
        brand.setSpacing(7)
        mark = QLabel("G")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(f"background:{ACCENT};color:#ffffff;border-radius:9px;"
                           "font-size:16px;font-weight:700;")
        brand.addWidget(mark, 0, Qt.AlignLeft)

        logo = QLabel(APP_TITLE)
        logo.setObjectName("AppName")
        brand.addWidget(logo)
        sv.addLayout(brand)

        sub = QLabel("本地 A 股量化")
        sub.setObjectName("AppSub")
        sub.setContentsMargins(1, 3, 0, 0)
        sv.addWidget(sub)
        sv.addSpacing(10)

        # 导航区可滚动:21 个页面在 780px 高的窗口里放不下,
        # 以前是直接溢出侧栏 —— 后 8 个页面根本看不见也点不到。
        self.nav_scroll = QScrollArea()
        self.nav_scroll.setObjectName("NavScroll")
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setFrameShape(QFrame.NoFrame)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        holder = QWidget()
        holder.setObjectName("NavHolder")
        nav_lay = QVBoxLayout(holder)
        nav_lay.setContentsMargins(0, 2, 0, 2)
        nav_lay.setSpacing(1)
        self.nav_scroll.setWidget(holder)
        # 视口默认会画自己的底色,在深色侧栏上会露一块明显的方块
        self.nav_scroll.viewport().setAutoFillBackground(False)
        self.nav_scroll.viewport().setStyleSheet("background: transparent;")
        sv.addWidget(self.nav_scroll, 1)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._group_heads: dict[str, QPushButton] = {}
        self._group_buttons: dict[str, list[QPushButton]] = {}
        self._folded: set[str] = set(self.cfg.get("ui.nav_folded") or [])

        idx = 0
        for gname, items in NAV_GROUPS:
            head = QPushButton(gname)
            head.setObjectName("NavGroup")
            head.setCursor(Qt.PointingHandCursor)
            head.setCheckable(False)
            head.clicked.connect(lambda _=False, g=gname: self._toggle_group(g))
            nav_lay.addWidget(head)
            self._group_heads[gname] = head
            mine: list[QPushButton] = []
            for name, key, hint in items:
                btn = QPushButton(name)
                btn.setObjectName("Nav")
                btn.setCheckable(True)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setToolTip(f"{name} · {hint}\n"
                               + (f"Ctrl+{idx + 1} 直达" if idx < 9 else "Ctrl+K 快跳"))
                btn.clicked.connect(lambda _=False, i=idx: self._go(i))
                self.nav_group.addButton(btn, idx)
                nav_lay.addWidget(btn)
                self._nav_buttons[key] = btn
                mine.append(btn)
                idx += 1
            self._group_buttons[gname] = mine
        nav_lay.addStretch(1)
        self._sync_group_heads()

        sv.addSpacing(8)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{BORDER};")
        sv.addWidget(line)
        sv.addSpacing(6)

        self.btn_help = QPushButton("帮助 · 口径 / 函数库")
        self.btn_help.setObjectName("Ghost")
        self.btn_help.setToolTip("指标说明、策略函数库、风控口径、快捷键(F1)")
        self.btn_help.setCursor(Qt.PointingHandCursor)
        self.btn_help.clicked.connect(self._open_help)
        sv.addWidget(self.btn_help)
        self.side_badge = QLabel("休市")
        self.side_badge.setObjectName("Badge")
        self.side_badge.setAlignment(Qt.AlignCenter)
        sv.addWidget(self.side_badge)
        self.side_status = QLabel("")
        self.side_status.setWordWrap(True)
        self.side_status.setStyleSheet(f"color:{TEXT_MUT};font-size:11px;")
        sv.addWidget(self.side_status)
        ver = QLabel(f"v{VERSION} · 构建 {_build_stamp()}")
        ver.setStyleSheet("color:#414c5a;font-size:11px;")
        ver.setToolTip("构建时间取程序自身文件的时间;和 exe 的修改时间一致")
        sv.addWidget(ver)

        # 记住是否折叠了侧栏(折叠后把 200px 全给数据区)
        if bool(self.cfg.get("ui.sidebar_hidden", False)):
            QTimer.singleShot(0, lambda: self._toggle_sidebar(force_show=False))
        return side

    # ------------------------------------------------------------ 导航分组
    def _toggle_group(self, gname: str) -> None:
        """折叠 / 展开一个分组。折叠的不删控件,只隐藏。"""
        if gname in self._folded:
            self._folded.discard(gname)
        else:
            # 当前页就在这一组里时不许折叠,否则选中项跟着消失
            cur = NAV[self.pages.currentIndex()][1] if self.pages.count() else ""
            if NAV_GROUP_OF.get(cur) == gname:
                # 明说一句,不然点了没反应像是坏了
                self.statusBar().showMessage(
                    f"「{gname}」里有当前页面,先切到别的页再折叠", 3000)
                return
            self._folded.add(gname)
        for b in self._group_buttons.get(gname, []):
            b.setVisible(gname not in self._folded)
        self._sync_group_heads()
        try:
            self.cfg.set("ui.nav_folded", sorted(self._folded), save=True)
        except Exception:
            pass

    def _sync_group_heads(self) -> None:
        for gname, head in self._group_heads.items():
            folded = gname in self._folded
            head.setText(("▸ " if folded else "▾ ") + gname)
            head.setProperty("folded", folded)
            head.style().unpolish(head)
            head.style().polish(head)
            n = len(self._group_buttons.get(gname, []))
            head.setToolTip(f"{gname} · {n} 个页面 · 点一下{'展开' if folded else '折叠'}")

    def _unfold_of(self, key: str) -> None:
        """跳到的页面如果被折叠了,先把它那一组展开。"""
        gname = NAV_GROUP_OF.get(key)
        if gname and gname in self._folded:
            self._folded.discard(gname)
            for b in self._group_buttons.get(gname, []):
                b.setVisible(True)
            self._sync_group_heads()
            try:
                self.cfg.set("ui.nav_folded", sorted(self._folded), save=True)
            except Exception:
                pass

    def _toggle_sidebar(self, force_show: bool | None = None) -> None:
        """折叠/展开整个侧栏:标题栏那一列让给数据区。"""
        if not hasattr(self, "side"):
            return
        show = (not self.side.isVisible()) if force_show is None else bool(force_show)
        self.side.setVisible(show)
        if hasattr(self, "btn_menu"):
            self.btn_menu.setText("☰" if not show else "◧")
            self.btn_menu.setToolTip(
                "展开侧栏(Ctrl+B)" if not show else "折叠侧栏,把宽度让给数据(Ctrl+B)")
        try:
            self.cfg.set("ui.sidebar_hidden", not show, save=True)
        except Exception:
            pass

    def _build_topbar(self) -> QFrame:
        top = QFrame()
        top.setObjectName("TopBar")
        self.topbar = top
        th = QHBoxLayout(top)
        th.setContentsMargins(10, 6, 10, 6)
        th.setSpacing(8)

        # 收起/展开侧栏:窗口不宽时把 196px 让给表格
        self.btn_menu = QPushButton("◧")
        self.btn_menu.setObjectName("IconBtn")
        self.btn_menu.setFixedSize(26, 26)
        self.btn_menu.setCursor(Qt.PointingHandCursor)
        self.btn_menu.setToolTip("折叠侧栏,把宽度让给数据(Ctrl+B)")
        self.btn_menu.clicked.connect(lambda: self._toggle_sidebar())
        th.addWidget(self.btn_menu)

        box = QVBoxLayout()
        box.setSpacing(0)
        self.page_title = QLabel(NAV[0][0])
        self.page_title.setObjectName("PageTitle")
        self.page_hint = QLabel(NAV[0][2])
        self.page_hint.setObjectName("PageHint")
        box.addWidget(self.page_title)
        box.addWidget(self.page_hint)
        th.addLayout(box)
        th.addSpacing(4)
        th.addStretch(1)

        # 页面快跳:21 个页面翻侧栏慢,这里打个字就到
        self.btn_jump = QPushButton("跳转")
        self.btn_jump.setObjectName("Ghost")
        self.btn_jump.setToolTip("按名字跳到任意页面(Ctrl+K)")
        self.btn_jump.setCursor(Qt.PointingHandCursor)
        self.btn_jump.clicked.connect(self._open_jump)
        th.addWidget(self.btn_jump)

        self.chips = [IndexChip(n) for n in ("上证", "深成", "创业板", "沪深300")]
        for c in self.chips:
            th.addWidget(c)

        self.btn_reset = QPushButton("重置布局")
        self.btn_reset.setObjectName("Ghost")
        self.btn_reset.setToolTip("把所有页面的卡片尺寸恢复默认(Ctrl+Shift+R)")
        self.btn_reset.clicked.connect(self._reset_layout)
        th.addWidget(self.btn_reset)

        self.clock = QLabel("")
        self.clock.setObjectName("Clock")
        self.clock.setMinimumWidth(150)
        th.addWidget(self.clock)
        return top

    def _open_jump(self) -> None:
        """Ctrl+K:输入页面名直达。"""
        try:
            from .quickjump import QuickJump
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"快跳面板打不开:{e}", 4000)
            return
        dlg = QuickJump(NAV, NAV_GROUP_OF, self)
        if dlg.exec() and dlg.key and dlg.key in self.page_map:
            self._go(self.page_map[dlg.key])

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窄窗口时先让装饰物消失,别跟表格抢横向空间。"""
        super().resizeEvent(event)
        w = self.width()
        if hasattr(self, "btn_jump"):
            # 很窄时按钮字换成符号,省出横向空间给表格
            self.btn_jump.setText("跳转" if w >= 1180 else "⌕")
            self.btn_reset.setVisible(w >= 1320)
            for c in getattr(self, "chips", []):
                c.setVisible(w >= 1240)
            if hasattr(self, "page_hint"):
                self.page_hint.setVisible(w >= 1100)
            if hasattr(self, "clock"):
                self.clock.setVisible(w >= 1000)

    # ------------------------------------------------------------ 行情
    def _hub_codes(self) -> list[str]:
        """行情订阅 = 自选股。"""
        codes = list(self.quote_page.codes)
        seen: set[str] = set()
        out: list[str] = []
        for c in codes:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _sync_hub_codes(self) -> None:
        """持仓会变(自动交易/手动买卖),定期把新持仓码并进订阅。"""
        if not hasattr(self, "hub"):
            return
        codes = self._hub_codes()
        if codes != self._subscribed:
            self._subscribed = codes
            self.hub.set_codes(codes)

    def _start_hub(self):
        codes = self._hub_codes()
        self._subscribed = list(codes)
        interval = float(self.cfg.get("refresh_seconds.quote", 1))
        self.hub = QuoteHub(codes, interval, self)
        self.hub.quotes_ready.connect(self._on_quotes)
        self.hub.indexes_ready.connect(self._on_indexes)
        self.hub.feed.connect(self._on_feed)
        self.hub.status.connect(lambda m: self._status(m, 8000))
        self.hub.start()
        # 5 秒对一次账:SQLite 查询很便宜,不必每帧都算
        self._sub_timer = QTimer(self)
        self._sub_timer.setInterval(5000)
        self._sub_timer.timeout.connect(self._sync_hub_codes)
        self._sub_timer.start()

    def _on_feed(self, st: dict) -> None:
        """状态栏右侧:数据源 + 本次往返 + p50。"""
        self._feed = st
        src = st.get("source") or "--"
        lat = float(st.get("latency_ms") or 0)
        p50 = float(st.get("p50") or 0)
        ok = bool(st.get("ok"))
        col = TEXT_MUT if ok else DOWN
        text = f"行情 {src} · {lat:.0f}ms · p50 {p50:.0f}ms"
        mf = st.get("mf") or {}
        if mf.get("cached"):
            text += f" · 资金 {mf.get('fresh', 0)}/{mf.get('cached')}"
        if not ok:
            text = f"行情 {src} · 延迟 {lat:.0f}ms · 重试中"
        self.feed_label.setText(text)
        self.feed_label.setStyleSheet(f"color:{col};font-size:11px;"
                                      "font-family:Consolas;padding-right:10px;")
        self.quote_page.set_feed_status(f"{src} {lat:.0f}ms" if ok else "数据源重试中")
        try:
            from ..data import health
            health.mark("行情", ok, f"{src} {lat:.0f}ms")
        except Exception:
            pass

    def _on_quotes(self, data: dict) -> None:
        self.quote_page.update_quotes(data)

    def _on_indexes(self, rows: list) -> None:
        want = {"000001": 0, "399001": 1, "399006": 2, "000300": 3}
        for r in rows:
            i = want.get(r.get("code"))
            if i is not None:
                self.chips[i].set(r.get("name") or "", r.get("price"), r.get("change_pct"))

    def _tick(self) -> None:
        st = session_state()
        self.clock.setText(f"{time.strftime('%m-%d %H:%M:%S')}  ·  "
                           f"{SESSION_LABEL.get(st, st)}")
        self.side_status.setText(f"自选 {len(self.quote_page.codes)} 只\n"
                                 "Ctrl+K 跳转 · F5 刷新")
        self.side_badge.setText(SESSION_LABEL.get(st, st))

    # ------------------------------------------------------------ 交互
    def _go(self, idx: int) -> None:
        idx = max(0, min(len(NAV) - 1, int(idx)))
        page = self._ensure_page(NAV[idx][1])
        self.pages.setCurrentIndex(idx)
        _, key, hint = NAV[idx]
        # 目标页所在分组如果是折叠的,先展开,否则选中项根本不在屏幕上
        self._unfold_of(key)
        btn = self.nav_group.button(idx)
        if btn is not None:
            if not btn.isChecked():
                btn.setChecked(True)
            # 长导航里保证当前项滚进可视区(20+ 页面时这步很关键)
            if hasattr(self, "nav_scroll"):
                try:
                    self.nav_scroll.ensureWidgetVisible(btn, 0, 12)
                except Exception:
                    pass
        self.page_title.setText(NAV[idx][0])
        # 带上分组名:侧栏收起时也知道自己在哪个功能区
        g = NAV_GROUP_OF.get(key, "")
        self.page_hint.setText(f"{g} · {hint}" if g else hint)
        # 慢页面(风险档位那种要建池子的)首次进来就起任务,
        # 不指望 Qt 一定派发 showEvent
        fn = getattr(page, "ensure_loaded", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
        # 起始页每切一次都整份写 config.json 太浪费,攒到退出时一起落盘
        self.cfg.set("ui.start_page", key)

    def _step_page(self, delta: int) -> None:
        """Ctrl+Tab / Ctrl+Shift+Tab:按 NAV 顺序翻页。"""
        cur = self.pages.currentIndex()
        self._go((cur + delta) % len(NAV))

    def _refresh_current(self) -> None:
        page = self.pages.currentWidget()
        for name in ("refresh_now", "refresh"):
            fn = getattr(page, name, None)
            if callable(fn):
                try:
                    fn()
                    self.statusBar().showMessage("已刷新当前页", 3000)
                except Exception as e:  # noqa: BLE001
                    self.statusBar().showMessage(f"刷新失败:{e}", 4000)
                return
        self.statusBar().showMessage("这一页没有可刷新的动作", 3000)

    def _open_help(self) -> None:
        try:
            dlg = HelpDialog(self)
            dlg.exec()
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"帮助窗口打不开:{e}", 4000)

    def _reset_layout(self) -> None:
        """一键把全部页面的分栏恢复默认(不只是当前页)。"""
        for _n, _key, _h in NAV:          # 要全量重置,就把页面都建出来
            self._ensure_page(_key)
        n = 0
        for i in range(self.pages.count()):
            w = self.pages.widget(i)
            if w is None:
                continue
            for b in w.findChildren(Board):
                b.reset()
                n += 1
        self.statusBar().showMessage(f"已把 {n} 处分栏恢复默认比例", 4000)

    def _goto_stock(self, code: str) -> None:
        """板块页双击成分股 → 行情页选中它(不写自选)。"""
        if not code:
            return
        self._go(self.page_map["quote"])
        self.quote_page.select_code(code)

    def _goto_eval(self, code: str) -> None:
        """公司分析页点「评估报告」→ 公司评估页带着代码生成。"""
        if not code:
            return
        self._go(self.page_map["company_eval"])
        self.company_eval_page.set_code(code)

    def _goto_sector(self, name: str) -> None:
        """行情页双击所属板块 → 板块页定位到它。"""
        if not name:
            return
        self._go(self.page_map["sector"])
        self.sector_page.focus_board(name)

    def _apply_settings(self) -> None:
        interval = float(self.cfg.get("refresh_seconds.quote", 1))
        if hasattr(self, "hub"):
            self.hub.set_interval(interval)
            self._subscribed = self._hub_codes()
            self.hub.set_codes(self._subscribed)
        self.quote_page.reload_watchlist()

    @staticmethod
    def _exit_now(code: int = 0) -> None:
        """立刻结束进程,跳过 CRT / DLL 收尾(Qt 的 TLS 清理会 fail-fast)。

        坑:伪句柄 `GetCurrentProcess()`(-1)传给 TerminateProcess 会
        ERROR_INVALID_HANDLE,退化成 os._exit,那就又把 Qt 收尾跑了一遍。
        必须先 OpenProcess(PROCESS_TERMINATE) 拿真句柄。
        """
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x0001, False, os.getpid())   # PROCESS_TERMINATE
            if h:
                k32.TerminateProcess(h, int(code))
        except Exception:
            pass
        os._exit(int(code))

    def closeEvent(self, event) -> None:  # noqa: N802
        # 只做「必须落盘」的几件事,然后结束进程。
        # 不在这里逐个停页面的 hub / 掐线程:后台线程正在跑原生代码时被拆,
        # 会以 0xC0000409 fail-fast 崩(现象:关窗偶尔崩、偶尔干净)。
        # TerminateProcess 不跑 DLL 卸载通知,进程直接没,线程跟着一起走。
        try:
            self._clock.stop()
        except Exception:
            pass
        try:
            self._sub_timer.stop()
        except Exception:
            pass
        try:
            self._learn_sched.stop()
        except Exception:
            pass
        try:                     # 退出前截断 WAL,否则 -wal 会一直留几个 MB
            from ..data.store import store
            store().checkpoint()
        except Exception:
            pass
        self._save_geometry()
        # 自测脚本要在关窗后继续断言,设 GRAY_NO_HARD_EXIT=1 走下面的软退出
        if os.environ.get("GRAY_NO_HARD_EXIT") != "1":
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            self._exit_now(0)

        # ---- 以下只给自测用:按顺序收线程,再交给 Qt 正常收尾 ----
        for page in list(getattr(self, "_all_pages", []) or []):
            fn = getattr(page, "shutdown", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
        for page in (self.macro_page, self.forecast_page,
                     self.pick_page,
                     self.risk_page, self.world_page,
                     self.health_page, self.pattern_page,
                     self.ta_page, self.grid_page, self.lhb_page,
                     self.strategy_page, self.news_page, self.alert_page):
            try:
                page.stop()
            except Exception:
                pass
        try:
            if hasattr(self, "hub"):
                self.hub.stop()
                self.hub.wait(1500)
        except Exception:
            pass
        # 等后台任务收尾;等不动的直接掐掉,否则 Qt 报
        # "QThread: Destroyed while thread is still running" 并以 0xC0000409 退出
        pending = []
        for page in self._all_pages:
            pending.extend(list(getattr(page, "_tasks", []) or []))
        deadline = time.monotonic() + 3.0
        for t in pending:
            try:
                if t.isRunning():
                    t.wait(int(max(200, (deadline - time.monotonic()) * 1000)))
            except Exception:
                pass
        for t in pending:
            try:
                if t.isRunning():
                    t.blockSignals(True)
                    t.terminate()
                    t.wait(300)
            except Exception:
                pass
        # 兜底:不在页面 _tasks 里的线程(加密 hub、父对象为空的 TaskRunner)
        # 在这里统一收掉,否则解释器退出会 0xC0000409
        try:
            from ..engine.hub import stop_live
            stop_live(1500)
        except Exception:
            pass
        super().closeEvent(event)