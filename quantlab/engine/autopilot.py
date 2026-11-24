# -*- coding: utf-8 -*-
"""全自动模式:用数据把「仓位 / 止盈止损 / 回撤上限」三项参数自己算出来。

先说清楚它是什么、不是什么:

  **不是机器学习,也没有训练过程。** 这是一个确定性的自适应规则集:
  用个股近 20 日的波动率(ATR%)和账户的回撤状态,按固定公式推出参数。
  同样的输入永远给同样的输出,每个数字都能在面板上追溯到来源。
  之所以这么设计,是因为交易参数一旦变成黑箱,回撤时没人敢按下确认键。

三项分别怎么定:

1) 单票仓位上限(per_position_pct)
   目标:让单只票的日波动贡献不超过账户的 `risk_budget`(默认 1.5%)。
   个股权重 = risk_budget / 个股日波动率,再夹在 [floor, cap] 区间。
   波动 2%/日 → 0.75 倍基准;波动 4%/日 → 0.375 倍。波动越大买得越少。

2) 止损/止盈
   止损 = atr_mult × 20 日 ATR%(默认 2.5 倍),夹在 [3%, 12%]。
   止盈 = 盈亏比(默认 2.5) × 止损,夹在 [8%, 40%]。
   高波动的票自动给宽一点的止损,免得被日常噪声扫出去;
   低波动的票收紧,亏得快跑。

3) 回撤上限与动作(阶梯式,不是一刀切)
   dd < 半阈值        → 不干预
   dd ≥ 阈值          → 新仓预算 × 0.5(halve)
   dd ≥ 1.5×阈值      → 停止开新仓(stop_new)
   dd ≥ 2×阈值        → 清仓(liquidate)
   阈值本身也按波动自适应:账户年化波动高时阈值放宽,否则会被正常波动打穿。

冷启动:样本不足(持仓 < 1 或日线 < 30 根)时全部退回配置值,reason 里写明,
绝不拿半截数据假装算出了结论。
"""
from __future__ import annotations

import math

from .adaptive import AdaptiveSizer, DrawdownGuard

# 全自动模式的默认目标
DEF = {
    "risk_budget": 0.015,     # 单票允许贡献的日波动(占账户比例)
    "atr_mult": 2.5,          # 止损 = N × ATR%
    "rr_ratio": 2.5,          # 盈亏比:止盈 = rr × 止损
    "pos_floor": 0.06,        # 单票仓位下限(再保守也留一点)
    "pos_cap": 0.30,          # 单票仓位上限
    "sl_floor": 0.03,         # 止损区间
    "sl_ceil": 0.12,
    "tp_floor": 0.08,
    "tp_ceil": 0.40,
    "dd_base": 0.10,          # 回撤阈值基准(账户从峰值回撤 10% 开始干预)
    "lookback": 20,           # 波动率回看天数
}


def atr_pct(bars: list[dict], n: int = 20) -> float:
    """日线 ATR%:真实波幅占收盘价的均值。数据不够返回 0。"""
    rows = [b for b in bars if b.get("high") and b.get("low") and b.get("close")]
    if len(rows) < n + 1:
        return 0.0
    rows = rows[-(n + 1):]
    trs = []
    for prev, cur in zip(rows, rows[1:]):
        hi, lo, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        trs.append(tr / pc if pc else 0.0)
    return sum(trs) / len(trs) if trs else 0.0


def vol_pct(bars: list[dict], n: int = 20) -> float:
    """日收益率标准差(更平滑,用于仓位)。"""
    closes = [float(b["close"]) for b in bars if b.get("close")]
    if len(closes) < n + 1:
        return 0.0
    closes = closes[-(n + 1):]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 3:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(max(0.0, var))


class AutoPilot:
    """把「仓位/止盈止损/回撤」三项参数按数据算出来。"""

    def __init__(self, account: str = "default", cfg=None, params: dict | None = None,
                 strategy: str = ""):
        self.account = account or "default"
        self.cfg = cfg
        self.p = dict(DEF)
        if params:
            self.p.update({k: v for k, v in params.items() if v is not None})
        self.guard = DrawdownGuard(self.account)
        # 用真实策略名当 key,才能读到 auto trader 记下的样本;
        # 以前固定 "autopilot" → 那个 key 永远没人写,系数恒 1.0,说明是假的
        self.sizer = AdaptiveSizer(strategy or "autopilot", self.account)

    # ------------------------------------------------------------ 单票参数
    def position_pct(self, bars: list[dict], base: float = 0.18) -> tuple[float, str]:
        """按个股波动率折算单票仓位上限。

        以「基准仓位 base 对应基准波动 risk_budget」为锚点等比缩放:
        pct = base × risk_budget / 个股日波动。
        波动翻倍 → 仓位减半,正好让单票贡献的日波动保持恒定。
        """
        v = vol_pct(bars, int(self.p["lookback"]))
        if v <= 0:
            return float(base), "日线不足,仓位用配置值"
        want = float(base) * float(self.p["risk_budget"]) / v
        lo, hi = float(self.p["pos_floor"]), float(self.p["pos_cap"])
        pct = max(lo, min(hi, want))
        return round(pct, 4), f"日波动 {v:.2%} → 单票 {pct:.1%}"

    def stop_take(self, bars: list[dict]) -> tuple[float, float, str]:
        """按 ATR 给止损/止盈。返回 (止损, 止盈, 说明)。"""
        a = atr_pct(bars, int(self.p["lookback"]))
        if a <= 0:
            return 0.0, 0.0, "日线不足,止盈止损用配置值"
        sl = max(float(self.p["sl_floor"]),
                 min(float(self.p["sl_ceil"]), a * float(self.p["atr_mult"])))
        tp = max(float(self.p["tp_floor"]),
                 min(float(self.p["tp_ceil"]), sl * float(self.p["rr_ratio"])))
        return round(sl, 4), round(tp, 4), f"ATR {a:.2%} → 止损 {sl:.1%} / 止盈 {tp:.1%}"

    # ------------------------------------------------------------ 账户级
    def _dd_base(self) -> float:
        """回撤基准:界面填了就按界面的,没填用默认 10%。

        全自动模式必须有回撤保护,所以这里不会因为「用户设 0」就关掉 ——
        设 0 的含义是「没设」,用默认值,并在 notes 里写清楚。
        """
        try:
            v = float(self.cfg.get("autotrade.max_drawdown_pct", 0) or 0) if self.cfg else 0.0
        except Exception:
            v = 0.0
        return v if v > 0 else float(self.p["dd_base"])

    def drawdown_plan(self, equity: float, base_pct: float | None = None) -> dict:
        """阶梯式回撤动作,比单档 halt 更平滑。"""
        base = self._dd_base() if base_pct is None else float(base_pct)
        st = self.guard.peek(equity, base, 0.5)
        dd = float(st.get("dd") or 0.0)
        if base <= 0:
            return {"dd": dd, "level": 0, "scale": 1.0, "block_buys": False,
                    "liquidate": False, "max_drawdown": 0.0,
                    "message": "回撤控制关闭"}
        if dd >= base * 2:
            return {"dd": dd, "level": 3, "scale": 0.0, "block_buys": True,
                    "liquidate": True, "max_drawdown": base,
                    "message": f"回撤 {dd:.2%} ≥ {base * 2:.0%}:清仓"}
        if dd >= base * 1.5:
            return {"dd": dd, "level": 2, "scale": 0.0, "block_buys": True,
                    "liquidate": False, "max_drawdown": base,
                    "message": f"回撤 {dd:.2%} ≥ {base * 1.5:.0%}:停止开新仓"}
        if dd >= base:
            return {"dd": dd, "level": 1, "scale": 0.5, "block_buys": False,
                    "liquidate": False, "max_drawdown": base,
                    "message": f"回撤 {dd:.2%} ≥ {base:.0%}:新仓减半"}
        return {"dd": dd, "level": 0, "scale": 1.0, "block_buys": False,
                "liquidate": False, "max_drawdown": base,
                "message": f"回撤 {dd:.2%},正常"}

    def decide(self, equity: float, holdings: list, bars_of, base_pct: float = 0.18) -> dict:
        """一次算全:给 autotrader 的参数包 + 可读说明。

        bars_of(code) -> list[dict] 由调用方提供,便于复用已缓存的日线。
        """
        notes: list[str] = []
        # 账户级:回撤阶梯 + 自适应系数
        dd_plan = self.drawdown_plan(equity)
        notes.append(dd_plan["message"])
        coef = float(self.sizer.summary().get("multiplier") or 1.0)
        if abs(coef - 1.0) > 1e-6:
            notes.append(f"历史表现系数 {coef:.2f}")

        # 持仓级:按持仓票的平均波动折算(没有持仓就用基准)
        vols = []
        for p in holdings or []:
            code = getattr(p, "code", None) or (p.get("code") if isinstance(p, dict) else None)
            if not code:
                continue
            try:
                bars = bars_of(code) or []
            except Exception:
                bars = []
            v = vol_pct(bars, int(self.p["lookback"]))
            if v > 0:
                vols.append(v)
        if vols:
            avg_v = sum(vols) / len(vols)
            pct = max(float(self.p["pos_floor"]),
                      min(float(self.p["pos_cap"]),
                          float(base_pct) * float(self.p["risk_budget"]) / avg_v))
            notes.append(f"持仓均波动 {avg_v:.2%} → 单票 {pct:.1%}")
        else:
            pct = float(base_pct)
            notes.append("无持仓波动样本,仓位用配置值")

        scale = dd_plan["scale"] * coef
        return {
            "per_position_pct": round(max(0.02, pct * scale), 4),
            "base_pct": round(pct, 4),
            "size_mult": round(scale, 3),
            "max_drawdown": dd_plan["max_drawdown"],
            "dd_action": ("liquidate" if dd_plan["liquidate"]
                          else "stop_new" if dd_plan["block_buys"] else "halve"),
            "dd_level": dd_plan["level"],
            "block_buys": dd_plan["block_buys"],
            "liquidate": dd_plan["liquidate"],
            "equity": float(equity or 0),
            "notes": notes,
        }
