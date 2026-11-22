# -*- coding: utf-8 -*-
"""帮助中心:指标口径、策略 DSL 函数库、交易与风控口径、快捷键。

内容都对着本仓库的实现写,不抄别的软件。哪条是「我们的口径」、
哪条是「A 股规则的现实」,分开讲清楚——这两者搞混是最容易亏钱的地方。
"""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QPlainTextEdit, QPushButton,
                               QTabWidget, QVBoxLayout)

from ..config import cfg
from ..engine import rules
from .dsl_help import _EXAMPLES as _DSL_EXAMPLES, _SYNTAX as _DSL_SYNTAX
from .dsl_help import _func_table as _dsl_func_table

_IND = """【均线 MA / EMA】
ma(n)  = 最近 n 根收盘价的算术平均。ema(n) 是带权重的指数均线,对新价反应更快。
用法:价格在均线上方且均线向上,视为多头环境。均线本身是滞后指标,
      它描述的是「已经发生的事」,不能用来预测拐点。

【MACD(12,26,9)】
macd_dif = EMA12 - EMA26;macd_dea = DIF 的 9 日 EMA;macd_hist = (DIF - DEA) × 2。
用法:金叉/死叉看 DIF 与 DEA 的穿越;柱状值(hit)看动能强弱。
      零轴之上的金叉比零轴之下的金叉更可靠——前者是趋势中的回调结束。

【KDJ(9)】
RSV = (收盘 - 9 日最低) / (9 日最高 - 9 日最低) × 100;
K = RSV 的 3 日平滑,D = K 的 3 日平滑,J = 3K - 2D。
用法:>80 超买、<20 超卖。震荡市有效,单边趋势里会长期钝化,
      这时候看 KDJ 会连续踏空或过早离场。

【RSI(14)】
RSI = 平均涨幅 / (平均涨幅 + 平均跌幅) × 100。
用法:>70 偏热、<30 偏冷。和 KDJ 一样,在强趋势里会失效。

【BOLL(20,2)】
boll_mid = MA20;boll_up/low = 中轨 ± 2 倍标准差。
用法:带宽(boll_up - boll_low)收缩到极窄,往往预示变盘;
      价格贴轨运行是趋势的标志,不是反转的标志。

【ATR(14)】
真实波幅的平均值,单位是元。它衡量「一天大概波动多少钱」。
用法:仓位 = 可承受亏损 / (ATR × 倍数),这是把止损距离换算成手数的标准做法。
      别用固定百分比止损代替它——同样的 5%,在高波动股上等于没设。

【量能与换手】
volume 单位统一为手(1 手 = 100 股),amount 是成交额(元)。
换手率 = 成交量 / 流通股本,本工具在需要时会从流通市值反推。
判读:放量突破才算突破;缩量上涨多为惜售,不代表买盘强。
"""

_DSL = _DSL_SYNTAX + "\n" + _dsl_func_table() + "\n" + _DSL_EXAMPLES + """
【v3 新增的四件事】
  1. 参数占位 #1 #2 …:一条公式跑一片参数。研究页的参数网格用它铺开,
     回填走 dsl.render(),表达式本身有语法错当场就报,不会拼出半句坏代码。
  2. 自定义函数 fn 名(a,b) = 式子:把常用组合收成一句,
     调用点就地展开,预热长度和横截面依赖都会跟着传递。
  3. 静态体检与逐式调试:
        dsl.compile_expr("ma(close,1) > 0").lint()     → 指出没意义的窗口
        dsl.compile_expr(式).trace(df, code)           → 逐个子式列出真实值
     不用改表达式就能看出是哪一段判错了。
  4. 编译期优化:常量折叠 + 公共子式合并。ma(close,20) 在同一句里写两遍
     只算一次,1+2 这种在编译时就算完。策略页右上「编译计划」能看到
     优化前后的节点数和求值顺序,结果与手写完全一致。

【写规则时的四个坑】
  1. 预热不足:ma(60) 要 60 根 K 线才算得准。表达式引擎会自动屏蔽预热区,
     但如果你只拉了 100 根日线,实际有效区间只有 40 根。
  2. 用未来数据:ref(-1) 这类写法不存在,所有函数只看历史。
     含 amount 这种当日才确定的量时,回测要确认时点。
  3. 参数过拟合:在 300 根 K 线上调出 8 个参数,实盘基本失效。
     研究页的批量枚举就是为这个准备的——看参数稳健性,别只看最高收益。
  4. 忽略成本:换手高的规则要扣掉双边手续费与滑点再看净值。

【安全约束】
  表达式先分词再建语法树,全程不经过 eval;不能访问属性、不能 import、
  不能调用未注册的函数。这不是限制自由,是防止一条规则把进程带崩。
"""


def _facts() -> str:
    rk = cfg().get("risk") or {}
    fee = cfg().get("fee") or {}
    return (
        "【A 股交易规则】\n"
        "  涨跌停、申报单位、T+1、集合竞价时段等,以「大盘研判 → 交易规则速查」为准,\n"
        "  那份内容由 exchange 模块按当前市场状态生成,不写死在文档里以免过期。\n\n"
        "【本工具的风控口径(设置页可改)】\n"
        f"  单笔金额上限        {float(rk.get('max_order_amount') or 0):,.0f} 元\n"
        f"  单票仓位上限        {float(rk.get('max_position_ratio') or 0) * 100:.0f}%\n"
        f"  每日下单笔数上限    {int(rk.get('max_daily_orders') or 0)} 笔\n"
        f"  价格偏离上限        {float(rk.get('max_price_deviation') or 0) * 100:.0f}%"
        "(委托价与现价的偏离,防止乌龙指)\n"
        f"  最低佣金            {float(fee.get('min_commission') or 0):.0f} 元/笔\n\n"
        "【回测口径】\n"
        "  · 按手取整,买不进涨停、卖不出跌停,T+1 当日买入不可卖\n"
        "  · 成本含佣金与印花税,卖出侧计印花税\n"
        "  · 净值曲线以每日收盘计,不做盘中撮合\n"
        "  · 批量对比看「参数稳健性」:同一规则在相邻参数上是否都赚钱。\n"
        "    只有一两个参数点好看、旁边全亏的,是过拟合而不是策略。\n\n"
        "【概率与预判口径】\n"
        "  · 走势预判给的是分桶实测频率,不是模型预测\n"
        "  · 小样本用拉普拉斯平滑,避免「3 次全对 = 100%」\n"
        "  · Brier 分数衡量校准度:越接近 0 越好;它比命中率更能暴露过度自信\n"
        "  · 形态页的「超额(pp)」= 形态后上涨占比 − 全样本基准,样本 <10 不给结论\n\n"
        "【持仓与复盘的边界】\n"
        "  · 复盘的平仓配对用 FIFO,只认订单表里有的东西\n"
        "  · 订单表没有策略名字段,所以按策略归因做不了——要做得先让下单写 remark\n"
        "  · 抓到涨停、盘口挂单这类判断需要实时行情,复盘页不做推测,标注「需行情」\n"
    )


_KEYS = """【全局快捷键】
  Ctrl+K             按名字跳页面(可打中文、英文 key、缩写,如 bt / mac)
  Ctrl+B             收起 / 展开左侧导航(收起后数据区多出 196px)
  Ctrl+Tab / Ctrl+PgDown     下一个页面
  Ctrl+Shift+Tab / Ctrl+PgUp 上一个页面
  Ctrl+1 ~ Ctrl+9    直接到第 N 个页面(顺序与左侧导航一致)
  F5                 刷新当前页
  Ctrl+Shift+R       把所有页面的分栏比例恢复默认
  F1                 打开这个帮助窗口

【导航】
  侧栏按 看盘 / 研究 / 研判 / 交易 / 管理 分组;
  点分组标题可折叠或展开该组(当前页所在的那组不会折叠);
  页面多时侧栏自己滚动,当前页会自动滚到看得见的位置。

【卡片操作】
  拖动卡片四周的分栏线   调整相邻卡片的比例(自动存盘)
  双击卡片标题栏         把这张卡片在区域内铺满 / 还原
  双击分栏线             把该处分栏恢复为默认比例

【表格】
  单击选中行;部分页面提供单击行联动右侧详情(如形态页、复盘页)。
  列宽随窗口自适应;真的放不下时出现横向滚动条,不会再把右端几列切掉。
  内容过长时鼠标悬停看提示。

【页面里的常用动作】
  行情页  双击板块名 → 跳板块页;双击成分股 → 跳回行情页
  选股页  选中结果 → 一键送去交易页填单
  板块页  双击成分股 → 行情页定位
"""


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("帮助中心 · 口径与函数库")
        self.setMinimumSize(880, 620)
        self.setSizeGripEnabled(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._page(_IND), "指标说明")
        tabs.addTab(self._page(_DSL), "策略函数库")
        tabs.addTab(self._page(_facts()), "口径速查")
        tabs.addTab(self._page(_KEYS), "快捷键与操作")
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
        return t


def dsl_function_names() -> list[str]:
    """给自检用:函数库里的名字应当都能在 rules 的白名单里找到。"""
    return list(rules.FUNCS)
