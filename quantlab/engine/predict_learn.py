# -*- coding: utf-8 -*-
"""预测层学习:每天记预测,到期判对错,错在哪归因到因子。**不对外**。

和 selflearn.py 的分工:
  selflearn  —— 管**执行**:分批、参数、决策记忆
  predict_learn —— 管**判断**:今天预测了什么、事后对不对、哪个因子误导了

为什么不直接改 forecast.py 的权重:那是 L0 引擎,回测/界面/自动交易全在用它,
改错一个系数所有地方一起错。这里只**记录和归因**,产出「因子可信度」建议,
要不要采纳由人决定 —— 机器自己改核心权重,出事说不清。

数据表:
  sl_predictions  每天每只票一条,含概率、置信、因子贡献、事后结果
"""
from __future__ import annotations

import json
import time

from ..data.store import store


def _fb():
    """数据回流模块。拿不到就返回 None —— 预测链不能因为上报模块坏了停摆。"""
    try:
        from .. import feedback
        return feedback
    except Exception:  # noqa: BLE001
        return None


def _fb_channel() -> str:
    try:
        from ..config import cfg
        return str((cfg().get("autotrade") or {}).get("gateway") or "sim")
    except Exception:  # noqa: BLE001
        return "sim"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sl_predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,
    account     TEXT NOT NULL DEFAULT 'default',
    code        TEXT NOT NULL,
    name        TEXT DEFAULT '',
    horizon     INTEGER NOT NULL DEFAULT 5,
    up          REAL,
    flat        REAL,
    down        REAL,
    verdict     TEXT DEFAULT '',
    confidence  TEXT DEFAULT '',
    skill       REAL,
    ece         REAL,
    threshold   REAL,
    factors     TEXT NOT NULL DEFAULT '[]',
    actual_ret  REAL,
    actual_dir  TEXT,
    correct     INTEGER,
    reviewed_at REAL,
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sl_pred ON sl_predictions(day, code);
CREATE INDEX IF NOT EXISTS ix_sl_pred_due ON sl_predictions(correct, day);
"""

_ready = False


def _db():
    global _ready
    st = store()
    if not _ready:
        with st.conn as c:
            c.executescript(_SCHEMA)
            # 老库没有 note(涨幅预选写备注用):补上,别让整张表报废。
            # 云端那份库是更早的脚本建的,不 ALTER 的话 record() 直接报
            # "unexpected keyword argument note" 下面再撞 "no column named note"。
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sl_predictions)")}
            for name, ddl in (("note", "TEXT DEFAULT ''"),
                              # strategy 以前只是传给云端事件的参数,没落库。
                              # 双策略之后必须存,否则分不清这条是稳健还是进攻选的。
                              ("strategy", "TEXT DEFAULT 'forecast'"),
                              ("day", "TEXT DEFAULT ''")):
                if name not in cols:
                    c.execute(f"ALTER TABLE sl_predictions ADD COLUMN {name} {ddl}")
        _ready = True
    return st


def _top_factors(rep: dict, k: int = 6) -> list[dict]:
    """只留贡献最大的几个因子。全存会把表撑爆,也没人看。"""
    fs = rep.get("factors") or []
    pick = sorted(fs, key=lambda x: -abs(float(x.get("contrib") or 0)))[:k]
    return [{"key": f.get("key"), "name": f.get("name"),
             "contrib": round(float(f.get("contrib") or 0), 4),
             "value": round(float(f.get("value") or 0), 3)}
            for f in pick]


def _proj_dir(up: float, flat: float, down: float) -> str:
    m = max((up, "up"), (flat, "flat"), (down, "down"))
    return m[1]


class PredictLog:
    def __init__(self, account: str = "default"):
        self.account = account or "default"

    def record(self, code: str, rep: dict, day: str | None = None,
               note: str = "", strategy: str = "forecast") -> int:
        """落一条预测。strategy 同时落库和进云端事件,默认 forecast 保持原口径。"""
        import datetime as dt
        day = day or dt.date.today().isoformat()
        probs = rep.get("probs_adj") or rep.get("probs") or {}
        bt = rep.get("backtest") or {}
        st = _db()
        with st.conn as c:
            cur = c.execute(
                "INSERT INTO sl_predictions(ts,day,account,code,name,horizon,"
                "up,flat,down,verdict,confidence,skill,ece,threshold,factors,note,"
                "strategy) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), day, self.account, code, rep.get("name") or "",
                 int(rep.get("horizon") or 5),
                 float(probs.get("up") or 0), float(probs.get("flat") or 0),
                 float(probs.get("down") or 0),
                 str(rep.get("verdict") or ""), str(rep.get("confidence") or ""),
                 bt.get("skill"), bt.get("calib_err"),
                 float(rep.get("threshold") or 0),
                 json.dumps(_top_factors(rep), ensure_ascii=False),
                 str(note or ""), str(strategy or "forecast")))
            pid = int(cur.lastrowid or 0)
        self._report_prediction(pid, code, rep, strategy)
        return pid

    def _report_prediction(self, pid: int, code: str, rep: dict,
                           strategy: str = "forecast") -> None:
        """预测归档上报:哪个因子在哪个方向上说了话,事后对不对。"""
        fb = _fb()
        if fb is None or pid <= 0:
            return
        try:
            probs = rep.get("probs_adj") or rep.get("probs") or {}
            bt = rep.get("backtest") or {}
            fb.push("prediction", "a",
                    {"symbol": code,
                     "name": str(rep.get("name") or "")[:20],
                     "verdict": str(rep.get("verdict") or "")[:12],
                     "confidence": str(rep.get("confidence") or "")[:8],
                     "prob_up": round(float(probs.get("up") or 0), 4),
                     "prob_flat": round(float(probs.get("flat") or 0), 4),
                     "prob_down": round(float(probs.get("down") or 0), 4),
                     "horizon_bars": int(rep.get("horizon") or 5),
                     "threshold": round(float(rep.get("threshold") or 0), 4),
                     "skill": bt.get("skill"),
                     "factors": [{"fid": f.get("key"), "name": f.get("name"),
                                  "contrib": round(float(f.get("contrib") or 0), 4)}
                                 for f in _top_factors(rep)]},
                    event_id=f"sl-prd-{pid}", strategy=strategy or "forecast",
                    channel=_fb_channel())
        except Exception:  # noqa: BLE001
            pass

    def due(self, horizon: int | None = None, limit: int = 200) -> list[dict]:
        """到期还没回填的。day + horizon 个交易日已过的那些。"""
        import datetime as dt
        st = _db()
        rows = st.conn.execute(
            "SELECT * FROM sl_predictions WHERE account=? AND correct IS NULL "
            "ORDER BY day LIMIT ?", (self.account, int(limit))).fetchall()
        out = []
        today = dt.date.today()
        for r in rows:
            d = dict(r)
            h = int(d.get("horizon") or horizon or 5)
            try:
                day = dt.date.fromisoformat(str(d["day"]))
            except ValueError:
                continue
            # 自然日粗判:horizon 交易日 ≈ horizon*1.5 自然日,够宽就好
            if (today - day).days < max(1, int(h * 1.4)):
                continue
            out.append(d)
        return out

    def review(self, pid: int, actual_ret: float, threshold: float) -> dict:
        """回填一条。方向按预测时用的阈值判,和时间点上的口径一致。"""
        if actual_ret > threshold:
            adir = "up"
        elif actual_ret < -threshold:
            adir = "down"
        else:
            adir = "flat"
        st = _db()
        row = st.conn.execute(
            "SELECT up,flat,down FROM sl_predictions WHERE id=?", (int(pid),)).fetchone()
        if not row:
            return {"ok": False, "why": "没这条"}
        pdir = _proj_dir(float(row["up"] or 0), float(row["flat"] or 0),
                         float(row["down"] or 0))
        correct = 1 if pdir == adir else 0
        with st.conn as c:
            c.execute("UPDATE sl_predictions SET actual_ret=?, actual_dir=?, "
                      "correct=?, reviewed_at=? WHERE id=?",
                      (float(actual_ret), adir, correct, time.time(), int(pid)))
        self._report_outcome(int(pid), actual_ret, adir, bool(correct))
        return {"ok": True, "pred": pdir, "actual": adir, "correct": bool(correct)}

    def _report_outcome(self, pid: int, ret: float, adir: str,
                        correct: bool) -> None:
        fb = _fb()
        if fb is None or pid <= 0:
            return
        try:
            fb.push("prediction_outcome", "a",
                    {"ret": round(float(ret), 6), "actual": adir,
                     "correct": bool(correct)},
                    event_id=f"sl-prdo-{pid}", ref=f"sl-prd-{pid}",
                    strategy="forecast", channel=_fb_channel())
        except Exception:  # noqa: BLE001
            pass

    def stats(self, window: int = 200) -> dict:
        st = _db()
        rows = st.conn.execute(
            "SELECT correct, actual_dir, verdict, horizon FROM sl_predictions "
            "WHERE account=? AND correct IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (self.account, int(window))).fetchall()
        n = len(rows)
        if not n:
            return {"n": 0, "acc": 0.0, "by_dir": {}, "by_verdict": {},
                    "by_horizon": {}}
        hit = sum(1 for r in rows if int(r["correct"]) == 1)
        by_dir: dict[str, list[int]] = {}
        by_verdict: dict[str, list[int]] = {}
        by_horizon: dict[str, list[int]] = {}
        for r in rows:
            by_dir.setdefault(str(r["actual_dir"] or ""), []).append(int(r["correct"]))
            by_verdict.setdefault(str(r["verdict"] or ""), []).append(int(r["correct"]))
            # 同一只票按 1/3/5 日分别记,分开看才知道该持几天
            by_horizon.setdefault(str(int(r["horizon"] or 5)),
                                  []).append(int(r["correct"]))
        return {
            "n": n,
            "acc": hit / n,
            "by_dir": {k: {"n": len(v), "acc": sum(v) / len(v)} for k, v in by_dir.items()},
            "by_verdict": {k: {"n": len(v), "acc": sum(v) / len(v)}
                           for k, v in by_verdict.items()},
            "by_horizon": {k: {"n": len(v), "acc": sum(v) / len(v)}
                           for k, v in sorted(by_horizon.items(),
                                              key=lambda kv: int(kv[0]))},
        }

    def factor_report(self, window: int = 300) -> list[dict]:
        """按因子统计:它出现在「判对」和「判错」的样本里各多少次、贡献多大。

        误导率 = 判错时该因子贡献方向的绝对值占比高 → 这个因子在骗人。
        """
        st = _db()
        rows = st.conn.execute(
            "SELECT factors, correct FROM sl_predictions "
            "WHERE account=? AND correct IS NOT NULL ORDER BY ts DESC LIMIT ?",
            (self.account, int(window))).fetchall()
        agg: dict[str, dict] = {}
        for r in rows:
            try:
                fs = json.loads(r["factors"] or "[]")
            except Exception:
                continue
            ok = int(r["correct"]) == 1
            for f in fs:
                key = str(f.get("key") or "")
                if not key:
                    continue
                a = agg.setdefault(key, {"key": key, "name": f.get("name") or key,
                                         "hit": 0, "miss": 0, "miss_contrib": 0.0})
                if ok:
                    a["hit"] += 1
                else:
                    a["miss"] += 1
                    a["miss_contrib"] += abs(float(f.get("contrib") or 0))
        out = []
        for a in agg.values():
            n = a["hit"] + a["miss"]
            if n < 5:          # 样本太少不给结论
                continue
            out.append({
                "key": a["key"], "name": a["name"], "n": n,
                "hit": a["hit"], "miss": a["miss"],
                "acc": a["hit"] / n,
                "miss_contrib": round(a["miss_contrib"], 3),
            })
        out.sort(key=lambda x: x["acc"])
        return out

    def recent(self, limit: int = 30) -> list[dict]:
        st = _db()
        rows = st.conn.execute(
            "SELECT * FROM sl_predictions WHERE account=? ORDER BY ts DESC LIMIT ?",
            (self.account, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def existing_today(self, day: str | None = None) -> set[str]:
        """今天已经预测过的代码。作业重跑时跳过,不白跑十分钟。"""
        import datetime as dt
        day = day or dt.date.today().isoformat()
        st = _db()
        rows = st.conn.execute(
            "SELECT DISTINCT code FROM sl_predictions WHERE account=? AND day=?",
            (self.account, day)).fetchall()
        return {str(r["code"]) for r in rows}


# ---------------------------------------------------------------- 批量作业
def daily_predict(codes: list[str], horizon: int = 5,
                  account: str = "default", on_one=None,
                  skip_existing: bool = True) -> dict:
    """对给定股票跑一遍 forecast 并记下来。慢(每只 ~1 分钟),收盘后跑。

    skip_existing:今天已经预测过的跳过。作业重跑(补跑、手动再跑一次)
    不该把同样的十分钟再花一遍。
    """
    from . import forecast as fc

    log = PredictLog(account)
    done = log.existing_today() if skip_existing else set()
    ok, bad, skipped = 0, [], 0
    for i, code in enumerate(codes, 1):
        if code in done:
            skipped += 1
            continue
        try:
            rep = fc.forecast(code, horizon=horizon)
            log.record(code, rep)
            ok += 1
            if on_one:
                on_one(i, len(codes), code, None)
        except Exception as e:  # noqa: BLE001
            bad.append({"code": code, "error": str(e)[:120]})
            if on_one:
                on_one(i, len(codes), code, e)
    return {"ok": ok, "failed": bad, "total": len(codes), "skipped": skipped}


def review_pending(account: str = "default", limit: int = 200) -> dict:
    """回填到期的预测。用 horizon 个交易日后的收盘价算实际收益。"""
    import pandas as pd

    from ..data import kline as kl
    from . import indicators as ind

    log = PredictLog(account)
    todo = log.due(limit=limit)
    done, bad = 0, []
    for d in todo:
        code = d["code"]
        try:
            bars, _src = kl.get_kline(code, 101, count=int(d["horizon"]) + 30)
            if not bars:
                bad.append({"code": code, "why": "没日线"})
                continue
            df = ind.bars_to_df(bars)
            df = df[df["datetime"] >= pd.Timestamp(str(d["day"]))]
            if len(df) <= int(d["horizon"]):
                bad.append({"code": code, "why": "后续K线不足"})
                continue
            base = float(df["close"].iloc[0])
            fut = float(df["close"].iloc[int(d["horizon"])])
            if base <= 0:
                bad.append({"code": code, "why": "基准价异常"})
                continue
            ret = fut / base - 1
            log.review(int(d["id"]), ret,
                       float(d.get("threshold") or 0.02))
            done += 1
        except Exception as e:  # noqa: BLE001
            bad.append({"code": code, "why": str(e)[:100]})
    return {"reviewed": done, "failed": bad, "due": len(todo)}


def summary(account: str = "default") -> dict:
    log = PredictLog(account)
    return {"stats": log.stats(), "factors": log.factor_report()[:5]}
