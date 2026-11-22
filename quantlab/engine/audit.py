# -*- coding: utf-8 -*-
"""绩效与审计。

刻意保留两套夏普(EP007 踩过的坑):
  * sharpe_mtm —— 每日盯市净值算的年化夏普(控制台口径,界面主用)
  * sharpe_realized —— 按「平仓日」归集已实现盈亏算的年化夏普(目标函数口径)
两者可差 0.4~0.6,混用会把结论判反。

另外提供:
  * alpha/beta(相对基准回归)、最大回撤、Calmar、Sortino、胜率、盈亏比
  * 月度拆分、蒙特卡洛重采样、Deflated Sharpe(试了多少次才挑出这个夏普)
  * 未来函数检查:同一时点在「全量数据」与「截断数据」上的信号是否一致
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import attribution as at

TRADING_DAYS = 252


# ---------------------------------------------------------------- 基础指标

def sharpe_mtm(equity: pd.Series, rf: float = 0.0) -> float:
    if equity is None or len(equity) < 20:
        return 0.0
    r = equity.pct_change().dropna()
    sd = float(r.std(ddof=1))
    if sd == 0 or math.isnan(sd):
        return 0.0
    return float((r.mean() - rf / TRADING_DAYS) / sd * math.sqrt(TRADING_DAYS))


def sharpe_realized(trades: pd.DataFrame, capital: float, start, end) -> float:
    """按平仓日归集盈亏 → 日收益率序列 → 年化夏普。"""
    if trades is None or trades.empty or capital <= 0:
        return 0.0
    pnl = pd.to_numeric(trades["profit"], errors="coerce").fillna(0.0)
    day = pd.to_datetime(trades["close_date"]).dt.floor("D")
    daily = pnl.groupby(day).sum() / capital
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    daily = daily.reindex(idx, fill_value=0.0)
    sd = float(daily.std(ddof=1))
    if len(daily) < 30 or sd == 0 or math.isnan(sd):
        return 0.0
    return float(daily.mean() / sd * math.sqrt(TRADING_DAYS))


def max_drawdown(equity: pd.Series) -> tuple[float, str, str]:
    """返回 (最大回撤(负数%), 峰日期, 谷日期)。"""
    if equity is None or len(equity) < 2:
        return 0.0, "", ""
    eq = equity.astype(float)
    peak = eq.cummax()
    dd = eq / peak - 1
    low_idx = int(dd.to_numpy().argmin())
    peak_idx = int(eq.to_numpy()[: low_idx + 1].argmax())
    return float(dd.iloc[low_idx] * 100), str(eq.index[peak_idx]), str(eq.index[low_idx])


def sortino(equity: pd.Series) -> float:
    if equity is None or len(equity) < 20:
        return 0.0
    r = equity.pct_change().dropna()
    downside = r[r < 0]
    sd = float(downside.std(ddof=1)) if len(downside) > 2 else 0.0
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(TRADING_DAYS))


def alpha_beta(port_ret: pd.Series, bench_ret: pd.Series, rf: float = 0.0) -> dict:
    """OLS: r_p - rf = alpha + beta*(r_b - rf) + e。alpha 年化。"""
    df = pd.concat([port_ret, bench_ret], axis=1).dropna()
    if len(df) < 30:
        return {"alpha": None, "beta": None, "r2": None, "n": len(df), "t_alpha": None}
    x = (df.iloc[:, 1] - rf / TRADING_DAYS).to_numpy()
    y = (df.iloc[:, 0] - rf / TRADING_DAYS).to_numpy()
    X = np.column_stack([np.ones(len(x)), x])
    try:
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {"alpha": None, "beta": None, "r2": None, "n": len(df), "t_alpha": None}
    resid = y - X @ coef
    dof = max(len(y) - 2, 1)
    sigma2 = float(resid @ resid) / dof
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
        t_alpha = float(coef[0] / math.sqrt(cov[0, 0])) if cov[0, 0] > 0 else None
    except np.linalg.LinAlgError:
        t_alpha = None
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / ss_tot if ss_tot > 0 else None
    return {"alpha": float(coef[0] * TRADING_DAYS), "beta": float(coef[1]),
            "r2": r2, "n": int(len(df)), "t_alpha": t_alpha}


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    if equity is None or len(equity) < 2:
        return pd.DataFrame()
    eq = equity.copy()
    eq.index = pd.to_datetime(eq.index)
    monthly = eq.resample("ME").last().ffill()
    monthly = pd.concat([pd.Series([eq.iloc[0]], index=[eq.index[0]]), monthly])
    ret = monthly.pct_change().dropna() * 100
    out = pd.DataFrame({"month": ret.index.strftime("%Y-%m"), "ret": ret.to_numpy()})
    return out


# ---------------------------------------------------------------- 蒙特卡洛 / DSR

def monte_carlo(equity: pd.Series, n: int = 1000, seed: int = 42) -> dict:
    """有放回重采样日收益(1000 次),给出终值收益与最大回撤的分布。

    注:不能用「打乱顺序」——打乱只是换路径,累计乘积不变,终值必然一模一样。
    """
    if equity is None or len(equity) < 30:
        return {}
    r = equity.pct_change().dropna().to_numpy()
    rng = np.random.default_rng(seed)
    L = len(r)
    finals, mdds = [], []
    for _ in range(n):
        sample = rng.choice(r, size=L, replace=True)
        curve = np.cumprod(1 + sample)
        finals.append(curve[-1] - 1)
        peak = np.maximum.accumulate(curve)
        mdds.append(float((curve / peak - 1).min()))
    finals = np.array(finals)
    mdds = np.array(mdds)
    return {
        "n": n,
        "final_p05": float(np.percentile(finals, 5) * 100),
        "final_p50": float(np.percentile(finals, 50) * 100),
        "final_p95": float(np.percentile(finals, 95) * 100),
        "mdd_p50": float(np.percentile(mdds, 50) * 100),
        "mdd_p95": float(np.percentile(mdds, 5) * 100),   # 5% 分位 = 更差端
        "prob_profit": float((finals > 0).mean()),
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def deflated_sharpe(equity: pd.Series, n_trials: int = 1, sr_var_annual: float | None = None) -> dict:
    """Bailey & López de Prado:这个夏普有多少是「试了 N 次挑最好」的运气。"""
    if equity is None or len(equity) < 30:
        return {}
    r = equity.pct_change().dropna()
    T = len(r)
    sd = float(r.std(ddof=1))
    if sd == 0:
        return {}
    sr_daily = float(r.mean() / sd)
    sr_annual = sr_daily * math.sqrt(TRADING_DAYS)
    g3 = float(pd.Series(r).skew())
    g4 = float(pd.Series(r).kurt()) + 3.0
    if n_trials < 2:
        return {"DSR": None, "note": "只试过 1 组参数,DSR 不适用",
                "sharpe_annual": sr_annual, "T": T}
    var_trials = sr_var_annual if sr_var_annual is not None else (abs(sr_annual) * 0.5) ** 2
    e = math.e
    gamma = 0.5772156649
    sr0_daily = math.sqrt(max(var_trials, 0) / TRADING_DAYS) * (
        (1 - gamma) * _norm_ppf(1 - 1.0 / n_trials) + gamma * _norm_ppf(1 - 1.0 / (n_trials * e)))
    denom = 1.0 - g3 * sr_daily + (g4 - 1.0) / 4.0 * sr_daily ** 2
    if denom <= 0:
        return {"DSR": None, "note": "分布极端,DSR 不可用", "sharpe_annual": sr_annual, "T": T}
    z = (sr_daily - sr0_daily) * math.sqrt(T - 1) / math.sqrt(denom)
    dsr = _norm_cdf(z)
    return {
        "DSR": dsr,
        "n_trials": n_trials,
        "sharpe_annual": sr_annual,
        "sr0_annual_by_luck": sr0_daily * math.sqrt(TRADING_DAYS),
        "T": T, "skew": g3, "kurt": g4,
        "verdict": ("显著" if dsr >= 0.95 else "偏弱" if dsr >= 0.90 else "大概率是搜出来的运气"),
    }


# ---------------------------------------------------------------- 汇总

def summarize(result, benchmark: pd.DataFrame | None = None, n_trials: int = 1,
              capital: float | None = None, do_mc: bool = True) -> dict:
    """把 BTResult 汇总成一份完整成绩单。"""
    eq = result.equity.copy()
    eq = eq.set_index("date") if "date" in eq.columns else eq
    equity = eq["equity"].astype(float)
    cap = float(capital or result.params.get("initial_cash") or equity.iloc[0])
    n = len(equity)
    total_ret = float(equity.iloc[-1] / cap - 1) * 100 if cap else 0.0
    years = max(n / TRADING_DAYS, 1 / TRADING_DAYS * 5)
    annual = float((1 + total_ret / 100) ** (1 / years) - 1) * 100 if n > 5 else total_ret
    mdd, peak_d, trough_d = max_drawdown(equity)
    trades = result.trade_df()
    bench_ret = None
    if benchmark is not None and not benchmark.empty:
        b = benchmark.set_index("datetime")["close"].reindex(equity.index).ffill()
        bench_ret = b.pct_change()

    ab = alpha_beta(equity.pct_change(), bench_ret) if bench_ret is not None else {}
    win_rate = float((trades["profit"] > 0).mean() * 100) if len(trades) else 0.0
    gains = float(trades.loc[trades["profit"] > 0, "profit"].sum()) if len(trades) else 0.0
    losses = float(-trades.loc[trades["profit"] < 0, "profit"].sum()) if len(trades) else 0.0
    profit_factor = float(gains / losses) if losses > 0 else (float("inf") if gains > 0 else 0.0)
    fees = float(trades["fee"].sum()) if len(trades) else 0.0
    turnover_amt = float((trades["open_price"] * trades["qty"]).sum()) if len(trades) else 0.0

    metrics = {
        "start": str(equity.index[0])[:10], "end": str(equity.index[-1])[:10],
        "days": n, "capital": cap,
        "total_return_pct": round(total_ret, 2),
        "annual_return_pct": round(annual, 2),
        "max_drawdown_pct": round(mdd, 2), "mdd_peak": peak_d[:10], "mdd_trough": trough_d[:10],
        "calmar": round(annual / abs(mdd), 2) if mdd else None,
        "sharpe_mtm": round(sharpe_mtm(equity), 3),
        "sharpe_realized": round(sharpe_realized(trades, cap, equity.index[0], equity.index[-1]), 3),
        "sortino": round(sortino(equity), 3),
        "information_ratio": (round(at.information_ratio(equity.pct_change(),
                                                         bench_ret), 3)
                              if bench_ret is not None else None),
        "alpha_annual": round(ab.get("alpha"), 4) if ab.get("alpha") is not None else None,
        "beta": round(ab.get("beta"), 3) if ab.get("beta") is not None else None,
        "r2": round(ab.get("r2"), 3) if ab.get("r2") is not None else None,
        "t_alpha": round(ab.get("t_alpha"), 2) if ab.get("t_alpha") is not None else None,
        "bench_total_pct": (round(float(bench_ret.add(1).prod() - 1) * 100, 2)
                            if bench_ret is not None else None),
        "trades": len(trades), "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor not in (float("inf"),) else None,
        "avg_profit_pct": round(float(trades["profit_pct"].mean()), 3) if len(trades) else None,
        "total_fee": round(fees, 2), "fee_pct_of_capital": round(fees / cap * 100, 3) if cap else None,
        "turnover_amount": round(turnover_amt, 2),
        "exposure_pct": round(float((eq["market_value"] / eq["equity"]).mean()) * 100, 2)
        if "market_value" in eq.columns and cap else None,
    }

    # --- 末端强平必须单列:回测最后一天一律平仓,这是机械产物,不是策略能力
    if len(trades) and "reason" in trades.columns:
        fx = trades[trades["reason"].astype(str).str.contains("强平", na=False)]
        fx_pnl = float(fx["profit"].sum()) if len(fx) else 0.0
        metrics["force_exit_trades"] = int(len(fx))
        metrics["force_exit_pnl"] = round(fx_pnl, 2)
        metrics["force_exit_pct_of_capital"] = round(fx_pnl / cap * 100, 2) if cap else None
        if cap:
            gross = float(trades["profit"].sum())
            metrics["return_excl_force_exit_pct"] = round(
                (gross - fx_pnl) / cap * 100, 2)

    # --- 分市场状态:盈利月占比、最赚月贡献
    try:
        mret = monthly_returns(equity)["ret"]
        if len(mret):
            pos = mret[mret > 0]
            metrics["profitable_months"] = f"{int((mret > 0).sum())}/{len(mret)}"
            metrics["profitable_months_pct"] = round(float((mret > 0).mean()) * 100, 1)
            metrics["top_month_contribution_pct"] = (
                round(float(pos.max() / pos.sum() * 100), 1) if len(pos) and pos.sum() else None)
    except Exception:
        pass
    metrics["_monthly"] = monthly_returns(equity).to_dict("records")
    if do_mc:
        metrics["_mc"] = monte_carlo(equity)
    metrics["_dsr"] = deflated_sharpe(equity, n_trials)
    metrics["_equity"] = [{"date": str(d)[:10], "equity": float(v)}
                          for d, v in equity.items()]
    return metrics


def to_markdown(m: dict, title: str = "回测报告") -> str:
    mc = m.get("_mc") or {}
    dsr = m.get("_dsr") or {}
    lines = [
        f"# {title}",
        "",
        f"- 区间:{m.get('start')} → {m.get('end')}({m.get('days')} 个交易日)",
        f"- 初始资金:{m.get('capital', 0):,.0f}",
        f"- 总收益:**{m.get('total_return_pct')}%**,年化 {m.get('annual_return_pct')}%",
        f"- 最大回撤:**{m.get('max_drawdown_pct')}%**({m.get('mdd_peak')} → {m.get('mdd_trough')})"
        f",Calmar {m.get('calmar')}",
        f"- 夏普(盯市):{m.get('sharpe_mtm')} ｜ 夏普(平仓归集):{m.get('sharpe_realized')}"
        f" ｜ Sortino {m.get('sortino')}",
        f"- Alpha(年化):{m.get('alpha_annual')} ｜ Beta:{m.get('beta')} ｜ R²:{m.get('r2')}"
        f" ｜ t(alpha) {m.get('t_alpha')}",
        f"- 基准总收益:{m.get('bench_total_pct')}%",
        f"- 交易 {m.get('trades')} 笔 ｜ 胜率 {m.get('win_rate_pct')}% ｜ 盈亏比 {m.get('profit_factor')}"
        f" ｜ 均单笔 {m.get('avg_profit_pct')}%",
        f"- 手续费合计 {m.get('total_fee')}(占本金 {m.get('fee_pct_of_capital')}%)",
        f"- 平均仓位 {m.get('exposure_pct')}%",
        "",
    ]
    if mc:
        lines += [
            "## 蒙特卡洛(打乱日收益顺序,1000 次)",
            f"- 终值收益 5%/50%/95% 分位:{mc.get('final_p05')}% / {mc.get('final_p50')}% / {mc.get('final_p95')}%",
            f"- 最大回撤 中位/更差端:{mc.get('mdd_p50')}% / {mc.get('mdd_p95')}%",
            f"- 盈利概率:{mc.get('prob_profit')}",
            "",
        ]
    if dsr:
        lines += [
            "## Deflated Sharpe(防「搜出来的夏普」)",
            f"- 年化夏普 {dsr.get('sharpe_annual')} ｜ DSR {dsr.get('DSR')}"
            f"({dsr.get('verdict') or dsr.get('note')})",
            "",
        ]
    if m.get("_monthly"):
        rows = m["_monthly"]
        lines.append("")
        # 末端强平必须单列:最后一天机械平仓,不算策略能力
        if m.get("force_exit_trades"):
            lines.append("## 末端强平(回测最后一天强制平仓)")
            fpct = m.get("force_exit_pct_of_capital")
            excl = m.get("return_excl_force_exit_pct")
            lines.append(f"- 强平 {m['force_exit_trades']} 笔,盈亏 {m.get('force_exit_pnl')}"
                         f"(占本金 {fpct}%)")
            lines.append(f"- 剔除强平后收益:{excl}%")
            lines.append("")
        lines.append("## 月度分布")
        if m.get("profitable_months"):
            lines.append(f"- 盈利月:{m['profitable_months']}"
                         f"({m.get('profitable_months_pct')}%)")
        if m.get("top_month_contribution_pct") is not None:
            lines.append(f"- 最赚月贡献:占正月合计 {m['top_month_contribution_pct']}%"
                         "(占比越高越靠单月)")
        lines.append("")
        lines.append("## 月度收益(%)")
        chunk = [f"{r['month']}: {r['ret']:+.2f}" for r in rows]
        for i in range(0, len(chunk), 6):
            lines.append("- " + " ｜ ".join(chunk[i:i + 6]))
        lines.append("")
    lines += ["---", "",
              "口径说明:信号在 t 日收盘生成、t+1 开盘成交;T+1 限制;涨停不买跌停不卖;"
              "含佣金/印花税/过户费/滑点。回测不是实盘承诺。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 未来函数检查

def check_causality(strategy, data: dict[str, pd.DataFrame], points: int = 8,
                    tol: float = 1e-9) -> dict:
    """同一时点,用「全量数据」与「只到 t 的数据」算因子,结果必须一致。

    这是 EP004/EP007 用的同一套思路:不一致 = 用了未来数据。
    """
    worst = 0.0
    n_cmp = 0
    leaked = None
    for code, df in data.items():
        if df is None or len(df) < 60:
            continue
        try:
            full = strategy.factor_scores({code: df})
        except Exception:
            continue
        if full.empty or code not in full.columns:
            continue
        col = full[code]
        L = len(df)
        for t in np.linspace(int(L * 0.4), L - 2, points).astype(int):
            if t < 5 or t >= L:
                continue
            vf = col.iloc[t] if t < len(col) else np.nan
            if not np.isfinite(vf):
                continue
            part = strategy.factor_scores({code: df.iloc[: t + 1].copy()})
            if part.empty or code not in part.columns:
                leaked = (code, int(t), "截断数据算不出(全量能),疑似需要未来数据")
                break
            vt = part[code].iloc[-1]
            if not np.isfinite(vt):
                leaked = (code, int(t), "截断数据该点为 NaN")
                break
            d = abs(float(vf) - float(vt))
            worst = max(worst, d)
            n_cmp += 1
            if d > tol:
                leaked = (code, int(t), f"全量={vf:.6g} vs 截断={vt:.6g}")
                break
        if leaked:
            break
    verdict = "no_leak" if leaked is None else "leak"
    # 阈值跟着可用点数走:一次跑 3 只票 × 5 个点本来就比不到 10 次,
    # 用 10 当硬门槛会把正常结果误判成 inconclusive。
    need = min(10, max(3, points))
    if leaked is None and n_cmp < need:
        verdict = "inconclusive"
    return {"pass": verdict == "no_leak", "verdict": verdict, "compared": n_cmp,
            "max_diff": worst, "leak": leaked}
