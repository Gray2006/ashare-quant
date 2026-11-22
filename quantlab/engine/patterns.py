# -*- coding: utf-8 -*-
"""K线形态:词典 + 识别 + 出现之后的实测涨跌频率。

分成两件事,别混:

1. 词典 —— 每个形态的判据、怎么读、目标位怎么算。给「形态」页右侧的详情用。
2. 实测 —— 在真实历史上数它出现过几次,统计发生后 1/3/5/10 日涨跌,
   再和「同期全样本基准」对比,看它到底有没有超额。

为什么要有第 2 件:形态书上的说法是别人的样本。同一张图在 A 股、在
2015 年和 2024 年,胜率不一样。只有拿自己的数据数一遍,才知道这条
判据在你交易的市场上值不值得看。

可自动识别的形态 = 单根/双根/三根组合 + 几类价格突破(规则明确)。
头肩顶、三角形这类要画趋势线的,判据没有唯一解,词典里给读法,
但不假装能自动认出来(detect=False)。
"""
from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------- 词典
# detect=True 的,scan/measure 能真的认出来;False 的只有文字。
PATTERNS: list[dict] = [
    # --- 单根 ---
    dict(key="hammer", name="锤子线", kind="单根", bias="看涨", detect=True,
         rule="下影线≥实体 2 倍,实体小且在上部,出现在下跌后",
         note="盘中空方一度大幅压低价格,收盘前被多方拉回开盘附近,说明卖压衰竭。"
              "影线越长、实体越小,反击力度越强。单独出现只是「止跌迹象」,"
              "要次日的阳线或放量才谈得上反转。",
         target="目标位看最近的下跌起点或前高;止损放在锤子线最低点下方"),
    dict(key="inverted_hammer", name="倒锤子", kind="单根", bias="看涨", detect=True,
         rule="上影线≥实体 2 倍,实体小且在下部,出现在下跌后",
         note="多方试探性推高被打回,但买盘开始出现。它比锤子线更弱,"
              "因为收盘没能守住涨幅。必须等次日阳线确认,不确认就当没发生。",
         target="确认后目标位看缺口/前高;止损为倒锤子最低点"),
    dict(key="hanging_man", name="上吊线", kind="单根", bias="看跌", detect=True,
         rule="形状同锤子线,但出现在上涨趋势之后",
         note="同样的长下影,位置决定含义:在涨了一段之后出现,说明盘中有过一轮"
              "抛售,多方勉强拉回。次日若低开低走,反转基本成立。",
         target="目标位看下方均线或前低;失效条件是次日收复上吊线最高点"),
    dict(key="shooting_star", name="射击之星", kind="单根", bias="看跌", detect=True,
         rule="上影线≥实体 2 倍,实体小且在下部,出现在上涨之后",
         note="盘中冲高后全部吐回,上方抛压明确。影线超过实体两倍以上才算,"
              "越小越像普通震荡。高位放量的射击之星最有效。",
         target="目标位看最近支撑;止损放在射击之星最高点上方"),
    dict(key="doji", name="十字星", kind="单根", bias="中性", detect=True,
         rule="开收盘几乎相同,实体≤全幅 10%",
         note="多空力量短暂均衡。它本身不是信号,是「位置信号」:在趋势末端"
              "出现提示动能减弱,在震荡中段出现基本没有意义。影线越长分歧越大。",
         target="无固定目标;等下一根 K 线选方向"),
    # --- 双根 ---
    dict(key="engulf_bull", name="看涨吞没", kind="双根", bias="看涨", detect=True,
         rule="前阴后阳,阳线实体完全包住前一根阴线实体",
         note="空方先得手,随后被一根更大的阳线整体吞回,力量对比当场反转。"
              "吞没的阳线越大、量越足越可信。位置比形态本身重要:跌了一段之后才有效。",
         target="目标位=吞没阳线高度的 1~2 倍,或前一个密集成交区;止损为阳线最低点"),
    dict(key="engulf_bear", name="看跌吞没", kind="双根", bias="看跌", detect=True,
         rule="前阳后阴,阴线实体完全包住前一根阳线实体",
         note="高位出现一根更大的阴线覆盖前阳,说明获利盘集中出逃。"
              "配合放量下跌最可靠;缩量吞没经常是假摔。",
         target="目标位=吞没阴线高度的 1~2 倍;止损为阴线最高点"),
    dict(key="piercing", name="刺透形态", kind="双根", bias="看涨", detect=True,
         rule="大阴线之后低开,阳线收盘越过前阴实体 50%",
         note="低开说明恐慌延续,但收盘强势收复一半以上失地,是明确的反击。"
              "收复不到 50% 的叫「待入线」,力度差一档,不足以当买点。",
         target="目标位看前阴开盘价;止损为阳线最低点"),
    dict(key="dark_cloud", name="乌云盖顶", kind="双根", bias="看跌", detect=True,
         rule="大阳线之后高开,阴线收盘跌破前阳实体 50%",
         note="高开诱多后一路走低,把前一日涨幅吞掉一半以上。"
              "插入越深越可靠;只跌到 30% 的多为洗盘。",
         target="目标位看前阳开盘价;止损为阴线最高点"),
    dict(key="harami_bull", name="看涨孕线", kind="双根", bias="看涨", detect=True,
         rule="大阴线之内包着一根小阳线,小实体完全落在大实体内",
         note="跌势里突然缩量收窄,是做空动能耗尽的迹象,但只是「停手」不是「反攻」。"
              "强度弱于吞没,必须等突破孕线高点再动手。",
         target="突破孕线最高点后,目标位=母阴线高度的 1 倍;止损为孕线最低点"),
    dict(key="harami_bear", name="看跌孕线", kind="双根", bias="看跌", detect=True,
         rule="大阳线之内包着一根小阴线,小实体完全落在大实体内",
         note="上涨中突然缩量、波动收窄,买方迟疑。可靠性中等,"
              "单看不足以卖出,通常作为「减仓提示」而不是「清仓信号」。",
         target="跌破孕线最低点后目标位=母阳线高度的 1 倍"),
    # --- 三根 ---
    dict(key="morning_star", name="晨星", kind="三根", bias="看涨", detect=True,
         rule="大阴线 + 小实体(星)+ 大阳线,阳线收盘深入第一根实体",
         note="三段式见底:杀跌 → 犹豫 → 反攻。中间那颗星是转折点,"
              "第三根阳线必须收复第一根阴线的一半以上才算成立。"
              "这是最可靠的底部组合之一,出现频率也低。",
         target="目标位=第一根阴线开盘价,或整组高度的 1.5 倍;止损为星线最低点"),
    dict(key="evening_star", name="暮星", kind="三根", bias="看跌", detect=True,
         rule="大阳线 + 小实体(星)+ 大阴线,阴线收盘深入第一根实体",
         note="晨星的镜像。高位冲高后迟疑,再由一根大阴线确认。"
              "星线若带长上影,反转意味更浓。",
         target="目标位=第一根阳线开盘价;止损为星线最高点"),
    dict(key="three_soldiers", name="红三兵", kind="三根", bias="看涨", detect=True,
         rule="三连阳,收盘逐根抬高,开盘落在前一根实体内",
         note="多方连续三日推进,是趋势启动的常见形态。三根实体大小接近、"
              "上影线短的最健康;实体逐根变小或上影变长,说明后续乏力,易变盘。",
         target="目标位看上方密集区;跌破第三根阳线最低点视为形态失效"),
    dict(key="three_crows", name="三只乌鸦", kind="三根", bias="看跌", detect=True,
         rule="三连阴,收盘逐根走低,开盘落在前一根实体内",
         note="红三兵的镜像,空方连续掌控。若三根都伴随放量下跌,"
              "往往是主跌段开始;缩量三阴更像获利回吐。",
         target="目标位看下方支撑;收复第一根阴线开盘价视为形态失效"),
    # --- 突破类(规则明确,可自动认) ---
    dict(key="new_high", name="新高突破", kind="突破", bias="看涨", detect=True,
         rule="收盘创 60 日新高(且此前 60 日内没有更高点)",
         note="所有近期买入者都在盈利,没有套牢盘压制。趋势交易里最简单的过滤器。"
              "风险是追在加速段末端,所以要配合回踩确认。",
         target="目标位不好估,按移动止盈处理(如跌破 10 日线离场)"),
    dict(key="new_low", name="新低破位", kind="突破", bias="看跌", detect=True,
         rule="收盘创 60 日新低",
         note="所有近期买入者都亏损,止损盘会自我强化。创新低后的反弹通常是逃命机会,"
              "而不是买点——除非同时出现放量长下影。",
         target="目标位看下一个成交密集区;反弹到前低附近为压力"),
    dict(key="box_break", name="箱体突破", kind="突破", bias="看涨", detect=True,
         rule="前 20 日在窄幅区间内震荡,收盘突破区间上沿 1%",
         note="横盘越久积累越足,突破后趋势通常越猛。要点是「量」:"
              "突破日成交量放大到均量 1.5 倍以上才算数,否则十有八九是假突破。",
         target="目标位=箱体高度投影(箱高加到突破点);跌回箱内视为失败"),
    dict(key="ma_break", name="均线突破", kind="突破", bias="看涨", detect=True,
         rule="收盘上穿 MA60(前一日在下方)",
         note="中期趋势由空转多的分界。站上之后 MA60 由压力变支撑。"
              "缺点是信号慢,震荡市里会反复被穿。",
         target="目标位看前期高点;跌回 MA60 下方 3% 视为假突破"),
    dict(key="boll_break", name="布林突破", kind="突破", bias="看涨", detect=True,
         rule="收盘上穿布林上轨(20,2)",
         note="分两种:带宽极窄之后的突破是趋势起点;带宽已经很宽时触上轨,"
              "多数是超买而非买点。看带宽比看价格重要。",
         target="强趋势里价格会贴着上轨走,用带宽收缩或跌破中轨离场"),
    dict(key="gap_up", name="向上跳空", kind="突破", bias="看涨", detect=True,
         rule="今日最低价高于昨日最高价(留缺口)",
         note="缺口本身是新的支撑位。有消息配合的跳空经常一路不回补;"
              "没消息的跳空多半在几日内回补,回补到缺口上沿是二次买点。",
         target="缺口上沿为支撑,跌破缺口下沿为失败"),
    # --- 只有文字、不自动识别 ---
    dict(key="hs_top", name="头肩顶", kind="形态", bias="看跌", detect=False,
         rule="左肩-头-右肩三个高点,跌破颈线确认",
         note="最经典的顶部反转。头部量最大、右肩量递减是典型结构。"
              "右肩略低于头部不算违规,但右肩高过头部就变成上升通道了。",
         target="跌幅 = 头部最高价 − 颈线价,从跌破点起算"),
    dict(key="hs_bottom", name="头肩底", kind="形态", bias="看涨", detect=False,
         rule="三个低点,中间最低,突破颈线确认",
         note="头肩顶的镜像。突破颈线时要放量,否则容易做成右肩之后再跌一段的假突破。",
         target="涨幅 = 颈线价 − 头部最低价,从突破点起算"),
    dict(key="double_top", name="双重顶", kind="形态", bias="看跌", detect=False,
         rule="两次冲击相近高点后回落,跌破颈线确认",
         note="M 顶。第二个顶量能通常小于第一个顶,说明买盘衰竭。"
              "两顶间距太近(不足 2 周)可靠性会打折。",
         target="跌幅 = 顶部到颈线的距离"),
    dict(key="double_bottom", name="双重底", kind="形态", bias="看涨", detect=False,
         rule="两次下探相近低点后反弹,突破颈线确认",
         note="W 底。第二次探底缩量、突破时放量是标准结构。"
              "底与底之间的反弹高点就是颈线。",
         target="涨幅 = 颈线到底部的距离"),
    dict(key="triple_top", name="三重顶", kind="形态", bias="看跌", detect=False,
         rule="三次触及相近高点后回落",
         note="比双重顶多一次验证,耗时更长,信号也更可靠。"
              "三次都在同一价位受阻,说明该位置有明确的卖压来源。",
         target="跌幅 = 顶部到颈线的距离"),
    dict(key="triple_bottom", name="三重底", kind="形态", bias="看涨", detect=False,
         rule="三次触及相近低点后反弹",
         note="支撑被反复验证三次,底部更牢。突破颈线后回踩不破颈线是最佳买点。",
         target="涨幅 = 颈线到底部的距离"),
    dict(key="round_top", name="圆弧顶", kind="形态", bias="看跌", detect=False,
         rule="价格缓慢由升转降,顶部呈圆弧",
         note="没有尖锐的转折点,是资金慢慢撤出的痕迹。形成周期长(数周到数月),"
              "对短线的指导有限,更适合判断中期趋势是否见顶。",
         target="跌幅 ≈ 圆弧的垂直高度"),
    dict(key="round_bottom", name="圆弧底", kind="形态", bias="看涨", detect=False,
         rule="价格缓慢见底后回升,底部呈 U 形",
         note="情绪由悲观转乐观的慢过程,常见于大盘蓝筹。"
              "突破圆弧右侧的平台时才算启动。",
         target="涨幅 ≈ 圆弧的垂直高度"),
    dict(key="asc_triangle", name="上升三角形", kind="形态", bias="看涨", detect=False,
         rule="水平阻力线 + 逐级抬高的低点",
         note="买方不断抬价试盘,卖方在同一位置挂单。压力位被试探的次数越多,"
              "突破后越干脆。整理时间通常 3~8 周。",
         target="突破点 + 三角形最宽处的垂直距离"),
    dict(key="desc_triangle", name="下降三角形", kind="形态", bias="看跌", detect=False,
         rule="水平支撑线 + 逐级下移的高点",
         note="卖方不断压低出货,买方死守一条支撑。破位往往很快,"
              "不要在支撑上沿抢反弹。",
         target="跌破点 − 三角形最宽处的垂直距离"),
    dict(key="sym_triangle", name="对称三角形", kind="形态", bias="中性", detect=False,
         rule="高点下移 + 低点上抬,振幅收敛",
         note="方向待定。突破方向决定后市,向上突破需放量、向下突破不需要。"
              "收敛到尖端才突破的,力度通常不及在 2/3 处突破的。",
         target="突破点 ± 三角形开口的最大高度"),
    dict(key="asc_wedge", name="上升楔形", kind="形态", bias="看跌", detect=False,
         rule="两条向上收敛的趋势线",
         note="价格在涨,但涨的速度在衰减,量也在缩。这是典型的「上涨末期」,"
              "向下破下轨的概率更大。",
         target="跌破点 − 楔形开口高度"),
    dict(key="desc_wedge", name="下降楔形", kind="形态", bias="看涨", detect=False,
         rule="两条向下收敛的趋势线",
         note="跌势在衰减。出现在下跌末端是反转,出现在上涨中途是中继整理。",
         target="突破点 + 楔形开口高度"),
    dict(key="flag", name="旗形", kind="形态", bias="中性", detect=False,
         rule="急涨/急跌之后,价格在两条平行线内小幅逆势整理",
         note="最可靠的中继形态之一。旗面整理时间通常不超过 3~4 周,"
              "拖太久就变成反转了。突破方向一般延续旗杆方向。",
         target="旗杆长度从突破点起算"),
    dict(key="pennant", name="三角旗形", kind="形态", bias="中性", detect=False,
         rule="急动之后的小型对称三角形整理",
         note="旗形的短促版本,整理 1~3 周。突破后往往复制旗杆的斜率。",
         target="旗杆长度从突破点起算"),
    dict(key="rectangle", name="矩形整理", kind="形态", bias="中性", detect=False,
         rule="价格在明确的水平支撑与阻力之间来回",
         note="箱体震荡。在箱内做高抛低吸,或等突破。箱体越久,突破后动能越大;"
              "但箱体内的假突破也最频繁。",
         target="突破点 ± 箱体高度"),
    dict(key="support_break", name="支撑跌破", kind="突破", bias="看跌", detect=False,
         rule="有效跌破多次获支撑的位置",
         note="支撑跌破后变阻力。有效跌破的标准是收盘确认 + 放量,"
              "盘中插针不算。跌破后常有回抽确认,回抽不过原支撑即二次卖出点。",
         target="下一支撑位;回抽不破原支撑为加空点"),
    dict(key="resistance_break", name="阻力突破", kind="突破", bias="看涨", detect=False,
         rule="放量站上多次受阻的位置",
         note="有效突破的标准:收盘站上、量能明显放大、后续 2~3 日不跌回。"
              "三个条件缺一个,都得按假突破防。",
         target="突破点 + 前期震荡区间高度"),
]

BY_KEY = {p["key"]: p for p in PATTERNS}


# ---------------------------------------------------------------- 识别
def _arr(bars: list[dict]) -> tuple[np.ndarray, ...]:
    o = np.array([float(b["open"]) for b in bars], dtype="float64")
    h = np.array([float(b["high"]) for b in bars], dtype="float64")
    l = np.array([float(b["low"]) for b in bars], dtype="float64")
    c = np.array([float(b["close"]) for b in bars], dtype="float64")
    return o, h, l, c


def _body(o, c):
    return np.abs(c - o)


def _rng(h, l):
    return np.maximum(h - l, 1e-9)


def _upper(o, c, h):
    return h - np.maximum(o, c)


def _lower(o, c, l):
    return np.minimum(o, c) - l


def _ma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _shift(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if n < len(x):
        out[n:] = x[:len(x) - n]
    return out


def _detect(key: str, o, h, l, c) -> np.ndarray:
    """返回布尔数组:第 i 根「完成」该形态。"""
    n = len(c)
    body, rng = _body(o, c), _rng(h, l)
    up_ = _upper(o, c, h)
    lo_ = _lower(o, c, l)
    bull = c > o
    bear = c < o
    ma5 = _ma(c, 5)
    trend_dn = c < _shift(ma5, 1)          # 处在短期均线下方 = 下跌语境
    trend_up = c > _shift(ma5, 1)
    out = np.zeros(n, dtype=bool)

    if key == "hammer":
        out = (lo_ >= 2 * body) & (up_ <= np.maximum(0.3 * body, 0.15 * rng)) & trend_dn
    elif key == "inverted_hammer":
        out = (up_ >= 2 * body) & (lo_ <= np.maximum(0.3 * body, 0.15 * rng)) & trend_dn
    elif key == "hanging_man":
        out = (lo_ >= 2 * body) & (up_ <= np.maximum(0.3 * body, 0.15 * rng)) & trend_up
    elif key == "shooting_star":
        out = (up_ >= 2 * body) & (lo_ <= np.maximum(0.3 * body, 0.15 * rng)) & trend_up
    elif key == "doji":
        out = body <= 0.1 * rng
    elif key == "engulf_bull":
        out = bull & _shift(bear, 1).astype(bool) & (o <= _shift(c, 1)) & (c >= _shift(o, 1))
    elif key == "engulf_bear":
        out = bear & _shift(bull, 1).astype(bool) & (o >= _shift(c, 1)) & (c <= _shift(o, 1))
    elif key == "piercing":
        pc, po = _shift(c, 1), _shift(o, 1)
        mid = (pc + po) / 2
        out = bull & _shift(bear, 1).astype(bool) & (o < pc) & (c > mid) & (c < po)
    elif key == "dark_cloud":
        pc, po = _shift(c, 1), _shift(o, 1)
        mid = (pc + po) / 2
        out = bear & _shift(bull, 1).astype(bool) & (o > pc) & (c < mid) & (c > po)
    elif key == "harami_bull":
        po, pc = _shift(o, 1), _shift(c, 1)
        big = _shift(bear & (body > 0.6 * rng), 1).astype(bool)
        out = bull & big & (o > pc) & (c < po)
    elif key == "harami_bear":
        po, pc = _shift(o, 1), _shift(c, 1)
        big = _shift(bull & (body > 0.6 * rng), 1).astype(bool)
        out = bear & big & (o < pc) & (c > po)
    elif key == "morning_star":
        b2, b1 = _shift(body, 2), _shift(body, 1)
        o2, c2 = _shift(o, 2), _shift(c, 2)
        mid2 = (o2 + c2) / 2
        out = (bull & _shift(bear, 2).astype(bool)
               & (b1 <= 0.4 * b2) & (c > mid2) & (c < o2))
    elif key == "evening_star":
        b2, b1 = _shift(body, 2), _shift(body, 1)
        o2, c2 = _shift(o, 2), _shift(c, 2)
        mid2 = (o2 + c2) / 2
        out = (bear & _shift(bull, 2).astype(bool)
               & (b1 <= 0.4 * b2) & (c < mid2) & (c > o2))
    elif key == "three_soldiers":
        out = (bull & _shift(bull, 1).astype(bool) & _shift(bull, 2).astype(bool)
               & (c > _shift(c, 1)) & (_shift(c, 1) > _shift(c, 2))
               & (o <= _shift(c, 1)) & (o >= _shift(o, 1)))
    elif key == "three_crows":
        out = (bear & _shift(bear, 1).astype(bool) & _shift(bear, 2).astype(bool)
               & (c < _shift(c, 1)) & (_shift(c, 1) < _shift(c, 2))
               & (o >= _shift(c, 1)) & (o <= _shift(o, 1)))
    elif key == "new_high":
        win = 60
        for i in range(win, n):
            out[i] = c[i] >= h[i - win:i].max() and c[i] > c[i - 1]
    elif key == "new_low":
        win = 60
        for i in range(win, n):
            out[i] = c[i] <= l[i - win:i].min() and c[i] < c[i - 1]
    elif key == "box_break":
        span, look = 20, 20
        for i in range(span + look, n):
            seg = c[i - look - span:i - look]
            top, bot = seg.max(), seg.min()
            if top <= 0 or (top - bot) / top > 0.12:      # 区间要够窄
                continue
            out[i] = c[i] > top * 1.01 and c[i - 1] <= top
    elif key == "ma_break":
        ma60 = _ma(c, 60)
        prev = _shift(ma60, 1)
        out = (c > ma60) & (_shift(c, 1) <= prev)
    elif key == "boll_break":
        ma20, sd = _ma(c, 20), np.full(n, np.nan)
        for i in range(19, n):
            sd[i] = c[i - 19:i + 1].std()
        up_band = ma20 + 2 * sd
        out = (c > up_band) & (_shift(c, 1) <= _shift(up_band, 1))
    elif key == "gap_up":
        out = l > _shift(h, 1)
    return np.nan_to_num(out).astype(bool)


def scan(bars: list[dict], bars_from_end: int = 20) -> list[dict]:
    """看最近若干根 K 线收盘时触发了哪些形态。

    返回 [{i, key, name, bias, close, date}] ,i 是相对 bars 的下标。
    """
    if len(bars) < 30:
        return []
    o, h, l, c = _arr(bars)
    out = []
    start = max(0, len(bars) - max(1, bars_from_end))
    for p in PATTERNS:
        if not p["detect"]:
            continue
        try:
            hit = _detect(p["key"], o, h, l, c)
        except Exception:
            continue
        for i in np.nonzero(hit)[0]:
            if i < start:
                continue
            out.append({"i": int(i), "key": p["key"], "name": p["name"],
                        "bias": p["bias"], "close": float(c[i]),
                        "date": str(bars[i].get("datetime") or "")})
    out.sort(key=lambda r: (-r["i"], r["name"]))
    return out


def measure(bars: list[dict], horizons: tuple[int, ...] = (1, 3, 5, 10),
            lookback: int = 250) -> dict:
    """数每个形态在样本里出现几次,以及出现之后 h 日的涨跌统计。

    基准 = 同一段样本里「随便某一天」的 h 日涨跌,用来看形态有没有超额。
    样本不足 10 次的不给结论,只报次数——免得把噪声当规律。
    """
    if len(bars) < 80:
        return {"ok": False, "reason": f"样本只有 {len(bars)} 根,至少要 80 根"}

    bars = bars[-max(80, lookback):]
    o, h, l, c = _arr(bars)
    n = len(c)
    maxh = max(horizons)

    base = {}
    for hz in horizons:
        fwd = c[hz:] / c[:-hz] - 1.0
        base[hz] = {"n": int(len(fwd)),
                    "up": float((fwd > 0).mean()) if len(fwd) else None,
                    "avg": float(fwd.mean()) if len(fwd) else None}

    rows = []
    for p in PATTERNS:
        if not p["detect"]:
            continue
        try:
            hit = _detect(p["key"], o, h, l, c)
        except Exception:
            continue
        idx = [i for i in np.nonzero(hit)[0] if i + maxh < n]
        if not idx:
            rows.append({"key": p["key"], "name": p["name"], "bias": p["bias"],
                         "n": 0, "h": {}})
            continue
        hs = {}
        for hz in horizons:
            r = np.array([c[i + hz] / c[i] - 1.0 for i in idx])
            hs[hz] = {"n": int(len(r)), "up": float((r > 0).mean()),
                      "avg": float(r.mean()), "med": float(np.median(r))}
        rows.append({"key": p["key"], "name": p["name"], "bias": p["bias"],
                     "n": len(idx), "h": hs})
    rows.sort(key=lambda r: (-r["n"], r["name"]))
    return {"ok": True, "bars": n, "horizons": list(horizons),
            "base": base, "rows": rows,
            "from": str(bars[0].get("datetime") or ""),
            "to": str(bars[-1].get("datetime") or "")}


def edge(row: dict, hz: int, base: dict) -> float | None:
    """形态出现后的上涨概率 − 基准上涨概率(百分点)。"""
    try:
        return (row["h"][hz]["up"] - base[hz]["up"]) * 100.0
    except Exception:
        return None


def describe(key: str) -> str:
    p = BY_KEY.get(key)
    if not p:
        return ""
    return (f"【{p['name']}】{p['kind']} · {p['bias']}\n\n"
            f"判据:{p['rule']}\n\n{p['note']}\n\n"
            f"目标位 / 止损:{p['target']}")


def to_text(rep: dict) -> str:
    if not rep.get("ok"):
        return f"样本不足:{rep.get('reason', '')}"
    lines = [f"形态实测 · {rep['from'][:10]} ~ {rep['to'][:10]} · {rep['bars']} 根",
             "基准(全样本):" + "  ".join(
                 f"{hz}日 {rep['base'][hz]['up'] * 100:.1f}%" for hz in rep["horizons"]),
             ""]
    for r in rep["rows"]:
        if not r["n"]:
            continue
        sep = "  ".join(f"{hz}日 {r['h'][hz]['up'] * 100:.0f}%" for hz in rep["horizons"])
        lines.append(f"{r['name']:<6}{r['bias']} 出现 {r['n']:>3} 次  上涨占比 {sep}")
    return "\n".join(lines)
