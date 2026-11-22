# -*- coding: utf-8 -*-
"""pyqtgraph 图表:K 线 + 成交量、分时、资金流、净值曲线。

A 股配色:涨红 #ef4444,跌绿 #22c55e。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPicture
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QStackedWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from .theme import ACCENT, BG2, DOWN, FLAT, GRID, TEXT_DIM, TEXT_MUT, UP

pg.setConfigOptions(antialias=True, background="#0d141d", foreground=TEXT_MUT)


def _pen(color, width: float = 1.2, style=None) -> QPen:
    """cosmetic 画笔:宽度按屏幕像素算,不随数据坐标缩放。

    pyqtgraph 的数据坐标里 1 单位 = 1 根 K 线 / 1 元。非 cosmetic 的笔在这种
    坐标系下「宽 1.3」= 1.3 根 K 线宽:线糊成柱、影线糊成实体。mkPen 默认就是
    cosmetic=True,这里显式写死,顺手统一收口所有画笔。
    """
    return pg.mkPen(color, width=width, style=style, cosmetic=True)


def _hex_rgb(color: str) -> tuple[int, int, int]:
    """'#rrggbb' → (r,g,b);认不出来就当灰。半透明填充要拆成 RGB。"""
    s = str(color or "").lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return 148, 163, 184
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 148, 163, 184


class CandlestickItem(pg.GraphicsObject):
    """用 QPicture 预渲染的蜡烛图(比逐根 addItem 快得多)。

    两个必须记住的坑:
      * 画笔**必须是 cosmetic**。QPicture 里的坐标就是数据单位(x=第几根,y=元),
        普通 1.0 宽的笔在这种坐标系下等于「1 根 K 线那么宽」,
        影线会被画成从最低到最高的一整块实心柱子——看起来就是「只有实体没有影线」。
      * 实体矩形必须用规范化的 QRectF(y=下沿, h>0),别指望 Qt 帮你翻正。

    宽度按**像素**反算:实体占每格 0.68,格宽 < 2px 时只画 1px 竖线。
    """

    def __init__(self, df: pd.DataFrame, colors: tuple[str, str] | None = None,
                 style: str = "candle"):
        super().__init__()
        self.df = df
        self._up_color, self._down_color = colors or (UP, DOWN)
        self._style = style if style in ("candle", "bar") else "candle"
        self._pic = QPicture()
        self._rect = QRectF()
        self._px_per_unit = 4.0      # 由 set_px_per_unit 更新
        self._o = self._h = self._l = self._c = np.zeros(0)
        self._up = np.zeros(0, dtype=bool)
        self._prepare()
        self._build()

    def _prepare(self) -> None:
        df = self.df
        if df is None or df.empty:
            self._o = self._h = self._l = self._c = np.zeros(0)
            self._up = np.zeros(0, dtype=bool)
            return
        self._o = df["open"].to_numpy(dtype=float)
        self._h = df["high"].to_numpy(dtype=float)
        self._l = df["low"].to_numpy(dtype=float)
        self._c = df["close"].to_numpy(dtype=float)
        self._up = self._c >= self._o

    def set_px_per_unit(self, v: float) -> None:
        """每 1 个 x 单位多少像素。变化不大就不重建。"""
        v = max(0.2, float(v or 4.0))
        if abs(v - self._px_per_unit) / self._px_per_unit < 0.08:
            return
        self._px_per_unit = v
        self._build()
        self.prepareGeometryChange()
        self.update()

    @staticmethod
    def _pen(color: str) -> QPen:
        pen = QPen(QColor(color))
        pen.setCosmetic(True)          # 关键:宽度按屏幕像素,不随数据坐标缩放
        pen.setWidthF(1.0)
        pen.setJoinStyle(Qt.MiterJoin)
        return pen

    def _build(self):
        pic = QPicture()
        p = QPainter(pic)
        try:
            p.setRenderHint(QPainter.Antialiasing, False)   # 细线不要糊
            cell_px = self._px_per_unit
            body_px = max(1.0, cell_px * 0.68)
            half = min((body_px / cell_px) / 2.0, 0.48)
            draw_body = body_px >= 2.0
            o, h, l, c, up = self._o, self._h, self._l, self._c, self._up
            for i in range(len(o)):
                oi, hi, li, ci = float(o[i]), float(h[i]), float(l[i]), float(c[i])
                color = self._up_color if up[i] else self._down_color
                p.setPen(self._pen(color))
                # 上下影线:cosmetic 笔 → 屏幕上永远是 1 像素
                p.drawLine(QPointF(i, li), QPointF(i, hi))
                if self._style == "bar":
                    # 美国线:左侧短横=开盘,右侧短横=收盘,不画实体
                    if cell_px >= 3:
                        p.drawLine(QPointF(i - half, oi), QPointF(i, oi))
                        p.drawLine(QPointF(i, ci), QPointF(i + half, ci))
                    continue
                if not draw_body:
                    continue
                top, bot = max(oi, ci), min(oi, ci)
                height = top - bot
                if height <= 1e-9:
                    # 一字板:一根横线
                    p.drawLine(QPointF(i - half, ci), QPointF(i + half, ci))
                    continue
                p.setBrush(QBrush(QColor(color)))
                p.drawRect(QRectF(i - half, bot, 2 * half, height))
        finally:
            p.end()
        self._pic = pic
        self._rect = QRectF(pic.boundingRect())

    def paint(self, p, *args):
        p.drawPicture(0, 0, self._pic)

    def boundingRect(self) -> QRectF:
        return self._rect

    def set_data(self, df: pd.DataFrame):
        self.df = df
        self._prepare()
        self._build()
        self.prepareGeometryChange()
        self.update()


class KLineWidget(QWidget):
    """上:K 线 + MA;下:成交量。鼠标悬停显示该根的开高低收。

    colors=(涨色, 跌色):A 股默认红涨绿跌;加密页传 (绿, 红) 切国际惯例。
    cursor_moved(int):鼠标落在第几根上,多图联动靠它广播。
    """

    cursor_moved = Signal(int)

    def __init__(self, parent=None, colors: tuple[str, str] | None = None):
        super().__init__(parent)
        self._up_color, self._down_color = colors or (UP, DOWN)
        self._style = "candle"      # candle | bar | close | area
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.info = QLabel("")
        self.info.setMinimumHeight(20)
        self.info.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;padding:0 4px;")
        lay.addWidget(self.info, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw, 4)
        self.vlw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.vlw, 1)
        self._init_plots()

    def _init_plots(self):
        self.price_plot = self.glw.addPlot(row=0, col=0)
        self.price_plot.showGrid(x=True, y=True, alpha=0.15)
        self.price_plot.getAxis("left").setTextPen(TEXT_DIM)
        self.price_plot.getAxis("bottom").setTextPen(TEXT_DIM)
        self.price_plot.hideAxis("bottom")
        # 价格轴放右边,和券商一致
        self.price_plot.showAxis("right")
        self.price_plot.getAxis("right").setTextPen(TEXT_DIM)
        self.price_plot.getAxis("right").setWidth(64)
        self.price_plot.hideAxis("left")
        self.price_plot.setMouseEnabled(x=True, y=False)
        # Y 轴跟着「可见的那几十根」自动收放,缩放时不会留下大片空白
        self.price_plot.vb.setAutoVisible(y=True)

        self.vol_plot = self.vlw.addPlot(row=0, col=0)
        self.vol_plot.showGrid(x=True, y=True, alpha=0.15)
        self.vol_plot.getAxis("left").setTextPen(TEXT_DIM)
        self.vol_plot.getAxis("bottom").setTextPen(TEXT_DIM)
        self.vol_plot.getAxis("left").setWidth(58)
        self.vol_plot.setMouseEnabled(x=True, y=False)
        self.vol_plot.vb.setAutoVisible(y=True)
        # 成交量与主图共用 X 轴。不用 setXLink:pyqtgraph 的 link 会给每侧
        # 各自补 padding,实测错开约 2 根 K 线,蜡烛和量柱对不上。手动镜像。

        # 十字光标
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                      pen=_pen("#64748b", 1, style=Qt.DashLine))
        self._hlabel = pg.TextItem("", anchor=(1, 1), color="#e5e7eb",
                                   fill=pg.mkBrush(30, 41, 59, 220))
        for w in (self._vline, self._hlabel):
            self.price_plot.addItem(w, ignoreBounds=True)
            w.hide()
        self.price_plot.scene().sigMouseMoved.connect(self._on_mouse)
        self.price_plot.vb.sigRangeChanged.connect(lambda *_: self._on_x_range("price"))
        self.vol_plot.vb.sigRangeChanged.connect(lambda *_: self._on_x_range("vol"))

        self.candle = None
        self.ma_curves = {}
        self.vol_bars = None
        self._dates: list[str] = []
        self._df = None
        self._hi = np.zeros(0)
        self._lo = np.zeros(0)
        self._vol = np.zeros(0)
        self._updating = False       # X 镜像/量程重入保护

    def _on_mouse(self, pos):
        if self._df is None or self._df.empty:
            return
        vb = self.price_plot.vb
        if not self.price_plot.sceneBoundingRect().contains(pos):
            return
        pt = vb.mapSceneToView(pos)
        i = int(round(pt.x()))
        df = self._df
        if i < 0 or i >= len(df):
            self._vline.hide()
            self._hlabel.hide()
            return
        r = df.iloc[i]
        self._vline.setPos(i)
        self._vline.show()
        self._hlabel.setPos(len(df) - 1, pt.y())
        self._hlabel.setText(f"{pt.y():.2f}")
        self._hlabel.show()
        o, h, l, c = (float(r["open"]), float(r["high"]),
                      float(r["low"]), float(r["close"]))
        prev = float(df.iloc[i - 1]["close"]) if i > 0 else o
        chg = (c / prev - 1) * 100 if prev else 0.0
        vol = float(r.get("volume") or 0)
        amt = float(r.get("amount") or 0)
        parts = [f"<span style='color:#94a3b8'>{self._dates[i]}</span>",
                 f"开 {o:.2f}", f"高 {h:.2f}", f"低 {l:.2f}",
                 f"收 <b style='color:{self._up_color if c >= prev else self._down_color}'>{c:.2f}</b>",
                 f"<span style='color:{self._up_color if chg >= 0 else self._down_color}'>{chg:+.2f}%</span>",
                 f"量 {vol:,.0f} {getattr(self, '_unit', '手')}"]
        if amt:
            parts.append(f"额 {amt / 1e8:.2f} 亿")
        self.info.setText("  ·  ".join(parts))
        self.cursor_moved.emit(i)

    def sync_cursor(self, i: int) -> None:
        """别的图把光标移到这里:只画竖线,不改自己的 info 文本。

        用竖线而不是把整块信息都同步,是因为各图标的周期/标的可能不同,
        「同一个下标」在四张图上未必是同一天 —— 同一标的多周期时才有意义。
        """
        if self._df is None or self._df.empty:
            return
        if i < 0 or i >= len(self._df):
            self._vline.hide()
            return
        self._vline.setPos(i)
        self._vline.show()

    def set_style(self, kind: str) -> str:
        """切换图表类型。返回真正生效的那个(认不出来的退回蜡烛)。"""
        kind = (kind or "candle").lower()
        if kind not in ("candle", "bar", "close", "area"):
            kind = "candle"
        self._style = kind
        if self._df is not None and not self._df.empty:
            self.plot(self._df, unit=getattr(self, "_unit", "手"))
        return kind

    def plot(self, df: pd.DataFrame, title: str = "", unit: str = "手",
             overlay: list | None = None):
        self._unit = unit
        self.price_plot.clear()
        self.vol_plot.clear()
        self.info.setText("")
        if df is None or df.empty:
            return
        df = df.reset_index(drop=True)
        self._df = df
        self._dates = [str(d)[:16] for d in df["datetime"]]
        self._hi = df["high"].to_numpy(dtype=float)
        self._lo = df["low"].to_numpy(dtype=float)
        x = np.arange(len(df))
        if self._style in ("close", "area"):
            # 收盘线 / 山形:不画蜡烛,只连收盘价,山形再填一层半透明
            close = df["close"].to_numpy(dtype=float)
            if self._style == "area":
                base = float(np.nanmin(self._lo)) if len(self._lo) else float(close.min())
                fill = pg.FillBetweenItem(
                    pg.PlotDataItem(x, close),
                    pg.PlotDataItem(x, np.full_like(close, base)),
                    brush=pg.mkBrush(*_hex_rgb(ACCENT), 46))
                fill.setZValue(-5)
                self.price_plot.addItem(fill, ignoreBounds=True)
            self.price_plot.plot(x, close, pen=_pen(ACCENT, 1.6))
            self.candle = None
        else:
            self.candle = CandlestickItem(df, (self._up_color, self._down_color),
                                          style=self._style)
            self.price_plot.addItem(self.candle)

        for n, color in ((5, "#eab308"), (20, "#38bdf8"), (60, "#a855f7")):
            if len(df) >= n:
                ma = df["close"].rolling(n, min_periods=1).mean()
                self.price_plot.plot(x, ma.to_numpy(), pen=_pen(color, 1.4),
                                     name=f"MA{n}")

        span = max(60, min(120, len(df)))
        self.price_plot.vb.enableAutoRange(x=False, y=True)
        self.price_plot.setXRange(len(df) - span, len(df) - 1, padding=0.01)

        up_mask = (df["close"] >= df["open"]).to_numpy()
        vol = df["volume"].to_numpy(dtype=float)
        self._vol = vol
        bg = pg.BarGraphItem(x=x, height=vol, width=0.68,
                             brushes=[pg.mkBrush(self._up_color if u else self._down_color)
                                      for u in up_mask])
        self.vol_plot.addItem(bg)
        self.vol_plot.vb.enableAutoRange(x=False, y=True)

        # 重绘后恢复十字光标
        self._vline = pg.InfiniteLine(angle=90, movable=False,
                                      pen=_pen("#64748b", 1, style=Qt.DashLine))
        self._hlabel = pg.TextItem("", anchor=(1, 1), color="#e5e7eb",
                                   fill=pg.mkBrush(30, 41, 59, 220))
        for w in (self._vline, self._hlabel):
            self.price_plot.addItem(w, ignoreBounds=True)
            w.hide()

        if overlay:
            self.draw_overlay(overlay)

        self._on_x_range()
        # 布局/首次显示时控件宽度还没定,宽度算错会让蜡烛偏细;等一拍重算。
        QTimer.singleShot(0, self._on_x_range)

    def draw_overlay(self, items: list) -> int:
        """在主图上画分析层。返回画了几笔。

        items 里的每条是一个 dict,支持这些类型(坐标一律用 K 线下标 x + 价格 y):
          {"type":"line","points":[(x,y),...],"color":"#fff","width":1.2,"dash":True}
          {"type":"box","x0","x1","y0","y1","color","alpha":0.12}
          {"type":"hline","y":...,"color","dash":True}
          {"type":"tag","x","y","text","color"}
        只画,不做任何数值判断 —— 判定都留在引擎层,免得两边各算一套。
        """
        if not items:
            return 0
        drawn = 0
        for it in items:
            kind = it.get("type")
            color = it.get("color", ACCENT)
            width = float(it.get("width", 1.2))
            dash = it.get("dash", False)
            try:
                if kind == "line":
                    pts = it.get("points") or []
                    if len(pts) < 2:
                        continue
                    xs = np.array([float(p[0]) for p in pts])
                    ys = np.array([float(p[1]) for p in pts])
                    self.price_plot.plot(xs, ys,
                                         pen=_pen(color, width,
                                                  style=Qt.DashLine if dash else None))
                    drawn += 1
                elif kind == "box":
                    x0, x1 = float(it["x0"]), float(it["x1"])
                    y0, y1 = float(it["y0"]), float(it["y1"])
                    alpha = int(max(0.0, min(1.0, float(it.get("alpha", 0.12)))) * 255)
                    r, g, b = _hex_rgb(color)
                    rect = pg.QtWidgets.QGraphicsRectItem(min(x0, x1), min(y0, y1),
                                                          abs(x1 - x0) or 0.8,
                                                          abs(y1 - y0))
                    rect.setPen(_pen(color, 1.0, style=Qt.DashLine))
                    rect.setBrush(pg.mkBrush(r, g, b, alpha))
                    self.price_plot.addItem(rect)
                    drawn += 1
                elif kind == "hline":
                    line = pg.InfiniteLine(pos=float(it["y"]), angle=0, movable=False,
                                           pen=_pen(color, width,
                                                    style=Qt.DashLine if dash else None))
                    self.price_plot.addItem(line, ignoreBounds=True)
                    drawn += 1
                elif kind == "tag":
                    txt = pg.TextItem(str(it.get("text", "")), anchor=(0.5, 1),
                                      color=color,
                                      fill=pg.mkBrush(15, 23, 42, 210))
                    txt.setPos(float(it["x"]), float(it["y"]))
                    self.price_plot.addItem(txt, ignoreBounds=True)
                    drawn += 1
            except Exception:  # noqa: BLE001
                continue        # 单条画不出来不该拖垮整张图
        return drawn

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        QTimer.singleShot(0, self._on_x_range)

    def _on_x_range(self, src: str = "price") -> None:
        """X 轴可视范围变了:重算蜡烛像素宽 + 日期刻度 + Y 轴量程。

        为什么 Y 要手算:整段 K 线是**一个** CandlestickItem 图元,pyqtgraph 的
        「可见数据自动量程」切不开单个图元的 boundingRect,所以 autoVisible 无效,
        Y 永远取全量高低点(缩放后一大片空白)。这里直接对可见那几十根求高低点。
        """
        if self._df is None or self._df.empty:
            return
        if self._updating:
            return
        self._updating = True
        try:
            src_plot = self.price_plot if src == "price" else self.vol_plot
            other = self.vol_plot if src == "price" else self.price_plot
            (x0, x1), _ = src_plot.vb.viewRange()
            other.vb.setXRange(x0, x1, padding=0)      # 严格镜像,不加 padding
            span_x = max(1e-6, float(x1 - x0))
            width_px = max(80.0, float(self.price_plot.vb.width()))
            # 收盘线/山形没有蜡烛图元,但仍要按可见范围收放 Y 轴
            if self.candle is not None:
                self.candle.set_px_per_unit(width_px / span_x)
            self._apply_ticks(x0, x1)
            self._fit_y(x0, x1)
        finally:
            self._updating = False

    def _fit_y(self, x0: float, x1: float) -> None:
        n = len(self._hi)
        if n == 0:
            return
        lo = max(0, int(np.floor(x0)) - 1)
        hi = min(n - 1, int(np.ceil(x1)) + 1)
        if hi <= lo:
            return
        ymin = float(self._lo[lo:hi + 1].min())
        ymax = float(self._hi[lo:hi + 1].max())
        pad = (ymax - ymin) * 0.06 or max(0.01, ymax * 0.002)
        self.price_plot.vb.setYRange(ymin - pad, ymax + pad, padding=0)
        vmax = float(self._vol[max(0, lo):hi + 1].max()) if len(self._vol) else 0.0
        if vmax > 0:
            self.vol_plot.vb.setYRange(0, vmax * 1.12, padding=0)

    def _apply_ticks(self, x0: float, x1: float) -> None:
        n = len(self._dates)
        if n < 2:
            self.vol_plot.getAxis("bottom").setTicks([[]])
            return
        lo = max(0, int(np.floor(x0)))
        hi = min(n - 1, int(np.ceil(x1)))
        if hi <= lo:
            lo, hi = max(0, hi - 1), min(n - 1, lo + 1)
        count = 6
        step = max(1, int(round((hi - lo) / count)))
        ticks = [(i, self._dates[i][5:10]) for i in range(lo, hi + 1, step)]
        if not ticks or ticks[-1][0] != hi:
            ticks.append((hi, self._dates[hi][5:10]))
        self.vol_plot.getAxis("bottom").setTicks([ticks])


class TrendWidget(QWidget):
    """分时:价格线 + 均价线 + 昨收基准。取不到数据时给出明确提示,不留空白坐标轴。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.hint = QLabel("")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.hint.hide()
        lay.addWidget(self.hint)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.plot = self.glw.addPlot(row=0, col=0)
        self.plot.showGrid(x=True, y=True, alpha=0.07)
        self.plot.getAxis("left").setTextPen(TEXT_MUT)
        self.plot.getAxis("bottom").setTextPen(TEXT_MUT)

    def plot_trend(self, data: dict):
        self.plot.clear()
        bars = (data or {}).get("bars") or []
        if not bars:
            self.hint.setText("分时数据暂不可用(数据源限流),自动重试中…")
            self.hint.show()
            return
        self.hint.hide()
        pre = data.get("pre_close")
        price = np.array([b["price"] for b in bars], dtype=float)
        x = np.arange(len(price))
        if pre:
            self.plot.addLine(y=pre, pen=_pen(TEXT_DIM, 1, style=Qt.DashLine))
        color = UP if pre and price[-1] >= pre else DOWN
        self.plot.plot(x, price, pen=_pen(color, 1.6))
        avg = [b.get("avg") for b in bars]
        if any(a for a in avg):
            self.plot.plot(x, np.array([a or np.nan for a in avg], dtype=float),
                           pen=_pen("#d9a441", 1.1))
        if pre:
            self.plot.setYRange(min(pre, price.min()) * 0.998, max(pre, price.max()) * 1.002)
        ticks = [(i, bars[i]["t"][11:16]) for i in range(0, len(bars), max(1, len(bars) // 5))]
        self.plot.getAxis("bottom").setTicks([ticks])


class FlowWidget(QWidget):
    """资金流分时(累计净流入,单位亿元)。"""

    NOTE_DAILY = ("柱 = 每日主力净额(红柱=当日净流入,绿柱=当日净流出),青线 = 区间累计　·　"
                  "数据源只给每日净额:流入 = 净额为正的那些天合计,流出 = 净额为负的那些天合计,"
                  "不是买卖毛额")

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.hint = QLabel("")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.hint.hide()
        lay.addWidget(self.hint)
        # 图上的口径注明,日常隐藏,画日频柱时亮出来
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color:{TEXT_DIM};font-size:11px;padding:0 2px;")
        self.note.hide()
        lay.addWidget(self.note)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.plot = self.glw.addPlot(row=0, col=0)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.getAxis("left").setTextPen(TEXT_DIM)
        self.plot.getAxis("bottom").setTextPen(TEXT_DIM)

    def plot_flow(self, bars: list[dict]):
        self.note.hide()
        self.plot.clear()
        if not bars:
            self.hint.setText("资金流数据暂不可用(东财 push2 限流),稍后自动重试…")
            self.hint.show()
            return
        self.hint.hide()
        x = np.arange(len(bars))
        main = np.array([(b.get("main") or 0) / 1e8 for b in bars])
        cum = np.cumsum(main)
        colors = [UP if v >= 0 else DOWN for v in cum]
        self.plot.plot(x, cum, pen=_pen("#38bdf8", 1.6))
        self.plot.addLine(y=0, pen=_pen(TEXT_DIM, 1, style=Qt.DashLine))
        ticks = [(i, bars[i]["t"][11:16]) for i in range(0, len(bars), max(1, len(bars) // 5))]
        self.plot.getAxis("bottom").setTicks([ticks])
        self.plot.setLabel("left", "累计主力净流入(亿)")

    def plot_daily(self, bars: list[dict]):
        """日频:每日主力净流入柱 + 区间累计线(亿)。"""
        self.plot.clear()
        if not bars:
            self.hint.setText("日频资金流暂不可用(东财 push2his 限流),稍后自动重试…")
            self.hint.show()
            return
        self.hint.hide()
        vals = np.array([(b.get("main") or 0) / 1e8 for b in bars])
        x = np.arange(len(bars))
        brushes = [QBrush(QColor(UP if v >= 0 else DOWN)) for v in vals]
        # 只有 3 根柱子时 0.62 会粗得像色块,按根数收窄
        bw = 0.62 if len(bars) >= 6 else 0.42
        self.plot.addItem(pg.BarGraphItem(x=x, height=vals, width=bw,
                                          brushes=brushes, pen=pg.mkPen(None)))
        cum = np.cumsum(vals)
        self.plot.plot(x, cum, pen=_pen("#38bdf8", 1.4))
        self.plot.addLine(y=0, pen=_pen(TEXT_DIM, 1, style=Qt.DashLine))
        step = max(1, len(bars) // 6)
        ticks = [(i, str(bars[i].get("date", ""))[5:]) for i in range(0, len(bars), step)]
        self.plot.getAxis("bottom").setTicks([ticks])
        self.plot.setXRange(-0.7, len(bars) - 0.3, padding=0)
        self.plot.setLabel("left", "每日净流入(亿)/ 累计")
        self.note.setText(self.NOTE_DAILY)
        self.note.show()


class FlowPanel(QWidget):
    """资金流面板:今日分时 / 今日 / 3 / 5 / 7 日合计 / 每日明细。

    三个面永远同时给:流入、流出、净流入。
      · 今日:新浪 r0/r1 的 in/out 是真实买卖毛额,直接用;
      · 区间与每日:源只给每日净额,流入 = 净额为正的那些天合计、流出 = 为负的那些天合计,
        图上和提示里都写明「按净额正负拆分,非买卖毛额」。
    """

    period_changed = Signal(str)

    PERIODS = [("今日分时", "intra"), ("今日", "1"), ("近 3 日", "3"),
               ("近 5 日", "5"), ("近 7 日", "7"), ("每日明细", "d10")]

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addWidget(QLabel("口径"))
        self.period = QComboBox()
        for text, key in self.PERIODS:
            self.period.addItem(text, key)
        self.period.setToolTip("今日分时看盘内曲线;今日/3/5/7 日按交易日合计;每日明细逐日列三项")
        self.period.currentIndexChanged.connect(lambda *_: self._emit())
        bar.addWidget(self.period)
        self.sum = QLabel("")
        self.sum.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
        self.sum.setToolTip("今日是真实买卖毛额;区间与明细按源净额的正负拆分")
        bar.addWidget(self.sum, 1)
        lay.addLayout(bar)

        self.stack = QStackedWidget()
        self.chart = FlowWidget()
        self.stack.addWidget(self.chart)
        # 第 5 列留空:吃掉剩余宽度,免得数字被推到最右边、中间空一大块
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["日期", "流入(亿)", "流出(亿)", "净流入(亿)", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(True)                    # 空列吃剩余宽度
        for _c, _w in enumerate([132, 104, 104, 104]):
            self.table.setColumnWidth(_c, _w)
        self.stack.addWidget(self.table)
        lay.addWidget(self.stack, 1)

    def _emit(self) -> None:
        self.sum.setText("")
        if self.key() != "intra":
            self.table.setRowCount(0)
        self.period_changed.emit(self.key())

    def key(self) -> str:
        return str(self.period.currentData() or "intra")

    def is_intra(self) -> bool:
        return self.key() == "intra"

    # -------------------------------------------------- 画
    def plot_flow(self, bars: list[dict]) -> None:
        self.stack.setCurrentWidget(self.chart)
        self.chart.plot_flow(bars)

    def set_summary(self, win: dict) -> None:
        if not win.get("count"):
            self.sum.setText("区间数据暂无")
            self.sum.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")
            return
        days = int(win.get("days") or 1)
        head = "今日" if days == 1 else f"{days} 日"
        total = float(win.get("total") or 0)
        color = UP if total > 0 else (DOWN if total < 0 else TEXT_DIM)
        if win.get("gross"):
            gin, gout = float(win["gross_in"]), float(win["gross_out"])
            lab_in, lab_out = "流入(买)", "流出(卖)"
        else:
            gin, gout = float(win["inflow"]), float(win["outflow"])
            lab_in, lab_out = "流入日合计", "流出日合计"
        self.sum.setText(
            f"{head} · {lab_in} <b style=\"color:{UP}\">{gin / 1e8:.2f} 亿</b>"
            f" · {lab_out} <b style=\"color:{DOWN}\">{gout / 1e8:.2f} 亿</b>"
            f" · 净流入 <b style=\"color:{color}\">{total / 1e8:+.2f} 亿</b>"
            f" · 红绿 {win.get('up_days', 0)}/{win.get('down_days', 0)} 天")
        self.sum.setStyleSheet(f"color:{TEXT_DIM};font-size:12px;")

    def plot_window(self, win: dict) -> None:
        self.stack.setCurrentWidget(self.chart)
        self.set_summary(win)
        self.chart.plot_daily(win.get("bars") or [])

    def plot_days(self, win: dict) -> None:
        """每日明细:逐日列流入 / 流出 / 净流入。"""
        self.stack.setCurrentWidget(self.table)
        self.set_summary(win)
        bars = list(win.get("bars") or [])
        self.table.setRowCount(len(bars))
        for i, b in enumerate(reversed(bars)):        # 最近的排最上面
            v = float(b.get("main") or 0.0)
            up = v if v > 0 else 0.0
            down = -v if v < 0 else 0.0
            self.table.setItem(i, 0, QTableWidgetItem(str(b.get("date") or "")))
            self.table.setItem(i, 1, QTableWidgetItem(f"{up / 1e8:.2f}"))
            self.table.setItem(i, 2, QTableWidgetItem(f"{down / 1e8:.2f}"))
            self.table.setItem(i, 3, QTableWidgetItem(f"{v / 1e8:+.2f}"))
            self.table.item(i, 0).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            for col in (1, 2, 3):
                self.table.item(i, col).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.item(i, 1).setForeground(QColor(UP))
            self.table.item(i, 2).setForeground(QColor(DOWN))
            self.table.item(i, 3).setForeground(
                QColor(UP if v > 0 else (DOWN if v < 0 else TEXT_DIM)))


class EquityWidget(QWidget):
    """净值曲线 + 回撤填充。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw)
        self.plot = self.glw.addPlot(row=0, col=0)
        self.plot.showGrid(x=True, y=True, alpha=0.12)
        self.plot.getAxis("left").setTextPen(TEXT_DIM)
        self.plot.getAxis("bottom").setTextPen(TEXT_DIM)
        self.plot.addLegend(offset=(-10, 10))

    def plot_equity(self, equity: pd.DataFrame, benchmark: pd.DataFrame | None = None):
        self.plot.clear()
        if equity is None or equity.empty:
            return
        eq = equity.copy()
        x = np.arange(len(eq))
        base = float(eq["equity"].iloc[0])
        vals = eq["equity"].to_numpy() / base * 100
        self.plot.plot(x, vals, pen=_pen("#38bdf8", 1.8), name="策略净值")
        if benchmark is not None and not benchmark.empty:
            b = benchmark.set_index("datetime")["close"]
            b = b.reindex(pd.to_datetime(eq["date"])).ffill()
            if not b.isna().all():
                bv = (b.to_numpy() / float(b.iloc[0])) * 100
                self.plot.plot(x, bv, pen=_pen("#8b97a8", 1.2, style=Qt.DashLine),
                               name="基准")
        peak = np.maximum.accumulate(vals)
        dd = vals / peak * 100 - 100
        dates = [str(d)[:10] for d in eq["date"]]
        ticks = [(i, dates[i]) for i in range(0, len(dates), max(1, len(dates) // 6))]
        self.plot.getAxis("bottom").setTicks([ticks])
