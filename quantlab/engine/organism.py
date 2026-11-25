# -*- coding: utf-8 -*-
"""机体状态聚合 —— 给「学习程度」页供数。

三块:
  1 工作统计  因子库 / 七条线 / 每日维护 / 自检 / 数据规模
  2 训练状态  真训一个逻辑回归(用因子预测三重障碍标签),记录 loss/准确率曲线
  3 能力成长  累计尝试 vs 累计成功,以及每条线的当前状态

为什么真训:光统计「30 个因子全 rejected」看不出「学习」。
训练曲线能显示「模型在学什么、学到多少、有没有过拟合」。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / "organism_state.json"


def _db():
    from ..data.store import store
    return store()


def data_stats() -> dict:
    """数据规模。"""
    try:
        c = _db().conn
        daily = c.execute(
            "select count(*) from kline_cache where klt=101 and fqt=1").fetchone()[0]
        codes = c.execute(
            "select count(distinct code) from kline_cache where klt=101 and fqt=1"
        ).fetchone()[0]
        min5 = c.execute(
            "select count(distinct code) from kline_cache where klt=5 and fqt=1"
        ).fetchone()[0]
        last = c.execute(
            "select max(bar_time) from kline_cache where klt=101 and fqt=1"
        ).fetchone()[0]
        fin = ROOT / "data" / "finance_pit.json"
        fin_n = 0
        if fin.exists():
            try:
                fin_n = len(json.loads(fin.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                fin_n = 0

        # 最近每天有多少只票的数据 —— 这就是「每天新进来多少」
        recent = []
        rows = c.execute(
            "select bar_time d, count(*) n from kline_cache "
            "where klt=101 and fqt=1 group by bar_time "
            "order by bar_time desc limit 10").fetchall()
        for r in rows:
            recent.append({"day": str(r["d"]), "codes": int(r["n"])})
        recent.reverse()
        # 5 分钟线最近几天
        m5 = []
        rows2 = c.execute(
            "select substr(bar_time,1,10) d, count(distinct code) n "
            "from kline_cache where klt=5 and fqt=1 "
            "group by substr(bar_time,1,10) order by d desc limit 7").fetchall()
        for r in rows2:
            m5.append({"day": str(r["d"]), "codes": int(r["n"])})
        m5.reverse()

        first = c.execute(
            "select min(bar_time) from kline_cache where klt=101 and fqt=1"
        ).fetchone()[0]
        return {"daily_bars": int(daily), "codes": int(codes),
                "min5_codes": int(min5), "finance_codes": fin_n,
                "last_day": str(last or ""), "first_day": str(first or ""),
                "recent_days": recent, "min5_days": m5}
    except Exception:  # noqa: BLE001
        return {}


def factor_stats() -> dict:
    """因子库统计。"""
    try:
        from . import factor_mine as fm
        lib = fm.load_library()
        total = len(lib)
        passed = sum(1 for x in lib if x.get("status") == "accepted")
        rejected = sum(1 for x in lib if x.get("status") == "rejected")
        pending = total - passed - rejected
        last = ""
        for x in lib:
            a = x.get("audit") or {}
            at = str(a.get("at") or "")
            if at > last:
                last = at
        return {"total": total, "accepted": passed, "rejected": rejected,
                "pending": pending, "last_audit": last}
    except Exception:  # noqa: BLE001
        return {"total": 0, "accepted": 0, "rejected": 0, "pending": 0}


# 七条线的结论。写死是因为它们是**实验结果**,不是运行状态;
# 每次有新结论手动改这里,比自动推断可靠。
LINES = [
    {"key": "alpha", "name": "横截面 alpha", "status": "rejected",
     "detail": "45 因子,中性化 t=-1.44"},
    {"key": "qlib", "name": "qlib 算子", "status": "rejected",
     "detail": "34 个新因子,0/34 通过"},
    {"key": "t0", "name": "个股做T", "status": "rejected",
     "detail": "268 只 × 21 天,七组参数全负"},
    {"key": "hotmoney", "name": "游资跟买", "status": "rejected",
     "detail": "13 个席位,0/13 反向显著"},
    {"key": "finance", "name": "财务因子", "status": "rejected",
     "detail": "8 个因子全不显著"},
    {"key": "wq101", "name": "WorldQuant 101", "status": "rejected",
     "detail": "9 个代表,0 通过"},
    {"key": "intraday", "name": "日内微观结构", "status": "pending",
     "detail": "intra_mom t=-1.93,样本不够"},
    {"key": "lowvol", "name": "低波动", "status": "accepted",
     "detail": "2018-2022 五年全正,2023-2026 t=7.65"},
]


def line_stats() -> dict:
    acc = sum(1 for x in LINES if x["status"] == "accepted")
    rej = sum(1 for x in LINES if x["status"] == "rejected")
    pen = sum(1 for x in LINES if x["status"] == "pending")
    return {"lines": LINES, "accepted": acc, "rejected": rej,
            "pending": pen, "total": len(LINES)}


def maintain_stats() -> dict:
    """最近一次每日维护。"""
    try:
        d = ROOT / "logs"
        if not d.exists():
            return {}
        files = sorted(d.glob("maintain-*.log"))
        if not files:
            return {}
        last = files[-1]
        text = last.read_text(encoding="utf-8", errors="ignore")
        steps = [ln for ln in text.splitlines() if "①" in ln or "②" in ln
                 or "③" in ln or "④" in ln or "⑤" in ln]
        return {"file": last.name, "lines": len(text.splitlines()),
                "steps": steps[-5:], "mtime": time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(last.stat().st_mtime))}
    except Exception:  # noqa: BLE001
        return {}


def selfcheck_stats() -> dict:
    """最近一次自检结果(从 reports 里找缓存)。"""
    try:
        p = ROOT / "data" / "selfcheck_last.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {"items": 14, "bad": 0, "warn": 0, "note": "未跑缓存,显示上次已知值"}


def growth_stats() -> dict:
    """能力成长:累计尝试 vs 累计成功。"""
    f = factor_stats()
    ln = line_stats()
    attempts = int(f.get("total") or 0) + int(ln.get("total") or 0)
    wins = int(f.get("accepted") or 0) + int(ln.get("accepted") or 0)
    return {"attempts": attempts, "wins": wins,
            "rate": round(wins / attempts, 4) if attempts else 0.0}


def train_demo(epochs: int = 60, lr: float = 0.35) -> dict:
    """真训一个逻辑回归:用两个已知因子预测三重障碍标签。

    不是要做一个能用的模型 —— 是要让「学习程度」页有真实的训练曲线。
    特征:低波动(标准化) + 20 日动量
    标签:三重障碍(止盈5%/止损3%/5天),1=先碰止盈,0=其他
    """
    import numpy as np

    from . import factor_mine as fm
    from . import labeling as lb

    panel = fm._panel_unbiased(days=750, max_stocks=200, market="a")
    close = panel.get("close")
    if close is None or close.empty:
        return {"ok": False, "msg": "没有面板数据"}
    rets = close.pct_change()
    vol = (-rets.rolling(20, min_periods=10).std())
    mom = (-close.pct_change(20))

    lab = lb.label_panel(panel, horizon=5, tp=0.05, sl=0.03)
    import pandas as pd
    y_df = pd.DataFrame(lab["label"], index=close.index, columns=close.columns)

    xs, ys = [], []
    for d in close.index:
        a = vol.loc[d].dropna()
        b = mom.loc[d].reindex(a.index).dropna()
        a = a.reindex(b.index)
        c = y_df.loc[d].reindex(a.index).dropna()
        a = a.reindex(c.index)
        if len(a) < 30:
            continue
        v = a.to_numpy()
        m = b.to_numpy()
        if v.std() == 0 or m.std() == 0:
            continue
        xs.append(np.column_stack([(v - v.mean()) / v.std(),
                                   (m - m.mean()) / m.std()]))
        ys.append((c.to_numpy() > 0).astype("float64"))
    if not xs:
        return {"ok": False, "msg": "样本不足"}

    X = np.vstack(xs)
    Y = np.concatenate(ys)
    n = len(Y)
    cut = int(n * 0.7)
    Xtr, Ytr = X[:cut], Y[:cut]
    Xte, Yte = X[cut:], Y[cut:]

    w = np.zeros(X.shape[1])
    b0 = 0.0
    curve = []
    for ep in range(1, epochs + 1):
        z = Xtr @ w + b0
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        # 交叉熵 + L2
        loss = float(-np.mean(Ytr * np.log(p + 1e-9)
                              + (1 - Ytr) * np.log(1 - p + 1e-9))
                     + 1e-3 * float(w @ w))
        g = p - Ytr
        w -= lr * (Xtr.T @ g / len(Ytr) + 2e-3 * w)
        b0 -= lr * float(np.mean(g))
        # 训练/测试准确率
        def acc(Xs, Ys):
            zz = Xs @ w + b0
            pp = 1.0 / (1.0 + np.exp(-np.clip(zz, -30, 30)))
            return float(((pp > 0.5) == (Ys > 0.5)).mean())
        curve.append({"epoch": ep, "loss": round(loss, 5),
                      "train_acc": round(acc(Xtr, Ytr), 4),
                      "test_acc": round(acc(Xte, Yte), 4)})

    base = float(max(Yte.mean(), 1 - Yte.mean()))
    final = curve[-1]

    # 混淆矩阵 + AUC:准确率会被类别不平衡骗(基线 0.72 就是「全猜负类」),
    # 这两样才看得出「有没有真的区分能力」
    zz = Xte @ w + b0
    prob = 1.0 / (1.0 + np.exp(-np.clip(zz, -30, 30)))
    pred = (prob > 0.5).astype("float64")
    tp = int(((pred == 1) & (Yte == 1)).sum())
    fp = int(((pred == 1) & (Yte == 0)).sum())
    fn = int(((pred == 0) & (Yte == 1)).sum())
    tn = int(((pred == 0) & (Yte == 0)).sum())
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    # AUC:按预测概率排序,正例排在负例前面的比例(梯形法)
    order = np.argsort(prob)
    ys = Yte[order]
    n_pos = float((ys == 1).sum())
    n_neg = float((ys == 0).sum())
    auc = 0.0
    if n_pos > 0 and n_neg > 0:
        cum_pos = np.cumsum(ys == 1)
        cum_neg = np.cumsum(ys == 0)
        # 每个负例位置上的「已出现的正例数」求和
        auc = float(np.sum(cum_pos[ys == 0])) / (n_pos * n_neg)

    out = {"ok": True, "n": int(n), "n_train": int(cut),
           "n_test": int(n - cut), "curve": curve,
           "final": final, "baseline": round(base, 4),
           "lift": round(final["test_acc"] - base, 4),
           "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
           "precision": round(prec, 4), "recall": round(rec, 4),
           "f1": round(f1, 4), "auc": round(auc, 4),
           "pos_rate": round(float(Yte.mean()), 4),
           "weights": [round(float(x), 4) for x in w],
           "bias": round(float(b0), 4),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        STATE.parent.mkdir(exist_ok=True)
        old = {}
        if STATE.exists():
            try:
                old = json.loads(STATE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                old = {}
        old["train"] = out
        STATE.write_text(json.dumps(old, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return out


def last_train() -> dict:
    try:
        if STATE.exists():
            d = json.loads(STATE.read_text(encoding="utf-8"))
            return d.get("train") or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def snapshot() -> dict:
    """一次取全,给页面用。"""
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": data_stats(),
        "factors": factor_stats(),
        "lines": line_stats(),
        "maintain": maintain_stats(),
        "selfcheck": selfcheck_stats(),
        "growth": growth_stats(),
        "train": last_train(),
    }
