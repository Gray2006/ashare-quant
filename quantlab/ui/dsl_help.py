# -*- coding: utf-8 -*-
"""表达式手册:DSL v2 的算子表、写法示例、常见报错含义。

内容全部从 `engine.dsl` 的注册表生成,代码改了手册跟着变,不会脱节。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QPlainTextEdit, QPushButton,
                               QTabWidget, QVBoxLayout)

from ..engine import dsl

_SYNTAX = """量化表达式 v3(QL)速查

【基本形态】
  买点 / 卖点各是一条表达式,对每只股票自己的历史逐根计算,严格因果。
  最后一行是输出(要判断的条件),前面可以定义变量和函数。

      趋势 := ma(60)
      close > 趋势 and volume > vol_ma(20) * 1.2 and macd_hist > 0

【语句】
  x := 表达式        定义变量(可多行,可引用之前的变量,不可重名)
  fn 名(a,b) = 式    定义函数(体是一句,可调用多次,调用点就地展开)
  名字: 表达式       通达信风格的输出行(导入老公式时保留)
  a; b; c            分号或换行分隔语句
  // 注释  {块注释}   两种注释都认

【参数占位:一条公式跑一片参数】
  ma(close, #1) > ma(close, #2)            #1 #2 是待填的参数
  dsl.evaluate(expr, df, params=[5, 20])   跑一组
  dsl.render(expr, [5, 20])                → "ma(close, 5) > ma(close, 20)"
  研究页的参数网格走的就是这个,不再靠 str.format 拼字符串。

【运算符(优先级从低到高)】
  ? :                三元:close > ma(20) ? close : ma(20)
  or  ||             或
  and &&             且
  not                非
  == != > >= < <=    比较;链式 a > b > c 等价于 a > b and b > c
  + -                加减
  * / %              乘除取余
  ^ **               幂

【自定义函数】
  fn 均线多头(a, b) = a > b and ma(a, 5) > ma(b, 5)
  均线多头(close, ma(20))
  限制:函数体只能是一句表达式、里面不能赋值、不能重定义内置函数。
  调用点就地展开,所以预热长度和横截面依赖都会正确传递。

【和旧版(v1/v2)的差别】
  · 前 N 根(预热区)不再输出信号:ma(20) 要用满 20 根才生效
  · 除零得到 inf/NaN,不再抛 '__import__' 这种看不懂的错
  · ma(close, 0)、ref(close, -1) 在保存前就报错,不会静默产出错误回测
  · 所有旧写法(AND/OR/NOT、C/O/H/L/V、filter/dynainfo/capital)照样认
  · v3 起有参数占位 #n、自定义函数 fn、静态体检 lint、逐式调试 trace

【静态体检】
  dsl.compile_expr(式).lint() 会挑出:窗口写 1、ref(x,0)、两边都是常数、
  定义了没用到的变量、预热超过 250 根 这类「多半写错了」的地方。
  检查的是你写的那棵树,不是优化后的样子。

【编译期优化】
  编译时做两件事,结果和手写完全一致,只是算得更少:
    · 常量折叠    1 + 2 > 0 → 常数;close * 1 → close
    · 公共子式合并 ma(close,20) 写两遍只算一次(指标页「编译计划」可看)
  dsl.compile_expr(式).plan() 打印求值顺序和被复用的子式;
  saved_nodes() 给出省掉多少节点。

【调试】
  dsl.compile_expr(式).trace(df, code) 逐个子式列出该根的值,
  不用改表达式就能看出是哪一段判错了。

【常见报错怎么改】
  「窗口必须 ≥1」            ma(close,0) → 窗口写 ≥3 的整数
  「窗口参数给成了序列」       ma(20, close) → 顺序反了:ma(close, 20)
  「不能往前看:位移必须 ≥0」   ref(close,-1) 没有语义,因果计算只看历史
  「是函数,后面要跟括号」     close > ma → ma(20)
  「没有这个函数」            拼错或者还没实现,按下面表里的名字写
  「横截面函数需要多只股票」   rank/zscore 只能在回测、选股、自动交易里跑
  「参数 #n 没有绑定值」       调用时传 params=[...],或把 #n 换成数字
  「函数体只能是一句表达式」   fn 里不能写 x := ...,把中间步骤搬到外面
"""

_EXAMPLES = """【可以直接抄的写法】

趋势跟随(带止损):
  买:trend := ma(60)
      close > trend and macd_hist > 0 and volume > vol_ma(20)
  卖:trend := ma(60)
      close < trend or pnl_pct(close > trend, close < ma(20)) < -8

放量突破 + 信号去重:
  买:filter(close > hhv(close, 20) * 0.999 and volume > vol_ma(20) * 1.8, 3)
  卖:close < ma(10)

超跌反弹(RSI + 布林):
  买:rsi(14) < 30 and close < boll_low and close > ref(close, 1)
  卖:rsi(14) > 60 or close > boll_mid

持有天数上限(状态算子,替代 max_hold):
  买:close > ma(20)
  卖:close < ma(20) or hold_bars(close > ma(20), close < ma(20)) >= 10

横截面动量(只在回测/选股/自动交易里能跑):
  买:rank(pct(20)) > 0.9 and rank(amount) > 0.5 and close > ma(20)
  卖:rank(pct(20)) < 0.5

成交量价相关(量价背离):
  买:ts_corr(close, volume, 10) < -0.3 and close > ma(20)
  卖:ts_corr(close, volume, 10) > 0.6

位置与回撤:
  买:close > hhv(close, 60) * 0.95 and ts_argmax(close, 60) < 5
  卖:close < hhv(close, 60) * 0.9

自定义函数:把常用的组合收成一句话:
  fn 多头(a, b) = a > b and ma(a, 5) > ma(b, 5)
  买:多头(close, ma(20)) and macd_hist > 0
  卖:not 多头(close, ma(20))

参数占位:同一套逻辑换参数反复试:
  fn 突破(n) = close > hhv(close, n) * 0.999
  买:突破(#1) and volume > vol_ma(20) * #2
  → 研究页会用 #[1]=20 #[2]=1.8 这类组合自动铺开

【通达信公式直接贴】
  GS1:=CAPITAL<8000000 AND DYNAINFO(4)>0;
  GS2:=CLOSE<45 AND REF(LOW,1)<MA(CLOSE,5);
  选股:GS1 AND GS2 AND CROSS(MA(C,5),MA(C,20));
  → 变量按 := 原样保留,不再全部内联展开
"""


def _func_table() -> str:
    groups: dict[str, list[tuple[str, str]]] = {}
    for group, name, doc in dsl.docs():
        groups.setdefault(group, []).append((name, doc))
    order = ["均线", "动量", "强弱", "波动", "趋势", "随机", "布林", "引用",
             "极值", "统计", "时序", "横截面", "逻辑", "状态", "个股信息", "数学"]
    lines = ["【函数库】按分组列出,全部因果(只看当根与之前)"]
    for group in order + [g for g in groups if g not in order]:
        rows = groups.get(group)
        if not rows:
            continue
        lines.append("")
        lines.append(f"《{group}》")
        for name, doc in sorted(rows):
            lines.append(f"  {doc}")
    lines.append("")
    lines.append("【列】close open high low volume amount turnover float_cap market_cap")
    lines.append("      简写:c o h l v vol amo   (volume 单位:手)")
    lines.append("")
    lines.append(f"共 {len(dsl.FUNC_SPECS)} 个函数、{len(dsl.COLUMNS)} 个列。")
    return "\n".join(lines)


class DslHelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("量化表达式手册 · v2")
        self.setMinimumSize(920, 660)
        self.setSizeGripEnabled(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._page(_SYNTAX), "语法与报错")
        tabs.addTab(self._page(_func_table()), "函数库")
        tabs.addTab(self._page(_EXAMPLES), "示例配方")
        lay.addWidget(tabs, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton("关闭")
        b.setObjectName("Primary")
        b.clicked.connect(self.accept)
        row.addWidget(b)
        lay.addLayout(row)

    @staticmethod
    def _page(text: str) -> QPlainTextEdit:
        t = QPlainTextEdit()
        t.setReadOnly(True)
        t.setPlainText(text)
        t.setObjectName("Mono")
        t.setLineWrapMode(QPlainTextEdit.NoWrap)
        return t
