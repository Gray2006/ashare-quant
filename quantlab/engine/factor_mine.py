# -*- coding: utf-8 -*-
"""策略因子挖掘:批量试表达式,按 IC/ICIR/分层收益筛,自动去重。

口径(和 factor_analysis 对齐):
  * 因子值用 dsl 表达式逐票求值,横截面函数(rank/zscore)传 xs=panel;
  * 预测收益 = close.shift(-horizon)/close - 1,同日横截面;
  * IC = 每日 rank 相关(Spearman),ICIR = mean(IC)/std(IC);
  * 换手 = 1 - 相邻两日因子排名的相关;
  * 分层 = 每日按因子分 5 组,取各组平均预测收益,多空 = 第 5 组 - 第 1 组。

只做统计筛选,不训练模型;结果写 USER_DIR/factor_library.json,供回测/策略引用。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import USER_DIR
from . import dsl
from . import factor_analysis as fa

RESULT_DIR = USER_DIR / "factor_mine"
CRYPTO_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                  "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
                  "LTCUSDT", "BCHUSDT", "TRXUSDT", "TONUSDT", "SUIUSDT",
                  "APTUSDT", "ARBUSDT", "OPUSDT", "NEARUSDT", "FILUSDT")

LIBRARY = USER_DIR / "factor_library.json"

# 表达式模板:{n} 是窗口。方向不预设,IC 正负由数据说话。
TEMPLATES: list[tuple[str, str]] = [
    ("动量", "roc(close,{n})"),
    ("反转", "-roc(close,{n})"),
    ("价格位置", "(close-llv(close,{n}))/(hhv(close,{n})-llv(close,{n})+1e-9)"),
    ("均线偏离", "close/ma(close,{n})-1"),
    ("波动", "-ts_std(roc(close,1),{n})"),
    ("量比", "volume/ts_mean(volume,{n})-1"),
    ("量价相关", "ts_corr(close,volume,{n})"),
    ("成交额趋势", "ts_mean(amount,{n})/amount-1"),
    ("RSI反转", "50-rsi({n})"),
    ("价格Z", "(close-ts_mean(close,{n}))/ts_std(close,{n})"),
    # ---- 下面几条是照 qlib Alpha158 的算子补的:趋势的斜率/干净度/残差、
    # 收益率分布形状、量价协方差。原来只有均值和标准差,因子空间窄一大截。
    ("趋势斜率", "slope(close,{n})/(close+1e-9)"),
    ("趋势干净度", "rsquare(close,{n})"),
    ("趋势残差", "resi(close,{n})/(close+1e-9)"),
    ("收益偏度", "-skew(roc(close,1),{n})"),
    ("收益峰度", "-kurt(roc(close,1),{n})"),
    ("中位偏离", "quantile(close,{n},0.5)/(close+1e-9)-1"),
    ("量价协方差", "ts_cov(roc(close,1),volume/ts_mean(volume,{n}),{n})"),
]

WINDOWS = (3, 5, 10, 20, 60)


def candidates(windows: tuple[int, ...] = WINDOWS) -> list[dict]:
    out = []
    for label, tpl in TEMPLATES:
        for n in windows:
            out.append({"label": f"{label}{n}", "expr": tpl.format(n=n),
                        "window": n})
    return out


def _crypto_panel(days: int = 250) -> dict:
    """加密日线:宽表格式和 A 股一致(close/volume/... dates×codes)。"""
    from ..crypto import feed

    frames = {}
    for sym in CRYPTO_SYMBOLS:
        try:
            rows = feed.klines(sym, "1d", limit=int(days))
        except Exception:  # noqa: BLE001
            continue
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df.index = pd.to_datetime(df["t"], unit="ms").dt.normalize()
        frames[sym] = df[["o", "h", "l", "c", "v"]].rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close",
                     "v": "volume"}).astype("float64")
    if not frames:
        return {}
    # 有的币种会返回很早的一段(缓存/接口异常),和最新日期差太远就剔掉,
    # 否则 union 出两段不相交日期,覆盖率直接腰斩。
    newest = max(df.index.max() for df in frames.values())
    frames = {k: v for k, v in frames.items()
              if v.index.max() >= newest - pd.Timedelta(days=5)}
    if not frames:
        return {}
    idx = sorted(set().union(*[set(df.index) for df in frames.values()]))
    # 只保留覆盖率 >= 30% 的日期(去掉个别币种停牌造成的空段)
    cov = pd.Series(0, index=pd.DatetimeIndex(idx))
    for df in frames.values():
        cov = cov.add(df.index.to_series().value_counts(), fill_value=0)
    keep_idx = cov[cov >= max(3, int(len(frames) * 0.3))].index
    idx = sorted(keep_idx)
    if not idx:
        return {}
    out = {}
    for name in ("open", "high", "low", "close", "volume"):
        out[name] = pd.DataFrame({sym: df[name].reindex(idx)
                                  for sym, df in frames.items()}).reindex(idx)
    out["amount"] = out["close"] * out["volume"]
    out["codes"] = list(frames)
    out["last_day"] = str(idx[-1].date())
    return out


def _panel(days: int, max_stocks: int, market: str = "a",
           asof: str | None = None) -> dict:
    """取横截面面板。

    asof:按哪一天的成交额选票。默认 None = 最近一天(选股/看盘)。
          **回测要传样本起点** —— 用今天的成交额挑历史池子是幸存者偏差,
          池子里全是后来变热的票,天然跑赢基准。
    """
    if market == "crypto":
        return _crypto_panel(days)
    from .alpha_pick import load_panel
    return load_panel(days=days, max_stocks=max_stocks, asof=asof)


def _panel_unbiased(days: int, max_stocks: int, market: str = "a") -> dict:
    """按**样本起点**的成交额选池子,避免用今天的温度挑历史票。

    用今天的前 N 名去回测过去 N 天,池子里全是一路变热的票,天然跑赢基准。
    实测同一组因子:有偏 +120.7% 超额,无偏 -22.4%。
    """
    base = _panel(days, max_stocks, market)
    close = base.get("close") if isinstance(base, dict) else None
    if close is None or close.empty:
        return base
    first = str(close.index[0])[:10]
    return _panel(days, max_stocks, market, asof=first)


def _is_wide(panel: dict) -> bool:
    """alpha_pick.load_panel 的格式:{"close": dates×codes, "volume": ...}。"""
    c = panel.get("close") if isinstance(panel, dict) else None
    return isinstance(c, pd.DataFrame)


def _frames(panel: dict) -> dict[str, pd.DataFrame]:
    """宽表 → 每只票一条 DataFrame(dsl 要按票求值)。"""
    if not _is_wide(panel):
        return {k: v for k, v in panel.items() if isinstance(v, pd.DataFrame)}
    close = panel.get("close")
    if close is None or close.empty:
        return {}
    out = {}
    for code in close.columns:
        df = pd.DataFrame({"datetime": close.index})
        for name in ("open", "high", "low", "close", "volume", "amount"):
            wide = panel.get(name)
            if isinstance(wide, pd.DataFrame) and code in wide.columns:
                df[name] = wide[code].to_numpy()
        out[str(code)] = df
    return out


def _fwd_panel(panel: dict, horizon: int) -> pd.DataFrame:
    """预测收益面板:close.shift(-h)/close-1。宽表和逐票字典都支持。"""
    if _is_wide(panel):
        c = panel["close"].astype("float64")
        return (c.shift(-horizon) / c - 1.0).sort_index()
    return fa._fwd_ret(panel, horizon)


def _factor_panel(panel: dict[str, pd.DataFrame], expr: str) -> pd.DataFrame:
    """逐票求值,拼成 index=日期、columns=代码 的因子面板。"""
    frames = _frames(panel)
    xs = frames if frames else panel
    cols = {}
    for code, df in frames.items():
        if df is None or df.empty or "datetime" not in df:
            continue
        try:
            vals = dsl.evaluate_value(expr, df, code, xs=xs)
        except Exception:  # noqa: BLE001
            continue
        s = pd.Series(vals, index=pd.DatetimeIndex(df["datetime"]))
        cols[code] = s[~s.index.duplicated(keep="last")]
    if not cols:
        return pd.DataFrame()
    return pd.DataFrame(cols).sort_index()


def _metrics(fac: pd.DataFrame, fwd: pd.DataFrame, horizon: int) -> dict:
    fac, fwd = fac.align(fwd, join="inner")
    if fac.empty or len(fwd) < 20:
        return {}
    # 每日 rank 相关(Spearman),忽略样本太少的横截面
    ic_rows, ic_dates = [], []
    for day in fac.index:
        a, b = fac.loc[day], fwd.loc[day]
        m = a.notna() & b.notna()
        if int(m.sum()) < 10:
            continue
        ic_rows.append(a[m].rank().corr(b[m].rank()))
        ic_dates.append(day)
    ic = pd.Series(ic_rows, index=pd.DatetimeIndex(ic_dates)).dropna()
    if len(ic) < 30:
        return {}
    mean, std = float(ic.mean()), float(ic.std(ddof=1) or 0.0)
    ir = mean / std if std > 1e-12 else 0.0
    t = ir * np.sqrt(len(ic))
    win = float((ic > 0).mean())
    coverage = float(fac.notna().mean().mean())
    ranks = fac.rank(axis=1)
    turns = []
    idx = list(ranks.index)
    for i in range(1, len(idx)):
        a, b = ranks.iloc[i], ranks.iloc[i - 1]
        m = a.notna() & b.notna()
        if int(m.sum()) >= 10:
            turns.append(1 - a[m].corr(b[m]))
    turnover = float(np.nanmean(turns)) if turns else 0.0

    # 分层
    layer_means = []
    for day in fac.index:
        a, b = fac.loc[day], fwd.loc[day]
        m = a.notna() & b.notna()
        if int(m.sum()) < 20:
            continue
        try:
            q = pd.qcut(a[m].rank(method="first"), 5, labels=False)
        except ValueError:
            continue
        layer_means.append(b[m].groupby(q).mean())
    if layer_means:
        lay = pd.concat(layer_means, axis=1).mean(axis=1).sort_index()
        layers = [round(float(v), 5) for v in lay.tolist()]
        spread = round(float(lay.iloc[-1] - lay.iloc[0]), 5)
        # 分层单调性:秩相关。pandas 的 method="spearman" 会去 import scipy,
        # 而 venv 里没有 scipy —— 先 rank 再算 Pearson 等价,且不依赖 scipy。
        mono = round(float(pd.Series(layers).rank().corr(
            pd.Series(range(5)).rank())), 3)
    else:
        layers, spread, mono = [], 0.0, 0.0
    # 样本外:前 70% 训练,后 30% 测试;再看 3 段稳定性
    n_tr = max(20, int(len(ic) * 0.7))
    tr, te = ic.iloc[:n_tr], ic.iloc[n_tr:]
    tr_m, tr_s = float(tr.mean()), float(tr.std(ddof=1) or 0.0)
    te_m, te_s = float(te.mean()), float(te.std(ddof=1) or 0.0)
    tr_ir = tr_m / tr_s if tr_s > 1e-12 else 0.0
    te_ir = te_m / te_s if te_s > 1e-12 else 0.0
    folds = [ic.iloc[i * len(ic) // 3:(i + 1) * len(ic) // 3] for i in range(3)]
    fold_means = [float(f.mean()) if len(f) >= 10 else 0.0 for f in folds]
    same_sign = sum(1 for m in fold_means if m * tr_m > 0 and abs(m) >= 0.01)
    stability = round(same_sign / 3.0, 2)
    # 样本外门槛。原来的版本只看测试段(|te_m| ≥ 0.015),结果「训练 IC 0.012、
    # 测试 IC 0.10」这种也能过 —— 测试段只有 30% 的时间,IC 偶然冲高很常见。
    # 现在要求:训练段也得有东西、测试段不能远超训练(超过 3 倍 + 0.02 视为不稳)。
    same_dir = (tr_m > 0) == (te_m > 0)
    oos_ok = (same_dir and abs(te_m) >= 0.015 and abs(tr_m) >= 0.010
              and len(te) >= 15
              and abs(te_m) <= 3.0 * abs(tr_m) + 0.02)
    if oos_ok and stability >= 0.66 and abs(te_ir) >= 0.2:
        risk = "低"
    elif oos_ok:
        risk = "中"
    else:
        risk = "高"
    return {"ic": round(mean, 4), "icir": round(ir, 3), "win": round(win, 3),
            "t": round(float(t), 2), "n": len(ic), "coverage": round(coverage, 3),
            "turnover": round(turnover, 3), "spread": spread, "mono": mono,
            "layers": layers, "horizon": horizon,
            "train_ic": round(tr_m, 4), "train_icir": round(tr_ir, 3),
            "test_ic": round(te_m, 4), "test_icir": round(te_ir, 3),
            "oos_ok": bool(oos_ok), "stability": stability, "risk": risk,
            "fitness": round(abs(tr_ir) + abs(te_ir) if oos_ok else 0.0, 4),
            "ic_series": [[str(d)[:10], round(float(v), 4)]
                          for d, v in ic.items()]}


def evaluate_expr(panel: dict[str, pd.DataFrame], expr: str,
                  horizon: int = 5) -> dict:
    fac = _factor_panel(panel, expr)
    if fac.empty:
        return {}
    fwd = _fwd_panel(panel, horizon)
    return _metrics(fac, fwd, horizon)


def _space_gate(cands: list[dict], ignore: bool = False) -> list[dict]:
    """按搜索空间预算筛候选。判死的空间(比如日线价量)不再抽新表达式。

    ignore=True 是给人工探索用的:界面上点「挖一次」可以越权,
    每日自动作业不行 —— 自动作业才是重复烧样本的地方。
    """
    if ignore:
        return list(cands)
    try:
        from . import factor_space as fsp
        left = fsp.budget()
    except Exception:  # noqa: BLE001
        return cands
    kept: list[dict] = []
    for c in cands:
        try:
            sp = fsp.classify(str(c.get("expr") or ""))
        except Exception:  # noqa: BLE001
            sp = "unknown"
        if int(left.get(sp, 0)) <= 0:
            continue
        left[sp] = int(left[sp]) - 1
        kept.append(c)
    return kept


def _gate_info(n_in: int, n_out: int) -> dict:
    try:
        from . import factor_space as fsp
        return {"in": int(n_in), "out": int(n_out), "budget": fsp.budget()}
    except Exception:  # noqa: BLE001
        return {"in": int(n_in), "out": int(n_out)}


def _record_spaces(rows: list[dict]) -> None:
    """把这一轮挖到的表达式按空间记账。"""
    try:
        from collections import Counter
        from . import factor_space as fsp
        cnt = Counter(fsp.classify(str(r.get("expr") or "")) for r in rows)
        for sp, n in cnt.items():
            fsp.record(sp, mined=int(n))
    except Exception:  # noqa: BLE001
        pass


def mine(days: int = 250, max_stocks: int = 300, horizon: int = 5,
         max_corr: float = 0.7, top: int = 30,
         windows: tuple[int, ...] = WINDOWS, market: str = "a",
         ignore_budget: bool = False) -> dict:
    """跑一批候选,返回排序 + 去重后的入选名单。"""
    panel = _panel_unbiased(days, max_stocks, market)
    if not panel:
        return {"ok": False, "error": "本地没有 K 线数据,先让行情页拉一段历史",
                "rows": []}
    fwd = _fwd_panel(panel, horizon)
    rows = []
    panels: dict[str, pd.DataFrame] = {}
    cands = candidates(windows)
    gated = _space_gate(cands, ignore=ignore_budget)
    gate_info = _gate_info(len(cands), len(gated))
    for cand in gated:
        fac = _factor_panel(panel, cand["expr"])
        if fac.empty:
            continue
        st = _metrics(fac, fwd, horizon)
        if not st:
            continue
        panels[cand["expr"]] = fac
        rows.append({**cand, **st})

    rows.sort(key=lambda r: (bool(r.get("oos_ok")),
                             abs(float(r.get("test_icir") or 0)),
                             abs(float(r.get("icir") or 0))), reverse=True)
    # 贪心去重:和已入选的相关太高就跳过;样本外没通过的不选
    selected: list[dict] = []
    for r in rows:
        if len(selected) >= top:
            break
        if not r.get("oos_ok") or float(r.get("coverage") or 0) < 0.5:
            continue
        f = panels[r["expr"]].rank(axis=1)
        dup = False
        for s in selected:
            g = panels[s["expr"]].rank(axis=1)
            f2, g2 = f.align(g, join="inner")
            try:
                c = f2.corrwith(g2, axis=1).mean()
            except Exception:  # noqa: BLE001
                c = 1.0
            if abs(float(c)) >= max_corr:
                dup = True
                break
        if not dup:
            selected.append(r)
    for r in rows:
        r["selected"] = any(s["expr"] == r["expr"] for s in selected)
    _record_spaces(rows)
    return {"ok": True, "rows": rows, "selected": selected,
            "horizon": horizon, "n_candidates": len(rows),
            "n_codes": len(panel.get("codes") or panel), "days": days,
            "market": market, "ts": time.strftime("%H:%M:%S"),
            "space_gate": gate_info}


# ---------------------------------------------------------------- 因子库
def load_library() -> list[dict]:
    try:
        return json.loads(LIBRARY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save_factor(expr: str, label: str = "", stats: dict | None = None) -> bool:
    expr = (expr or "").strip()
    if not expr:
        return False
    try:
        dsl.compile_expr(expr)
    except Exception:  # noqa: BLE001
        return False
    lib = load_library()
    if any(x.get("expr") == expr for x in lib):
        return False
    lib.append({"expr": expr, "label": (label or expr)[:80],
                "stats": {k: v for k, v in (stats or {}).items()
                          if k in ("ic", "icir", "win", "coverage", "turnover",
                                   "spread", "mono", "horizon", "train_ic",
                                   "train_icir", "test_ic", "test_icir",
                                   "stability", "risk", "oos_ok")},
                "created": time.strftime("%Y-%m-%d %H:%M")})
    try:
        LIBRARY.parent.mkdir(parents=True, exist_ok=True)
        LIBRARY.write_text(json.dumps(lib, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def remove_factor(expr: str) -> bool:
    lib = [x for x in load_library() if x.get("expr") != expr]
    try:
        LIBRARY.write_text(json.dumps(lib, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 组合回测
def score_panel(panel: dict, factors: list[dict]) -> pd.DataFrame:
    """把多个因子按 IC 方向合成一个横截面分数(rank 后等权)。"""
    total = None
    for f in factors:
        fac = _factor_panel(panel, str(f.get("expr") or ""))
        if fac.empty:
            continue
        r = fac.rank(axis=1, pct=True)
        # 方向优先用样本外 IC;没有才退回训练 IC
        sign = 1.0 if float(f.get("test_ic", f.get("ic")) or 0) >= 0 else -1.0
        part = (r - 0.5) * sign
        total = part if total is None else total.add(part, fill_value=0)
    if total is None:
        return pd.DataFrame()
    return total.div(max(1, len(factors)))


def composite_backtest(panel: dict, factors: list[dict], horizon: int = 5,
                       top_n: int = 5, cost: float = 0.0016,
                       stop_loss: float = 0.0) -> dict:
    """日频、每 horizon 天调仓、等权持有 top_n 的组合回测。

    只用于展示「这组因子在历史上大概什么水平」,不是实盘建议。
    """
    close = panel.get("close") if _is_wide(panel) else None
    if close is None or close.empty:
        return {"ok": False, "error": "没有行情面板"}
    close = close.astype("float64")
    score = score_panel(panel, factors)
    if score.empty:
        return {"ok": False, "error": "因子算不出分数"}
    score = score.reindex(close.index)
    rets = close.pct_change().fillna(0.0)

    # 选票用当日收盘的因子值,所以**当天不能享受当天的收益** ——
    # 收盘算出信号时这根 K 线已经走完了。原来的写法当天就持仓并乘当日收益,
    # 等于免费吃到一个已经发生过的涨幅(200 天能跑出 +1120%,夏普 4.8)。
    # 正确顺序:收盘选票 → 记下待换 → 明天起才持有。
    equity, curve, holdings, prev = 1.0, [], [], set()
    pending: list[str] | None = None
    turnover_sum = 0.0
    # 止损状态:entry_px 记入场价,pending_sell 记「该卖但 T+1 卖不掉」的票
    entry_px: dict[str, float] = {}
    pending_sell: set[str] = set()
    stop_hits = 0
    for i, d in enumerate(close.index):
        if i % max(1, int(horizon)) == 0:
            row = score.loc[d].dropna() if d in score.index else pd.Series(dtype=float)
            row = row.reindex(close.columns).dropna()
            if len(row) >= max(3, top_n):
                picked = list(row.nlargest(int(top_n)).index)
                # 换手率只算「买进来的那部分」。原来用对称差(集合 ^)除以持仓数,
                # 全换仓时 turn=2,成本被扣成两倍 —— 保守但不对。
                turn = len(set(picked) - prev) / max(1, len(picked))
                equity *= (1 - float(cost) * turn)
                turnover_sum += turn
                prev = set(picked)
                pending = picked
        # T+1 补卖:昨天该止损但卖不掉的,今天只要还亏着就卖
        if pending_sell and holdings:
            row_px = close.loc[d]
            for code in list(pending_sell):
                if code not in holdings:
                    pending_sell.discard(code)
                    entry_px.pop(code, None)
                    continue
                e = entry_px.get(code)
                px = float(row_px.get(code) or 0)
                if not e or px <= 0:
                    continue
                if px / e - 1 >= 0:        # 回本了,不强制卖
                    pending_sell.discard(code)
                    continue
                # 卖掉:从持仓移除,收益按今天收盘算
                holdings = [h for h in holdings if h != code]
                entry_px.pop(code, None)
                pending_sell.discard(code)
                stop_hits += 1
                equity *= (1 - float(cost))     # 卖出成本

        if holdings:
            r = float(rets.loc[d].reindex(holdings).mean())
            equity *= (1 + r)

        # 止损检查:当天跌破止损线 → 挂起,明天才能卖(T+1)
        if stop_loss > 0 and holdings:
            row_px = close.loc[d]
            for code in list(holdings):
                e = entry_px.get(code)
                if not e:
                    continue
                px = float(row_px.get(code) or 0)
                if px > 0 and px / e - 1 <= -float(stop_loss):
                    pending_sell.add(code)

        curve.append([str(d)[:10], round(equity, 4)])
        if pending is not None:          # 收盘后换仓,明天才生效
            holdings = pending
            # 新进的票记入场价(次日开盘价,没有就用收盘)
            row_px = close.loc[d]
            for code in holdings:
                if code not in entry_px:
                    entry_px[code] = float(row_px.get(code) or 0) or 1.0
            # 已经不在持仓里的清掉
            for code in list(entry_px):
                if code not in holdings:
                    entry_px.pop(code, None)
            pending = None

    eq = pd.Series([v for _d, v in curve])
    daily = eq.pct_change().dropna()
    total = float(eq.iloc[-1] - 1.0) * 100 if len(eq) else 0.0
    n = max(1, len(eq))
    cut = int(n * 0.7)
    oos_ret = ((float(eq.iloc[-1]) / float(eq.iloc[cut]) - 1) * 100
               if cut < n and float(eq.iloc[cut]) > 0 else 0.0)
    oos_daily = daily.iloc[cut:]
    oos_sharpe = (float(oos_daily.mean() / oos_daily.std(ddof=1) *
                        np.sqrt(252))
                  if len(oos_daily) > 5 and oos_daily.std(ddof=1) > 1e-12
                  else 0.0)
    annual = ((1 + total / 100) ** (252 / n) - 1) * 100 if n > 5 else total
    sharpe = float(daily.mean() / daily.std(ddof=1) * np.sqrt(252)) \
        if len(daily) > 5 and daily.std(ddof=1) > 1e-12 else 0.0
    peak, mdd = -1e9, 0.0
    for v in eq:
        peak = max(peak, float(v))
        mdd = min(mdd, float(v) / peak - 1.0)

    # 基准:同期全池等权(buy & hold,不调仓不扣费)。
    # 没有它,「总收益 131%」这种数字看不出多少是 beta ——
    # 一段普涨行情里任何策略都能赚,只有超额才是策略的功劳。
    bench_eq = (1.0 + rets.mean(axis=1)).cumprod()
    bench_total = float(bench_eq.iloc[-1] - 1.0) * 100 if len(bench_eq) else 0.0
    excess_total = total - bench_total
    bench_annual = ((1 + bench_total / 100) ** (252 / n) - 1) * 100 if n > 5 else bench_total
    excess_annual = annual - bench_annual

    return {
        "ok": True,
        "curve": curve,
        "metrics": {"total_return_pct": round(total, 2),
                    "annual_return_pct": round(annual, 2),
                    "sharpe": round(sharpe, 2),
                    "max_drawdown_pct": round(mdd * 100, 2),
                    "turnover_avg": round(turnover_sum / max(1, n // max(1, horizon)), 2),
                    "n_days": n, "top_n": int(top_n),
                    "horizon": int(horizon), "cost": float(cost),
                    "oos_return_pct": round(oos_ret, 2),
                    "oos_sharpe": round(oos_sharpe, 2),
                    # 基准与超额:只看绝对收益会被 beta 骗
                    "bench_total_pct": round(bench_total, 2),
                    "bench_annual_pct": round(bench_annual, 2),
                    "excess_total_pct": round(excess_total, 2),
                    "excess_annual_pct": round(excess_annual, 2),
                    "stop_loss": float(stop_loss),
                    "stop_hits": int(stop_hits)},
        "holdings": holdings,
    }


# ---------------------------------------------------------------- 每日自动
def _result_path(market: str) -> Path:
    return RESULT_DIR / f"{market}.json"


def save_result(market: str, res: dict) -> bool:
    try:
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        _result_path(market).write_text(json.dumps(
            {"date": time.strftime("%Y-%m-%d"), "saved": time.strftime("%H:%M"),
             "result": res}, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def load_result(market: str) -> dict:
    try:
        doc = json.loads(_result_path(market).read_text(encoding="utf-8"))
        return doc if doc.get("result") else {}
    except Exception:  # noqa: BLE001
        return {}


def auto_due(market: str, today: str | None = None) -> bool:
    today = today or time.strftime("%Y-%m-%d")
    doc = load_result(market)
    return str(doc.get("date") or "") != today


def run_daily(markets: tuple[str, ...] = ("a", "crypto"), days: int = 250,
              max_stocks: int = 300, horizon: int = 5) -> dict:
    """启动时调用:每个市场每天最多跑一次,结果落盘。"""
    out = {}
    for m in markets:
        if not auto_due(m):
            out[m] = {"skipped": "今天已经跑过"}
            continue
        # 空间预算为 0 → 这个信息集已经被证伪,再跑就是重复烧样本
        try:
            from . import factor_space as fsp
            if not fsp.can_mine("daily_ohlcv"):
                out[m] = {"skipped": "日线价量空间预算为 0(已证伪),换信息集再挖"}
                continue
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import factor_gp
            r = factor_gp.run(days=days, max_stocks=max_stocks,
                              horizon=horizon, market=m, generations=5,
                              population=24, time_budget=120)
            if r.get("ok"):
                save_result(m, r)
            out[m] = {"ok": bool(r.get("ok")), "rows": len(r.get("rows") or []),
                      "selected": len(r.get("selected") or []),
                      "error": r.get("error")}
        except Exception as e:  # noqa: BLE001
            out[m] = {"ok": False, "error": str(e)[:160]}
    return out


# ---------------------------------------------------------------- 持续挖掘
def _valid(expr: str) -> bool:
    if not expr or len(expr) > 220:
        return False
    try:
        dsl.compile_expr(expr)
        return True
    except Exception:  # noqa: BLE001
        return False


def _mutate(rows: list[dict], rng: np.random.Generator,
            limit: int = 14) -> list[dict]:
    """从头部因子里变异/组合出一批新表达式。"""
    top = [r for r in rows if r.get("selected")][:6] or rows[:6]
    exprs = [str(r.get("expr") or "") for r in top if r.get("expr")]
    out: list[dict] = []
    seen = {str(r.get("expr") or "") for r in rows}

    def add(expr: str, label: str):
        if len(out) >= limit or expr in seen or not _valid(expr):
            return
        seen.add(expr)
        out.append({"label": label[:60], "expr": expr, "window": 0,
                    "mutation": True})

    # 1) 单因子变形:取反、平滑、变化率、横截面 rank
    for e in exprs:
        add(f"-({e})", "取反")
        add(f"ts_mean({e},5)", "5日平滑")
        add(f"delta({e},3)", "3日变化")
        add(f"rank({e})", "横截面rank")

    # 2) 两两组合:加/减/乘/比(分母加保护)
    pairs = [(a, b) for i, a in enumerate(exprs) for b in exprs[i + 1:]]
    rng.shuffle(pairs)
    for a, b in pairs:
        op = str(rng.choice(["+", "-", "*"], p=[0.3, 0.4, 0.3]))
        add(f"({a}){op}({b})", f"组合{op}")
        if len(out) >= limit:
            break
    if len(out) < limit:
        for a, b in pairs:
            add(f"({a})/(({b})+1e-9)", "比值")
            if len(out) >= limit:
                break
    return out


def iter_mine(days: int = 250, max_stocks: int = 300, horizon: int = 5,
              market: str = "a", rounds: int = 8, max_corr: float = 0.7,
              time_budget: float = 300.0, seed: int = 7,
              progress=None, should_stop=None,
              ignore_budget: bool = False) -> dict:
    """持续挖:每轮在上一轮头部因子上变异,直到轮数/时间/无提升。

    progress: 每轮结束回调 {"round","total","pool","best","improved"}
    should_stop: 无参可调用,返回 True 提前停
    """
    t0 = time.time()
    panel = _panel(days, max_stocks, market)
    if not panel:
        return {"ok": False, "error": "本地没有 K 线数据", "rows": []}
    fwd = _fwd_panel(panel, horizon)
    rng = np.random.default_rng(int(seed))
    pool: list[dict] = []
    seen: set[str] = set()
    fac_cache: dict[str, pd.DataFrame] = {}
    history = []
    best_ir = -1.0
    low_rounds = 0

    def _fac(expr: str) -> pd.DataFrame:
        f = fac_cache.get(expr)
        if f is None:
            f = _factor_panel(panel, expr)
            fac_cache[expr] = f
        return f

    def evaluate(batch: list[dict]) -> list[dict]:
        got = []
        for c in batch:
            expr = str(c.get("expr") or "")
            if expr in seen:
                continue
            seen.add(expr)
            fac = _fac(expr)
            if fac.empty:
                continue
            st = _metrics(fac, fwd, horizon)
            if not st or float(st.get("coverage") or 0) < 0.5:
                continue
            got.append({**c, **st})
        return got

    base = evaluate(_space_gate(candidates(), ignore=ignore_budget))
    pool.extend(base)
    _record_spaces(base)

    def rank_rows(rows: list[dict]) -> list[dict]:
        rows = sorted(rows, key=lambda r: (bool(r.get("oos_ok")),
                                           abs(float(r.get("test_icir") or 0)),
                                           abs(float(r.get("icir") or 0))),
                      reverse=True)
        chosen = []
        for r in rows:
            if len(chosen) >= 30:
                break
            if not r.get("oos_ok"):
                continue
            dup = False
            f = _fac(r["expr"]).rank(axis=1)
            for s in chosen:
                g = _fac(s["expr"]).rank(axis=1)
                f2, g2 = f.align(g, join="inner")
                try:
                    c = f2.corrwith(g2, axis=1).mean()
                except Exception:  # noqa: BLE001
                    c = 1.0
                if abs(float(c)) >= max_corr:
                    dup = True
                    break
            if not dup:
                chosen.append(r)
        for r in rows:
            r["selected"] = any(s["expr"] == r["expr"] for s in chosen)
        return chosen

    chosen = rank_rows(pool)
    for rd in range(1, max(1, int(rounds)) + 1):
        if should_stop and should_stop():
            break
        if time.time() - t0 > float(time_budget):
            break
        new = _mutate(pool, rng, limit=14)
        got = evaluate(new)
        pool.extend(got)
        chosen = rank_rows(pool)
        cur = max([abs(float(r.get("icir") or 0)) for r in chosen] or [0.0])
        improved = cur > best_ir + 1e-6
        if improved:
            best_ir = cur
            low_rounds = 0
        else:
            low_rounds += 1
        history.append({"round": rd, "new": len(got), "pool": len(pool),
                        "best_icir": round(cur, 3), "improved": improved})
        if progress:
            try:
                snap = sorted(pool, key=lambda r: abs(float(r.get("icir") or 0)),
                              reverse=True)[:60]
                progress({"round": rd, "total": rounds, "pool": len(pool),
                          "best": chosen[:5], "improved": improved,
                          "new": len(got), "rows": snap,
                          "selected_exprs": [x["expr"] for x in chosen]})
            except Exception:  # noqa: BLE001
                pass
        if low_rounds >= 2 and rd >= 3:
            break

    rows = sorted(pool, key=lambda r: (bool(r.get("oos_ok")),
                                       abs(float(r.get("test_icir") or 0)),
                                       abs(float(r.get("icir") or 0))),
                  reverse=True)
    return {"ok": True, "rows": rows, "selected": chosen,
            "horizon": horizon, "n_candidates": len(rows),
            "n_codes": len(panel.get("codes") or panel), "days": days,
            "market": market, "rounds": len(history),
            "rounds_history": history,
            "cost_seconds": round(time.time() - t0, 1),
            "ts": time.strftime("%H:%M:%S")}
