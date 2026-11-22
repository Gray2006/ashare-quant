# -*- coding: utf-8 -*-
"""游资手法蒸馏:龙虎榜席位行为 → 可回测的规则。

数据流:
  每日龙虎榜(board) + 席位(seats) → lhb_seats 表
  → 按席位统计「买入后 N 日收益」→ 高胜率席位
  → 跟买回测 → 手法报告
  → 对当日上榜股预测次日方向 → 回填验证 → 迭代席位权重

只做统计和回测,不下单。云端和本地都能跑。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from pathlib import Path

from ..config import cfg
from ..data import lhb
from ..data.kline import get_kline

_lock = threading.Lock()
_ready = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lhb_seats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    seat TEXT NOT NULL,
    buy REAL DEFAULT 0,
    sell REAL DEFAULT 0,
    net REAL DEFAULT 0,
    close REAL DEFAULT 0,
    ret5 REAL,
    ret10 REAL,
    filled_at REAL,
    UNIQUE(day, code, seat)
);
CREATE INDEX IF NOT EXISTS idx_lhb_seat ON lhb_seats(seat, day);
CREATE INDEX IF NOT EXISTS idx_lhb_day ON lhb_seats(day);
CREATE TABLE IF NOT EXISTS lhb_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    direction TEXT NOT NULL,
    score REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    actual_ret REAL,
    correct INTEGER,
    reviewed_at REAL,
    UNIQUE(day, code)
);
CREATE INDEX IF NOT EXISTS idx_lhb_pred_day ON lhb_predictions(day);
"""


def _db_path() -> Path:
    return cfg().path.parent / "hotmoney.db"


def _db() -> sqlite3.Connection:
    global _ready
    p = _db_path()
    if not _ready:
        with _lock:
            if not _ready:
                p.parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(str(p), timeout=10)
                c.executescript(_SCHEMA)
                c.close()
                _ready = True
    c = sqlite3.connect(str(p), timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 采集

def collect_day(day: str, max_stocks: int = 20) -> int:
    """拉某日龙虎榜 + 前 max_stocks 只票的席位,存库。返回新增条数。"""
    rows = lhb.board(day, limit=300)
    if not rows:
        return 0
    # 按净买入排序,只取前 N 只,控制请求量
    rows = sorted(rows, key=lambda r: abs(float(r.get("net") or 0)),
                  reverse=True)[:max_stocks]
    n = 0
    c = _db()
    try:
        for r in rows:
            code = r.get("code") or ""
            if not code:
                continue
            try:
                seats = lhb.seats(code, day)
            except Exception:
                continue
            for s in seats:
                try:
                    c.execute(
                        "INSERT OR REPLACE INTO lhb_seats"
                        "(day,code,name,seat,buy,sell,net,close) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (day, code, r.get("name", ""), s["name"],
                         float(s.get("buy") or 0), float(s.get("sell") or 0),
                         float(s.get("net") or 0), float(r.get("close") or 0)))
                    n += 1
                except Exception:
                    continue
        c.commit()
    finally:
        c.close()
    return n


def collect(days: int = 30, max_stocks: int = 20) -> dict:
    """拉最近 days 个工作日的龙虎榜。返回汇总。"""
    out = {"days": 0, "rows": 0, "empty": []}
    for d in lhb.recent_days(days):
        try:
            n = collect_day(d, max_stocks=max_stocks)
        except Exception as e:  # noqa: BLE001
            out["empty"].append(f"{d}: {str(e)[:60]}")
            continue
        out["days"] += 1
        out["rows"] += n
        if n == 0:
            out["empty"].append(d)
    return out


# ---------------------------------------------------------------- 回填

def _close_after(code: str, day: str, n: int) -> float | None:
    """取 day 之后第 n 个交易日的收盘价。"""
    try:
        bars, _src = get_kline(code, 101, count=120)
    except Exception:
        return None
    if not bars:
        return None
    ds = [str(b.get("datetime") or "")[:10] for b in bars]
    try:
        i = ds.index(day)
    except ValueError:
        return None
    j = i + n
    if j >= len(bars):
        return None
    return float(bars[j].get("close") or 0) or None


def fill_returns(limit: int = 300) -> int:
    """回填席位记录的 5/10 日收益。返回回填条数。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT id, code, day, close FROM lhb_seats "
            "WHERE ret5 IS NULL AND day <= date('now','-7 day') "
            "ORDER BY day LIMIT ?", (int(limit),)).fetchall()
        n = 0
        for r in rows:
            base = float(r["close"] or 0)
            if base <= 0:
                continue
            c5 = _close_after(r["code"], r["day"], 5)
            c10 = _close_after(r["code"], r["day"], 10)
            if c5 is None and c10 is None:
                continue
            ret5 = (c5 / base - 1) if c5 else None
            ret10 = (c10 / base - 1) if c10 else None
            c.execute("UPDATE lhb_seats SET ret5=?, ret10=?, filled_at=? "
                      "WHERE id=?", (ret5, ret10, time.time(), r["id"]))
            n += 1
        c.commit()
        return n
    finally:
        c.close()


# ---------------------------------------------------------------- 统计

def seat_stats(min_samples: int = 3, days: int = 60) -> list[dict]:
    """按席位统计胜率(买入后 5 日收益)。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT seat, COUNT(*) n, AVG(ret5) avg5, "
            "SUM(CASE WHEN ret5>0 THEN 1 ELSE 0 END) win5, "
            "SUM(net) net_sum "
            "FROM lhb_seats WHERE ret5 IS NOT NULL "
            "AND day >= date('now', ?) "
            "GROUP BY seat HAVING n >= ? ORDER BY avg5 DESC",
            (f"-{int(days)} day", int(min_samples))).fetchall()
        out = []
        for r in rows:
            n = int(r["n"] or 0)
            win = int(r["win5"] or 0)
            # 贝叶斯平滑:Beta(2,2) 先验,样本少时胜率向 0.5 收缩,
            # 避免「3 次全对 = 100%」这种噪声被当成手法。
            win_s = (win + 2.0) / (n + 4.0)
            out.append({
                "seat": r["seat"], "n": n,
                "avg5": round(float(r["avg5"] or 0), 4),
                "win5": round(win_s, 3),
                "win5_raw": round(win / n, 3) if n else 0.0,
                "net_sum": round(float(r["net_sum"] or 0), 0),
            })
        return out
    finally:
        c.close()


def backtest_follow(seat: str, days: int = 60) -> dict:
    """跟买回测:该席位买入后,次日买入,5 日后卖出。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT day, code, close, ret5 FROM lhb_seats "
            "WHERE seat=? AND ret5 IS NOT NULL AND day >= date('now', ?) "
            "ORDER BY day", (seat, f"-{int(days)} day")).fetchall()
    finally:
        c.close()
    if not rows:
        return {"seat": seat, "n": 0, "avg": 0.0, "win": 0.0}
    rets = [float(r["ret5"]) for r in rows]
    return {
        "seat": seat, "n": len(rets),
        "avg": round(sum(rets) / len(rets), 4),
        "win": round(sum(1 for x in rets if x > 0) / len(rets), 3),
    }


def walk_forward(days: int = 60, train_days: int = 40,
                 min_samples: int = 3) -> dict:
    """样本外验证:前 train_days 天统计席位,后 days-train_days 天验证。

    训练集里胜率高、验证集里也站得住,才算手法;只在训练集高、验证集
    掉下去的,是过拟合,不该进预测。
    """
    c = _db()
    try:
        train = c.execute(
            "SELECT seat, COUNT(*) n, AVG(ret5) avg5, "
            "SUM(CASE WHEN ret5>0 THEN 1 ELSE 0 END) win5 "
            "FROM lhb_seats WHERE ret5 IS NOT NULL "
            "AND day >= date('now', ?) AND day < date('now', ?) "
            "GROUP BY seat HAVING n >= ?",
            (f"-{int(days)} day", f"-{int(days - train_days)} day",
             int(min_samples))).fetchall()
        test = c.execute(
            "SELECT seat, COUNT(*) n, AVG(ret5) avg5, "
            "SUM(CASE WHEN ret5>0 THEN 1 ELSE 0 END) win5 "
            "FROM lhb_seats WHERE ret5 IS NOT NULL "
            "AND day >= date('now', ?) "
            "GROUP BY seat HAVING n >= ?",
            (f"-{int(days - train_days)} day", int(min_samples))).fetchall()
    finally:
        c.close()

    def _rows(rows):
        out = []
        for r in rows:
            n = int(r["n"] or 0)
            win = int(r["win5"] or 0)
            out.append({"seat": r["seat"], "n": n,
                        "avg5": round(float(r["avg5"] or 0), 4),
                        "win5": round((win + 2.0) / (n + 4.0), 3)})
        return out

    tr = _rows(train)
    te = {r["seat"]: r for r in _rows(test)}
    hot = [r for r in tr if r["win5"] >= 0.55]
    # 验证集里这些席位的表现
    held = [te[r["seat"]] for r in hot if r["seat"] in te]
    train_avg = (sum(r["avg5"] for r in hot) / len(hot)) if hot else 0.0
    test_avg = (sum(r["avg5"] for r in held) / len(held)) if held else 0.0
    return {
        "days": days, "train_days": train_days,
        "hot_seats": hot[:10],
        "held_in_test": held[:10],
        "train_avg": round(train_avg, 4),
        "test_avg": round(test_avg, 4),
        "overfit": bool(hot and held and test_avg < train_avg * 0.5),
    }


# ---------------------------------------------------------------- 预测

def predict_day(day: str, top: int = 10) -> list[dict]:
    """对当日上榜股预测次日方向:净买入 + 高胜率席位加持。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT code, name, SUM(net) net, COUNT(*) n_seats, "
            "AVG(close) close FROM lhb_seats WHERE day=? "
            "GROUP BY code ORDER BY net DESC LIMIT ?",
            (day, int(top))).fetchall()
        stats = {s["seat"]: s for s in seat_stats(min_samples=3, days=60)}
        out = []
        for r in rows:
            code = r["code"]
            seats = c.execute(
                "SELECT seat, net FROM lhb_seats WHERE day=? AND code=?",
                (day, code)).fetchall()
            score = 0.0
            hot = []
            for s in seats:
                st = stats.get(s["seat"])
                if st and st["win5"] >= 0.55 and st["n"] >= 3:
                    score += float(s["net"] or 0) / 1e8 * st["win5"]
                    hot.append(s["seat"])
            direction = "up" if score > 0 else ("down" if score < 0 else "flat")
            out.append({
                "day": day, "code": code, "name": r["name"],
                "direction": direction, "score": round(score, 3),
                "reason": "高胜率席位:" + ",".join(hot[:3]) if hot else "无",
            })
        return out
    finally:
        c.close()


def save_predictions(rows: list[dict]) -> int:
    """落预测。返回新增条数。"""
    if not rows:
        return 0
    c = _db()
    try:
        n = 0
        for r in rows:
            try:
                c.execute(
                    "INSERT OR REPLACE INTO lhb_predictions"
                    "(day,code,name,direction,score,reason) "
                    "VALUES(?,?,?,?,?,?)",
                    (r["day"], r["code"], r.get("name", ""),
                     r["direction"], float(r.get("score") or 0),
                     r.get("reason", "")))
                n += 1
            except Exception:
                continue
        c.commit()
        return n
    finally:
        c.close()


def review_predictions(limit: int = 100) -> dict:
    """回填到期预测(5 个交易日后)。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT id, day, code, direction FROM lhb_predictions "
            "WHERE correct IS NULL AND day <= date('now','-7 day') "
            "ORDER BY day LIMIT ?", (int(limit),)).fetchall()
        out = {"reviewed": 0, "due": len(rows)}
        for r in rows:
            base = _close_after(r["code"], r["day"], 0)
            c5 = _close_after(r["code"], r["day"], 5)
            if not base or not c5:
                continue
            ret = c5 / base - 1
            actual = "up" if ret > 0.02 else ("down" if ret < -0.02 else "flat")
            correct = 1 if actual == r["direction"] else 0
            c.execute("UPDATE lhb_predictions SET actual_ret=?, correct=?, "
                      "reviewed_at=? WHERE id=?",
                      (ret, correct, time.time(), r["id"]))
            out["reviewed"] += 1
        c.commit()
        return out
    finally:
        c.close()


def accuracy(days: int = 60) -> dict:
    """预测准确率。"""
    c = _db()
    try:
        rows = c.execute(
            "SELECT COUNT(*) n, SUM(correct) ok FROM lhb_predictions "
            "WHERE correct IS NOT NULL AND day >= date('now', ?)",
            (f"-{int(days)} day",)).fetchone()
        n = int(rows["n"] or 0)
        ok = int(rows["ok"] or 0)
        return {"n": n, "ok": ok, "acc": round(ok / n, 3) if n else 0.0}
    finally:
        c.close()


# ---------------------------------------------------------------- 报告

def report(days: int = 60) -> dict:
    """游资手法报告。"""
    stats = seat_stats(min_samples=3, days=days)
    top = [s for s in stats if s["win5"] >= 0.55][:10]
    acc = accuracy(days=days)
    # 跟买回测:对前 5 个高胜率席位各跑一遍
    bts = [backtest_follow(s["seat"], days=days) for s in top[:5]]
    return {
        "day": dt.date.today().isoformat(),
        "window_days": days,
        "seats_total": len(stats),
        "hot_seats": top,
        "backtests": bts,
        "prediction_accuracy": acc,
    }


def save_report(rep: dict, outdir: Path | None = None) -> tuple[str, str]:
    """落 md + json。"""
    outdir = Path(outdir) if outdir else cfg().path.parent / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    day = rep.get("day") or dt.date.today().isoformat()
    js = outdir / f"{day}-hotmoney.json"
    md = outdir / f"{day}-hotmoney.md"
    js.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# 游资手法报告 {day}", "",
             f"窗口 {rep.get('window_days')} 天 · 席位 {rep.get('seats_total')} 个", ""]
    acc = rep.get("prediction_accuracy") or {}
    lines.append(f"预测准确率:{acc.get('acc', 0):.1%}({acc.get('ok', 0)}/{acc.get('n', 0)})")
    lines.append("")
    lines.append("## 高胜率席位")
    lines.append("")
    lines.append("| 席位 | 样本 | 5日均收益 | 胜率 | 净买入(亿) |")
    lines.append("|---|---|---|---|---|")
    for s in rep.get("hot_seats") or []:
        lines.append(f"| {s['seat']} | {s['n']} | {s['avg5']:.2%} | "
                     f"{s['win5']:.0%} | {s['net_sum']/1e8:.2f} |")
    bts = rep.get("backtests") or []
    if bts:
        lines.append("")
        lines.append("## 跟买回测(次日买,5 日后卖)")
        lines.append("")
        lines.append("| 席位 | 样本 | 平均收益 | 胜率 |")
        lines.append("|---|---|---|---|")
        for b in bts:
            lines.append(f"| {b['seat']} | {b['n']} | {b['avg']:.2%} | {b['win']:.0%} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(md), str(js)
