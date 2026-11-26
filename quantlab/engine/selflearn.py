# -*- coding: utf-8 -*-
"""私有自适应学习引擎。**不对外**:不进 gray-service,不暴露 HTTP。

三件事,分开看:

1) **分批执行**(TranchePlanner)
   目标仓位拆成 N 笔,按信号强度决定首笔大小和总批数。
   信号强 → 首笔大、批数少(快);信号弱 → 首笔小、批数多(试)。
   卖出比买入果断:首笔就出 60%,剩下的看后续。
   状态存 sl_tranche 表,重启不丢,不会因为进程重启就重新全买一遍。

2) **决策记忆**(DecisionLog)
   每次下单记一条:当时的价格、信号强度、市场状态、参数快照。
   事后回填结果(这笔赚了还是亏了)。没有记忆的"学习"是假的。

3) **参数迭代**(ParamTuner)
   按历史表现调 per_position_pct / stop_loss / take_profit。
   有滞回(变化不够大就不动),每次改动写 sl_versions,能追溯是哪批数据改的。

为什么不用 ML:样本太少(一天几笔),任何模型都会过拟合。
这里用的是可解释的反馈环 —— 和 adaptive.py 的 AdaptiveSizer 同一路数,
但管的是**参数**和**节奏**,不只是仓位系数。
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

from ..data.store import store


# ---------------------------------------------------------------- 回流挂点
def _fb():
    """数据回流模块。拿不到就返回 None —— 学习链不能因为上报模块坏了停摆。"""
    try:
        from .. import feedback
        return feedback
    except Exception:
        return None


def _fb_channel() -> str:
    try:
        from ..config import cfg
        return str((cfg().get("autotrade") or {}).get("gateway") or "sim")
    except Exception:
        return "sim"


def _fb_num_params(params: dict | None) -> dict:
    out = {}
    for k, v in (params or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[str(k)[:32]] = round(float(v), 5)
    return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sl_decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    account   TEXT NOT NULL DEFAULT 'default',
    strategy  TEXT NOT NULL DEFAULT '',
    code      TEXT NOT NULL,
    name      TEXT DEFAULT '',
    action    TEXT NOT NULL,              -- buy / sell
    qty       INTEGER NOT NULL DEFAULT 0,
    price     REAL NOT NULL DEFAULT 0,
    tranche   INTEGER NOT NULL DEFAULT 1, -- 第几批
    tranches  INTEGER NOT NULL DEFAULT 1, -- 共几批
    strength  REAL NOT NULL DEFAULT 0,    -- 当时的信号强度 0~1
    market    TEXT DEFAULT '',            -- 市场状态
    params    TEXT DEFAULT '{}',          -- 参数快照 JSON
    outcome   REAL,                       -- 回填:这笔的收益(小数)
    outcome_at REAL,
    close_outcome REAL,                   -- 收盘口径收益(当日收盘价,和第 20 小时口径并存)
    close_at  REAL,                       -- 收盘口径回填时间
    note      TEXT DEFAULT '',
    tries     INTEGER NOT NULL DEFAULT 0, -- 回填尝试次数(取不到价就 +1)
    dropped   INTEGER NOT NULL DEFAULT 0  -- 试够天数仍取不到价,放弃回填
);
CREATE INDEX IF NOT EXISTS ix_sl_dec ON sl_decisions(account, strategy, ts);

CREATE TABLE IF NOT EXISTS sl_versions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    account  TEXT NOT NULL DEFAULT 'default',
    strategy TEXT NOT NULL DEFAULT '',
    params   TEXT NOT NULL DEFAULT '{}',
    reason   TEXT DEFAULT '',
    score    REAL,                        -- 该版本的评估分
    n        INTEGER NOT NULL DEFAULT 0   -- 依据多少条样本
);

CREATE TABLE IF NOT EXISTS sl_tranche (
    account        TEXT NOT NULL DEFAULT 'default',
    code           TEXT NOT NULL,
    side           TEXT NOT NULL DEFAULT 'buy',
    target_qty     INTEGER NOT NULL DEFAULT 0,
    done_qty       INTEGER NOT NULL DEFAULT 0,
    tranches       INTEGER NOT NULL DEFAULT 1,
    done_tranches  INTEGER NOT NULL DEFAULT 0,
    strength       REAL NOT NULL DEFAULT 0,
    created        REAL NOT NULL DEFAULT 0,
    updated        REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (account, code, side)
);
"""

_ready = False
_lock = threading.Lock()   # 建表/迁移只做一次;多线程(UI + pilot)同时首次调用会撞 ALTER

# 复盘统计缓存:key = "account:days",value = (取的时间, 统计)
_REVIEW_CACHE: dict[str, tuple[float, dict]] = {}
_REVIEW_TTL = 600.0


def _db():
    """建表一次。用 store 的每线程连接,不另开。"""
    global _ready
    st = store()
    if not _ready:
        with _lock:
            if not _ready:
                with st.conn as c:
                    c.executescript(_SCHEMA)
                    _migrate(c)
                _ready = True
    return st


def _migrate(c) -> None:
    """老库补列。CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(sl_decisions)")}
    for name, ddl in (("tries", "INTEGER NOT NULL DEFAULT 0"),
                      ("dropped", "INTEGER NOT NULL DEFAULT 0"),
                      ("close_outcome", "REAL"),      # 收盘口径结果(和 outcome 并存)
                      ("close_at", "REAL")):
        if name not in cols:
            try:
                c.execute(f"ALTER TABLE sl_decisions ADD COLUMN {name} {ddl}")
            except Exception:  # noqa: BLE001
                # 并发迁移时另一个线程可能已经加过,列存在就跳过
                pass


# ---------------------------------------------------------------- 决策记忆
class DecisionLog:
    DROP_AFTER_DAYS = 7.0   # 超过这么多天还取不到价,放弃回填

    def __init__(self, account: str = "default", strategy: str = ""):
        self.account = account or "default"
        self.strategy = strategy or ""

    def record(self, code: str, action: str, qty: int, price: float, *,
               name: str = "", tranche: int = 1, tranches: int = 1,
               strength: float = 0.0, market: str = "",
               params: dict | None = None, note: str = "") -> int:
        # 口径留痕:写库用的策略名和 config 不一致时,把配置名记进 note。
        # 报告按 config 过滤、交易按实例写库 —— 两者不一致时,以前只能靠猜
        # (2026-11-25 日报 0 条就是这个)。这里不改行为,只让下次能一眼看出来。
        try:
            from ..config import cfg as _c
            want = str((_c().get("autotrade") or {}).get("strategy") or "")
            if want and want != (self.strategy or ""):
                note = ((note + " · ") if note else "") + f"配置策略={want}"
        except Exception:  # noqa: BLE001
            pass
        st = _db()
        with st.conn as c:
            cur = c.execute(
                "INSERT INTO sl_decisions(ts,account,strategy,code,name,action,"
                "qty,price,tranche,tranches,strength,market,params,note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), self.account, self.strategy, code, name, action,
                 int(qty), float(price), int(tranche), int(tranches),
                 float(strength), market,
                 json.dumps(params or {}, ensure_ascii=False), note))
            did = int(cur.lastrowid or 0)
        # 脱敏回流:不发股数/金额,只发信号强度、分批节奏和参数快照。
        # 没有成交价的绝对量也足够把「什么参数在什么市场下赢了」对齐。
        fb = _fb()
        if fb is not None:
            try:
                fb.push("decision", "a",
                        {"symbol": code, "name": name, "side": action,
                         "tranche": int(tranche), "tranches": int(tranches),
                         "strength": round(float(strength), 4),
                         "market": str(market or "")[:40],
                         "params": _fb_num_params(params)},
                        event_id=f"sl-dec-{did}", strategy=self.strategy,
                        channel=_fb_channel())
            except Exception:
                pass
        return did

    def backfill(self, decision_id: int, outcome: float) -> None:
        """回填结果。outcome 是这笔的收益(小数,0.01 = 1%)。"""
        st = _db()
        with st.conn as c:
            c.execute("UPDATE sl_decisions SET outcome=?, outcome_at=? WHERE id=?",
                      (float(outcome), time.time(), int(decision_id)))
        self._report_outcome(int(decision_id), float(outcome))

    def backfill_many(self, items: list[tuple[int, float]]) -> int:
        """批量回填,一次事务。逐条写 200 次就是 200 次提交,慢且碎。"""
        if not items:
            return 0
        now = time.time()
        st = _db()
        with st.conn as c:
            c.executemany(
                "UPDATE sl_decisions SET outcome=?, outcome_at=? WHERE id=?",
                [(float(v), now, int(i)) for i, v in items])
        for i, v in items:
            self._report_outcome(int(i), float(v))
        return len(items)

    def _report_outcome(self, decision_id: int, outcome: float) -> None:
        fb = _fb()
        if fb is None:
            return
        try:
            fb.push("outcome", "a", {"ret": round(float(outcome), 6)},
                    event_id=f"sl-out-{decision_id}",
                    ref=f"sl-dec-{decision_id}",
                    strategy=self.strategy, channel=_fb_channel())
        except Exception:
            pass

    def pending(self, limit: int = 200, oldest_first: bool = False) -> list[dict]:
        """还没回填结果的决策。

        oldest_first=True 给回填用:积压超过 limit 条时,先回填最老的,
        否则最新的 200 条永远占着窗口,老决策永远轮不到。
        dropped=1 的(长期取不到价)不再返回,免得死条目把队列堵死。
        """
        st = _db()
        order = "ASC" if oldest_first else "DESC"
        rows = st.conn.execute(
            f"SELECT * FROM sl_decisions WHERE account=? AND strategy=? "
            f"AND outcome IS NULL AND dropped=0 ORDER BY ts {order} LIMIT ?",
            (self.account, self.strategy, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, ts: float) -> int:
        """ts 之后又记了多少条决策。参数迭代的防抖用它,不用窗口条数 ——
        窗口满了以后 n 不再增长,拿 n 相减会让调参永久卡死。"""
        st = _db()
        r = st.conn.execute(
            "SELECT COUNT(*) AS n FROM sl_decisions "
            "WHERE account=? AND strategy=? AND ts>?",
            (self.account, self.strategy, float(ts or 0))).fetchone()
        return int((r or {})["n"] or 0)

    def stats(self, window: int = 60, half_life: float = 20.0) -> dict:
        """最近 window 条已回填决策的表现。

        half_life: 时间衰减半衰期(条)。最近的样本权重 1,每过 half_life 条
        权重减半 —— 市场在变,三个月前的胜率不该和昨天一样重。

        额外给 win_lb(Wilson 下界)和 std:样本少时胜率向 0.5 收缩,
        避免「3 条全对」被当成高胜率;std 用来算夏普。
        """
        st = _db()
        rows = st.conn.execute(
            "SELECT action, outcome FROM sl_decisions "
            "WHERE account=? AND strategy=? AND outcome IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (self.account, self.strategy, int(window))).fetchall()
        vals = [float(r["outcome"]) for r in rows]
        if not vals:
            return {"n": 0, "mean": 0.0, "win": 0.0, "win_lb": 0.0,
                    "std": 0.0, "buys": 0, "sells": 0}
        n = len(vals)
        w = [0.5 ** (i / max(1.0, half_life)) for i in range(n)]
        wsum = sum(w)
        mean = sum(wi * v for wi, v in zip(w, vals)) / wsum
        win = sum(wi for wi, v in zip(w, vals) if v > 0) / wsum
        # Wilson 下界:样本少时向 0.5 收缩
        z = 1.96
        denom = 1 + z * z / n
        center = (win + z * z / (2 * n)) / denom
        margin = z * math.sqrt(win * (1 - win) / n + z * z / (4 * n * n)) / denom
        win_lb = max(0.0, center - margin)
        var = sum(wi * (v - mean) ** 2 for wi, v in zip(w, vals)) / wsum
        buys = sum(1 for r in rows if r["action"] == "buy")
        return {
            "n": n, "mean": mean, "win": win, "win_lb": win_lb,
            "std": math.sqrt(var), "buys": buys, "sells": n - buys,
        }

    def recent(self, limit: int = 50) -> list[dict]:
        st = _db()
        rows = st.conn.execute(
            "SELECT * FROM sl_decisions WHERE account=? ORDER BY ts DESC LIMIT ?",
            (self.account, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def backfill_by_quotes(self, quotes: dict, min_age_s: float = 3600 * 20,
                           limit: int = 1000, cost: float = 0.0) -> int:
        """用现价回填「已经过了一段时间」的决策,返回回填条数。

        口径:
          buy  → 现价 / 决策价 - 1(买了之后涨了算对)
          sell → 决策价 / 现价 - 1(卖了之后跌了算对)

        这是**按现价算**的短期口径,不是平仓归集。好处是能持续产生样本,
        坏处是没考虑持有成本和时间价值。对「调参数」够用,对「算业绩」不够 ——
        业绩看 review.py 的 FIFO 配对。

        min_age_s 默认 20 小时:当天刚下的单不回填,避免用几分钟后的价格
        评价一个本该看几天的决定。

        取不到价的条目:超过 DROP_AFTER_DAYS 天就标记 dropped 放弃,否则只记
        一次尝试。以前这些条目永远留在 pending 里,把最老 200 条的位置占满,
        新决策永远轮不到回填 —— 学习链就这么空转。
        """
        now = time.time()
        # 行情整体取不到(断网/接口挂)时不要动 tries/dropped:
        # 否则连续失败 7 天会把所有决策误判成「取不到价」全部丢掉。
        if not quotes:
            return 0
        batch: list[tuple[int, float]] = []
        miss: list[int] = []
        drop: list[int] = []
        for d in self.pending(limit=limit, oldest_first=True):
            age = now - float(d["ts"])
            if age < min_age_s:
                continue
            q = (quotes or {}).get(d["code"]) or {}
            px = float(q.get("price") or q.get("last") or 0)
            base = float(d["price"] or 0)
            if px <= 0 or base <= 0:
                if age > self.DROP_AFTER_DAYS * 86400:
                    drop.append(int(d["id"]))
                else:
                    miss.append(int(d["id"]))
                continue
            # 买入:涨了算对;卖出:跌了算对(躲开的跌幅就是收益)。
            # 两者对称 —— 用 1 - px/base,不要用 base/px - 1(后者会把 10% 算成 11%)
            out = (px / base - 1) if d["action"] == "buy" else (1 - px / base)
            # 扣双边成本(手续费+滑点)。默认 0 保持旧口径,ParamTuner 用 0.0015。
            out -= float(cost)
            # 极端值截断:数据异常时别把参数带偏
            out = max(-0.5, min(0.5, out))
            batch.append((int(d["id"]), out))
        if miss:
            self._bump_tries(miss)
        if drop:
            self._drop(drop)
        return self.backfill_many(batch)

    def _bump_tries(self, ids: list[int]) -> None:
        st = _db()
        with st.conn as c:
            c.executemany("UPDATE sl_decisions SET tries=tries+1 WHERE id=?",
                          [(int(i),) for i in ids])

    def _drop(self, ids: list[int]) -> None:
        st = _db()
        with st.conn as c:
            c.executemany("UPDATE sl_decisions SET dropped=1 WHERE id=?",
                          [(int(i),) for i in ids])

    def backfill_close(self, quotes: dict, day_start: float | None = None,
                       limit: int = 1000, cost: float = 0.0) -> int:
        """收盘口径回填:给「今天下的单」按收盘价打一次分,返回条数。

        和 backfill_by_quotes 的区别:
          - 不看 20 小时年龄门槛,当天收盘就算
          - 结果写 close_outcome,不写 outcome —— 20 小时口径才是调参数的口径,
            这里只是「今天这批决定当天走得怎么样」的快照,不参与 ParamTuner
          - 独立事件 id(前缀 sl-cout-),云端和 sl-out- 分开统计,不会覆盖

        day_start: 只回填 ts >= day_start 的决策。None 表示不限(补跑用)。
        """
        if not quotes:
            return 0
        st = _db()
        sql = ("SELECT * FROM sl_decisions WHERE account=? AND strategy=? "
               "AND close_outcome IS NULL AND dropped=0")
        args: list = [self.account, self.strategy]
        if day_start is not None:
            sql += " AND ts>=?"
            args.append(float(day_start))
        sql += " ORDER BY ts ASC LIMIT ?"
        args.append(int(limit))
        rows = st.conn.execute(sql, args).fetchall()

        batch: list[tuple[int, float]] = []
        for d in rows:
            q = (quotes or {}).get(d["code"]) or {}
            px = float(q.get("price") or q.get("last") or 0)
            base = float(d["price"] or 0)
            if px <= 0 or base <= 0:
                continue
            out = (px / base - 1) if d["action"] == "buy" else (1 - px / base)
            out -= float(cost)
            out = max(-0.5, min(0.5, out))
            batch.append((int(d["id"]), out))
        if not batch:
            return 0

        now = time.time()
        with st.conn as c:
            c.executemany(
                "UPDATE sl_decisions SET close_outcome=?, close_at=? WHERE id=?",
                [(float(v), now, int(i)) for i, v in batch])
        # 上报:只发收益和关联 id,不发金额/股数
        fb = _fb()
        if fb is not None:
            for did, v in batch:
                try:
                    fb.push("outcome", "a",
                            {"ret": round(float(v), 6), "basis": "close"},
                            event_id=f"sl-cout-{did}",
                            ref=f"sl-dec-{did}",
                            strategy=self.strategy, channel=_fb_channel())
                except Exception:  # noqa: BLE001
                    pass
        return len(batch)


# ---------------------------------------------------------------- 分批执行
def topup_ok(want_qty: int, held_qty: int, min_qty: int = 100,
             hysteresis_pct: float = 0.0) -> bool:
    """这笔补单值不值得做。

    目标仓位每轮按「权益 × 权重」重算,权益一涨目标就变大 → 2/2 完成后又
    从头分批,变成小单连买(2026-11-25 实测 002086 一天 6 笔)。
    hysteresis_pct > 0 时,差额不到目标的这个比例就不新开计划;默认 0 = 关,
    保持原行为,等主人决定要不要开。
    """
    want = int(want_qty or 0)
    held = int(held_qty or 0)
    delta = want - held
    if delta < max(1, int(min_qty or 1)):
        return False
    if hysteresis_pct and float(hysteresis_pct) > 0:
        return delta >= float(want) * float(hysteresis_pct)
    return True


class TranchePlanner:
    """把目标仓位拆成多笔。状态落库,重启不重来。"""

    # 信号强度 → (首笔比例, 总批数)
    # 强信号快进快出,弱信号慢慢试 —— 这是"不一下子全买"的核心
    LADDER = ((0.70, 0.50, 2), (0.40, 0.34, 3), (0.00, 0.20, 4))
    SELL_FIRST = 0.60      # 卖出首笔就出 60%,风险优先
    SELL_TRANCHES = 2
    MIN_AMOUNT = 5000.0    # 整笔金额低于这个就不拆:每笔都有最低佣金,拆了更贵

    def __init__(self, account: str = "default"):
        self.account = account or "default"

    def _get(self, code: str, side: str) -> dict | None:
        st = _db()
        r = st.conn.execute(
            "SELECT * FROM sl_tranche WHERE account=? AND code=? AND side=?",
            (self.account, code, side)).fetchone()
        return dict(r) if r else None

    def _put(self, code: str, side: str, **kw) -> None:
        st = _db()
        cur = self._get(code, side) or {}
        cur.update(kw)
        cur.setdefault("created", time.time())
        cur["updated"] = time.time()
        with st.conn as c:
            c.execute(
                "INSERT INTO sl_tranche(account,code,side,target_qty,done_qty,"
                "tranches,done_tranches,strength,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account,code,side) DO UPDATE SET "
                "target_qty=excluded.target_qty, done_qty=excluded.done_qty, "
                "tranches=excluded.tranches, done_tranches=excluded.done_tranches, "
                "strength=excluded.strength, updated=excluded.updated",
                (self.account, code, side, int(cur.get("target_qty") or 0),
                 int(cur.get("done_qty") or 0), int(cur.get("tranches") or 1),
                 int(cur.get("done_tranches") or 0), float(cur.get("strength") or 0),
                 float(cur.get("created") or time.time()),
                 float(cur.get("updated") or time.time())))

    @staticmethod
    def _same_day(ts: float, now: float | None = None) -> bool:
        import datetime as _dt
        if not ts:
            return False
        a = _dt.datetime.fromtimestamp(float(ts)).date()
        b = _dt.datetime.fromtimestamp(float(now or time.time())).date()
        return a == b

    @staticmethod
    def _replan_tolerance() -> float:
        try:
            from ..config import cfg
            return float((cfg().get("selflearn") or {}).get("replan_tolerance", 0.05))
        except Exception:  # noqa: BLE001
            return 0.05

    def _drop(self, code: str, side: str) -> None:
        st = _db()
        with st.conn as c:
            c.execute("DELETE FROM sl_tranche WHERE account=? AND code=? AND side=?",
                      (self.account, code, side))

    @classmethod
    def _ladder(cls, strength: float) -> tuple[float, int]:
        s = max(0.0, min(1.0, float(strength or 0)))
        for lo, first, n in cls.LADDER:
            if s >= lo:
                return first, n
        return 0.20, 4

    def plan_buy(self, code: str, want_qty: int, held_qty: int,
                 strength: float, min_qty: int = 100,
                 price: float = 0.0, min_amount: float | None = None) -> dict:
        """本轮该买多少。返回 {qty, tranche, tranches, done, target}。

        price 给了才做「最低佣金」判断:整笔金额太小就一次买完。
        拆成 4 笔,每笔都要交最低佣金(通常 5 元),小额账户上分批反而更贵。
        """
        want = int(want_qty or 0)
        held = int(held_qty or 0)
        if want <= held:
            # 保留「今天已建仓」标记:否则这一轮删掉、下一轮目标稍涨又重开计划,
            # 同一天内还是碎单连买。
            st0 = self._get(code, "buy")
            done0 = int((st0 or {}).get("done_tranches") or 0)
            need0 = int((st0 or {}).get("tranches") or 1)
            keep = (st0 is not None and done0 >= need0
                    and self._same_day(float(st0.get("updated") or 0)))
            if not keep:
                self._drop(code, "buy")
            return {"qty": 0, "tranche": 0, "tranches": 0,
                    "done": held, "target": want,
                    "why": "今日已建仓" if keep else "已达标"}

        room = want - held
        floor = self.MIN_AMOUNT if min_amount is None else float(min_amount)
        if price > 0 and floor > 0 and float(price) * want < floor:
            qty = int(room // min_qty * min_qty)
            self._drop(code, "buy")
            if qty < min_qty:
                return {"qty": 0, "tranche": 0, "tranches": 1,
                        "done": held, "target": want, "why": "剩余不足一手"}
            return {"qty": qty, "tranche": 1, "tranches": 1,
                    "done": held + qty, "target": want, "why": "金额小,不拆"}

        st = self._get(code, "buy")
        # 同一天里计划已经跑完,就别因为「目标随权益/价格漂移」再开一轮:
        # 否则每轮都重来一次 1/2、2/2,变成碎单连买(2026-11-25 实测)。
        if st and int(st.get("done_tranches") or 0) >= int(st.get("tranches") or 1):
            tol = float(self._replan_tolerance())
            old_target = int(st.get("target_qty") or 0)
            same_day = self._same_day(float(st.get("updated") or 0))
            moved = (old_target > 0
                     and abs(want - old_target) / float(old_target) > tol)
            if same_day and not moved:
                return {"qty": 0, "tranche": 0, "tranches": 0, "done": held,
                        "target": want, "why": "今日已建仓"}
            self._drop(code, "buy")
            st = None
        if not st:
            first, n = self._ladder(strength)
            self._put(code, "buy", target_qty=want, done_qty=held,
                      tranches=n, done_tranches=0, strength=strength)
            st = self._get(code, "buy") or {}
            per = max(min_qty, int(want * first // min_qty * min_qty))
        else:
            n = max(1, int(st.get("tranches") or 1))
            per = max(min_qty, int(want / n // min_qty * min_qty))

        done_t = int(st.get("done_tranches") or 0) + 1
        # 最后一批直接买满剩余。按 want/n 算会剩几十股,不足一手被丢掉,
        # 目标仓位永远差一点 —— 分批是为了节奏,不是为了买不满。
        qty = room if done_t >= n else min(per, room)
        qty = int(qty // min_qty * min_qty)
        if qty < min_qty:
            self._drop(code, "buy")
            return {"qty": 0, "tranche": 0, "tranches": n,
                    "done": held, "target": want, "why": "剩余不足一手"}

        self._put(code, "buy", target_qty=want, done_qty=held + qty,
                  tranches=n, done_tranches=done_t, strength=strength)
        # 跑完不删,留个「今天已经建好仓」的标记;明天或目标变动超过容差才重开
        # strength 要带出去:调用方记 DecisionLog 时用它。
        # 以前没带,日志里永远是兜底值 0.5(2026-11-25 修)。
        return {"qty": qty, "tranche": done_t, "tranches": n,
                "done": held + qty, "target": want, "why": "",
                "strength": float(strength or 0.0)}

    def plan_sell(self, code: str, avail_qty: int, strength: float = 0.0,
                  min_qty: int = 100, price: float = 0.0,
                  min_amount: float | None = None) -> dict:
        """本轮该卖多少。卖出比买入果断:首笔 60%,第二批清干净。"""
        avail = int(avail_qty or 0)
        if avail < min_qty:
            self._drop(code, "sell")
            return {"qty": 0, "tranche": 0, "tranches": 0, "why": "不足一手"}

        floor = self.MIN_AMOUNT if min_amount is None else float(min_amount)
        if price > 0 and floor > 0 and float(price) * avail < floor:
            self._drop(code, "sell")
            return {"qty": avail, "tranche": 1, "tranches": 1,
                    "why": "金额小,不拆"}

        # 卖出不分批 —— 一次卖干净。
        # 原来按 avail*0.6 递减、状态又被 _drop 清掉,导致每轮都从「第一次」
        # 开始,实测 002579 卖 10 笔才清完 3300 股(2026-11-26)。
        # 卖出是降风险动作,分批只会多付佣金,没有好处。
        qty = int(avail // min_qty * min_qty)
        if qty < min_qty:
            self._drop(code, "sell")
            return {"qty": 0, "tranche": 0, "tranches": 1,
                    "why": "剩余不足一手"}
        self._drop(code, "sell")
        return {"qty": qty, "tranche": 1, "tranches": 1, "why": ""}

    def clear(self, code: str | None = None) -> None:
        st = _db()
        with st.conn as c:
            if code:
                c.execute("DELETE FROM sl_tranche WHERE account=? AND code=?",
                          (self.account, code))
            else:
                c.execute("DELETE FROM sl_tranche WHERE account=?", (self.account,))

    def prune(self, max_age_days: float = 7.0) -> int:
        """清掉过期的分批状态。

        两种该清:已经买满/卖完的(正常路径会自己删,兜底),和超过
        max_age_days 没动过的(票早就不在目标里了,状态留着只会让下次
        买入从中间批次接着走)。不清的话表会一直涨。
        """
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400
        st = _db()
        with st.conn as c:
            cur = c.execute(
                "DELETE FROM sl_tranche WHERE account=? AND "
                "(created<? OR done_tranches>=tranches)",
                (self.account, cutoff))
            return int(cur.rowcount or 0)


# ---------------------------------------------------------------- 参数迭代
def _attr_days(sc: dict) -> int:
    """归因窗口天数:窗口越长,判死后冻结越久。"""
    try:
        return int((sc or {}).get("attribution_days") or 90)
    except (TypeError, ValueError):
        return 90


class ParamTuner:
    """按历史表现调参数。有滞回,可追溯。"""

    MIN_SAMPLES = 12       # 少于这么多条不给结论
    STEP = 0.15            # 参数变化至少 15% 才动
    MIN_GAP = 20           # 两次调整之间至少隔多少条决策
    COST = 0.0015          # 双边手续费+滑点,判断收益时扣掉

    def __init__(self, account: str = "default", strategy: str = ""):
        self.account = account or "default"
        self.strategy = strategy or ""
        self.log = DecisionLog(account, strategy)

    def _last_version(self) -> dict | None:
        st = _db()
        r = st.conn.execute(
            "SELECT * FROM sl_versions WHERE account=? AND strategy=? "
            "ORDER BY ts DESC LIMIT 1",
            (self.account, self.strategy)).fetchone()
        return dict(r) if r else None

    def _review_stats(self, days: int = 30) -> dict:
        """最近 N 天的复盘统计(已实现口径)。

        带 10 分钟缓存:run_once 每 10 秒跑一轮,复盘要扫 5000 条委托,
        不缓存的话光这一项就能把一轮拖慢。
        """
        key = f"{self.account}:{days}"
        now = time.time()
        hit = _REVIEW_CACHE.get(key)
        if hit and now - hit[0] < _REVIEW_TTL:
            return dict(hit[1])
        try:
            from . import review as rv
            rep = rv.build(account=self.account, days=days)
            if not rep.get("ok"):
                return {}
            s = rep.get("stat") or {}
            out = {"trades": int(s.get("trades") or 0),
                   "win_rate": s.get("win_rate"),
                   "profit_factor": s.get("profit_factor"),
                   "max_consec_loss": int(s.get("max_consec_loss") or 0),
                   "pnl": float(s.get("pnl") or 0)}
            _REVIEW_CACHE[key] = (now, out)
            return out
        except Exception:  # noqa: BLE001
            return {}

    def tune(self, base: dict) -> dict:
        """base 是当前参数,返回调整后的参数 + 说明。有变化才落库。"""
        r = self._decide(base)
        if r.get("changed"):
            st = _db()
            with st.conn as c:
                c.execute(
                    "INSERT INTO sl_versions(ts,account,strategy,params,reason,score,n) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (time.time(), self.account, self.strategy,
                     json.dumps(r["params"], ensure_ascii=False),
                     str(r.get("reason") or ""), round(float(r.get("score") or 0), 6),
                     int(r.get("n") or 0)))
            fb = _fb()
            if fb is not None:
                try:
                    fb.push("tune", "a",
                            {"params": _fb_num_params(r["params"]),
                             "reason": str(r.get("reason") or "")[:120],
                             "n": int(r.get("n") or 0),
                             "score": r.get("score")},
                            event_id=f"sl-tune-{self.account}-{int(time.time() // 60)}",
                            strategy=self.strategy, channel=_fb_channel())
                except Exception:
                    pass
        return r

    def preview(self, base: dict) -> dict:
        """只看建议,不落库。界面用 —— 打开页面不该改参数。"""
        return self._decide(base)

    def _prior_shrink(self, base: dict, n: int) -> dict | None:
        """本地样本不足时,用群体先验把参数往群体均值拉。

        配置 `selflearn.prior_shrink` 关掉就完全不碰(默认开)。
        拉完仍然夹逼在合理区间:群体数据再多也不该把仓位带出 2%~30%。
        """
        try:
            from .. import feedback
            from ..config import cfg
            if not bool((cfg().get("selflearn") or {}).get("prior_shrink", True)):
                return None
            got = feedback.shrink_params("a", self.strategy, base, n)
        except Exception:  # noqa: BLE001
            return None
        if not got:
            return None
        out = dict(base)
        out.update(got)
        if "per_position_pct" in got:
            out["per_position_pct"] = round(
                min(0.30, max(0.02, float(out["per_position_pct"]))), 4)
        if "stop_loss" in got:
            out["stop_loss"] = round(
                min(0.30, max(0.01, float(out["stop_loss"]))), 4)
        parts = [f"{k} {float(base.get(k) or 0):.4g}→{float(v):.4g}"
                 for k, v in got.items() if k in base]
        if not parts:
            return None
        return {"params": out,
                "reason": f"本地样本 {n} 条不足,按群体先验收缩:" + "、".join(parts)}

    def _attr_adjust(self, out: dict, step: float) -> tuple[bool, list[str]]:
        """策略归因:仓位乘数 + 真暂停。返回 (是否改动, 说明)。

        单独抽出来是为了让「紧急降权」能绕过调参间隔(MIN_GAP):
        策略已经被判失效时,不该因为「上一条参数刚调过」而排队等 20 条决策。
        """
        changed, why = False, []
        try:
            # 直接读 config.json,不用 cfg() 单例 ——
            # 单例在进程启动时加载一次,之后改文件不刷新,
            # 导致 auto_suspend 从 False 改成 True 后运行中的进程读不到。
            try:
                import json as _json
                from pathlib import Path as _P
                _cfg_path = _P(__file__).resolve().parents[2] / "config.json"
                sc = (_json.loads(_cfg_path.read_text(encoding="utf-8"))
                      .get("selflearn") or {})
            except Exception:  # noqa: BLE001
                from ..config import cfg as _cfg
                sc = _cfg().get("selflearn") or {}
            if not bool(sc.get("attribution", True)):
                return changed, why
            from . import strategy_review as sr
            mult, sry = sr.tuner_mult(self.account, self.strategy,
                                      days=_attr_days(sc))
            if mult != 1.0 and sry:
                p = float(out.get("per_position_pct") or 0.1)
                np_ = max(0.02, min(0.30, p * float(mult)))
                if abs(np_ - p) / max(p, 1e-9) >= step * 0.5:
                    out["per_position_pct"] = round(np_, 4)
                    changed = True
                    why.append(f"策略归因:{sry}(仓位 {p:.1%}→{np_:.1%})")
            # 真暂停:auto_suspend 打开且归因判 suspend 时,单票仓位归零。
            # 不开这个开关就只是建议 —— 停策略是不可逆的大动作,默认交给人。
            if bool(sc.get("auto_suspend", False)):
                state = sr.state_of(self.account, self.strategy)
                if state == "suspend":
                    out["per_position_pct"] = 0.0
                    out["strategy_halt"] = True
                    changed = True
                    why.append("策略归因判暂停(auto_suspend):单票仓位归零,"
                               "只平不新开")
        except Exception:  # noqa: BLE001
            pass
        return changed, why

    def _decide(self, base: dict) -> dict:
        """算一遍参数建议。base 键:per_position_pct / stop_loss / take_profit"""
        out = dict(base)
        s = self.log.stats(window=60)
        n = int(s.get("n") or 0)
        if n < self.MIN_SAMPLES:
            shr = self._prior_shrink(out, n)
            if shr:
                # 先验收缩之后仍要跑归因 —— 归因是风控信号,不该被样本量挡住。
                # 实盘本地样本常年 n=0,原来这里提前返回,导致 auto_suspend
                # 和仓位降权全部失效(2026-11-26 发现)。
                em = dict(shr["params"])
                ch_em, why_em = self._attr_adjust(em, self.STEP)
                if ch_em:
                    return {"params": em, "changed": True, "n": n,
                            "reason": shr["reason"] + " · 归因:"
                                      + ";".join(why_em), "score": 0.0}
                return {"params": shr["params"], "changed": True, "n": n,
                        "reason": shr["reason"], "score": 0.0}
            em = dict(out)
            ch_em, why_em = self._attr_adjust(em, self.STEP)
            if ch_em:
                return {"params": em, "changed": True, "n": n,
                        "reason": "归因:" + ";".join(why_em), "score": 0.0}
            return {"params": out, "changed": False, "n": n,
                    "reason": f"样本 {n} 条,不足 {self.MIN_SAMPLES} 条,参数不动"}

        last = self._last_version()
        if last:
            # 用「上次调整之后又记了多少条」,不用窗口条数相减:
            # 窗口封顶 60 条后 n 不再增长,相减会永远小于 MIN_GAP,调参永久卡死。
            since = self.log.count_since(float(last.get("ts") or 0))
            if since < self.MIN_GAP:
                # 归因判降权/暂停不该排队:风控信号优先于「调参间隔」。
                # 只放行「往下降」,升权仍然要等间隔(上和下不对称)。
                em = dict(out)
                ch_em, why_em = self._attr_adjust(em, self.STEP)
                if ch_em and (float(em.get("per_position_pct") or 0)
                              < float(out.get("per_position_pct") or 0)):
                    return {"params": em, "changed": True, "n": n,
                            "reason": "紧急降权(不受调参间隔限制):"
                                      + ";".join(why_em)}
                return {"params": out, "changed": False, "n": n,
                        "reason": f"距上次调整只过了 {since} 条,先不动"}

        mean, win = float(s["mean"]), float(s["win"])
        mean_net = mean - self.COST
        win_lb = float(s.get("win_lb") or 0.0)
        # 样本少时提高调整门槛:12 条时 STEP 约 0.27,60 条才用满幅 0.15。
        # 样本越少,越不该被几条噪声带着大幅调参。
        step = min(0.30, self.STEP * max(1.0, 20.0 / max(n, 1)) ** 0.5)
        changed, why = False, []

        # 胜率低 + 净均值为负 → 降仓位、收紧止损。
        # win_lb 是 Wilson 下界:样本少时它比 win 低得多,单独触发降仓,
        # 防止「3 条全对」被当成高胜率、也防止小样本噪声把仓位带偏。
        if (win < 0.40 and mean_net < 0) or win_lb < 0.30:
            p = float(out.get("per_position_pct") or 0.1)
            np_ = max(0.02, p * 0.7)
            if abs(np_ - p) / max(p, 1e-9) >= step:
                out["per_position_pct"] = round(np_, 4)
                changed = True
                why.append(f"胜率 {win:.0%}、均值 {mean:+.2%}:单票仓位 {p:.1%}→{np_:.1%}")
            # 止损下一轮再调:一次只动一个参数,归因才清楚。
            # 同时调仓位和止损,亏了不知道是哪个起的作用。
        # 胜率高 + 均值为正 → 加仓位、放宽止盈。
        # win_lb > 0.40 是硬门槛:小样本(比如 3 条全对)下 Wilson 下界只有
        # 0.44 左右,刚过线;2 条全对只有 0.34,不够加仓。
        elif win > 0.55 and mean_net > 0.005 and win_lb > 0.40:
            p = float(out.get("per_position_pct") or 0.1)
            np_ = min(0.30, p * 1.2)
            if abs(np_ - p) / max(p, 1e-9) >= step:
                out["per_position_pct"] = round(np_, 4)
                changed = True
                why.append(f"胜率 {win:.0%}、均值 {mean:+.2%}:单票仓位 {p:.1%}→{np_:.1%}")
            # 止盈下一轮再调,理由同上:一次只动一个参数。

        # 预测准确率校准:低于随机说明「判断」本身不可信,仓位再打折。
        # 这是把 predict_learn 的结果接进执行层 —— 两层学习在一处汇合。
        try:
            from . import predict_learn as pl
            ps = pl.PredictLog(self.account).stats(window=200)
            acc, pn = float(ps.get("acc") or 0), int(ps.get("n") or 0)
            if pn >= 30 and acc < 0.45:
                p = float(out.get("per_position_pct") or 0.1)
                np_ = max(0.02, p * 0.8)
                if abs(np_ - p) / max(p, 1e-9) >= step:
                    out["per_position_pct"] = round(np_, 4)
                    changed = True
                    why.append(f"预测准确率 {acc:.0%}({pn} 条)低于随机:"
                               f"仓位 {p:.1%}→{np_:.1%}")
        except Exception:  # noqa: BLE001
            pass

        # 策略归因:参数调得再细,策略本身失效也白搭。
        # 这里只回答「这条策略扣费后还赚钱吗」,乘数和上面的胜率口径不重复扣。
        ch_attr, why_attr = self._attr_adjust(out, step)
        changed = changed or ch_attr
        why.extend(why_attr)

        # 复盘校准:连续亏损和盈亏比是硬信号,比单笔回填更可信
        rvs = self._review_stats()
        if rvs.get("trades", 0) >= 10:
            p = float(out.get("per_position_pct") or 0.1)
            if rvs.get("max_consec_loss", 0) >= 5:
                np_ = max(0.02, p * 0.7)
                if abs(np_ - p) / max(p, 1e-9) >= step:
                    out["per_position_pct"] = round(np_, 4)
                    changed = True
                    why.append(f"复盘最大连亏 {rvs['max_consec_loss']} 笔:"
                               f"仓位 {p:.1%}→{np_:.1%}")
            elif ((rvs.get("profit_factor") or 0) >= 1.5
                  and (rvs.get("win_rate") or 0) >= 55):
                np_ = min(0.30, p * 1.15)
                if abs(np_ - p) / max(p, 1e-9) >= step:
                    out["per_position_pct"] = round(np_, 4)
                    changed = True
                    why.append(f"复盘盈亏比 {rvs['profit_factor']:.2f}、"
                               f"胜率 {rvs['win_rate']:.0f}%:仓位 {p:.1%}→{np_:.1%}")

        # 最大回撤约束:从每日权益快照算,回撤超 15% 强制降仓。
        # 胜率可能还好看,但权益已经掉下来了 —— 回撤是比胜率更硬的信号。
        try:
            from ..data.store import store as _store
            st = _store()
            rows = st.conn.execute(
                "SELECT equity FROM sl_equity WHERE account=? "
                "ORDER BY day DESC LIMIT 60", (self.account,)).fetchall()
            eqs = [float(r["equity"]) for r in rows if r["equity"]]
            if len(eqs) >= 5:
                peak, mdd = 0.0, 0.0
                for v in reversed(eqs):          # 从旧到新
                    peak = max(peak, v)
                    if peak > 0:
                        mdd = max(mdd, (peak - v) / peak)
                if mdd >= 0.15:
                    p = float(out.get("per_position_pct") or 0.1)
                    np_ = max(0.02, p * 0.6)
                    if abs(np_ - p) / max(p, 1e-9) >= step:
                        out["per_position_pct"] = round(np_, 4)
                        changed = True
                        why.append(f"最大回撤 {mdd:.1%}:仓位 {p:.1%}→{np_:.1%}")
        except Exception:  # noqa: BLE001
            pass

        if not changed:
            return {"params": out, "changed": False, "n": n,
                    "reason": f"胜率 {win:.0%}、均值 {mean:+.2%},没到调整阈值"}

        return {"params": out, "changed": True, "n": n,
                "reason": ";".join(why), "score": mean}

    def versions(self, limit: int = 20) -> list[dict]:
        st = _db()
        rows = st.conn.execute(
            "SELECT * FROM sl_versions WHERE account=? AND strategy=? "
            "ORDER BY ts DESC LIMIT ?",
            (self.account, self.strategy, int(limit))).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 日报
def daily_report(account: str = "default", strategy: str = "",
                 day: str | None = None) -> dict:
    """某一天的学习成果。数据全从表里现算,不另存一份 —— 免得两份对不上。"""
    import datetime as dt

    day = day or dt.date.today().isoformat()
    d0 = dt.datetime.strptime(day, "%Y-%m-%d")
    start = d0.timestamp()
    end = (d0 + dt.timedelta(days=1)).timestamp()
    acc, strat = account or "default", strategy or ""
    st = _db()

    # 配置里的 (账户, 策略) 和实际写库的可能是两套:AutoTrader 用的是**启动时**
    # 的 strategy_name,learn_scheduler 用的是**当前** config。配置改过之后
    # 日报就会一片空白,而库里其实有数据。对不上就按库里最近真在写的那条出报告,
    # 并把「为什么换了」写进 key_note —— 不静默改口径。
    key_note = ""
    sr = None
    try:
        from . import strategy_review as sr  # noqa: F811
        key = sr.resolve_key(acc, strat)
        if key.get("source") == "db":
            strat = key["strategy"]
            key_note = str(key.get("note") or "")
    except Exception:  # noqa: BLE001
        sr = None

    def rows(sql: str, args: tuple) -> list[dict]:
        return [dict(r) for r in st.conn.execute(sql, args).fetchall()]

    today = rows(
        "SELECT code,name,action,qty,price,tranche,tranches,strength,outcome "
        "FROM sl_decisions WHERE account=? AND strategy=? AND ts>=? AND ts<? "
        "ORDER BY ts", (acc, strat, start, end))
    filled_today = rows(
        "SELECT COUNT(*) AS n FROM sl_decisions WHERE account=? AND strategy=? "
        "AND outcome_at>=? AND outcome_at<?", (acc, strat, start, end))
    # 收盘口径:当天按收盘价打过分的有多少、平均走得怎么样
    close_today = rows(
        "SELECT COUNT(*) AS n, AVG(close_outcome) AS avg_out, "
        "SUM(CASE WHEN close_outcome>0 THEN 1 ELSE 0 END) AS hits "
        "FROM sl_decisions WHERE account=? AND strategy=? "
        "AND close_at>=? AND close_at<?", (acc, strat, start, end))
    versions_today = rows(
        "SELECT params,reason,score,n FROM sl_versions WHERE account=? AND strategy=? "
        "AND ts>=? AND ts<? ORDER BY ts", (acc, strat, start, end))
    pending_tranches = rows(
        "SELECT code,side,target_qty,done_qty,tranches,done_tranches "
        "FROM sl_tranche WHERE account=?", (acc,))
    total = rows(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS filled "
        "FROM sl_decisions WHERE account=? AND strategy=?", (acc, strat))
    all_versions = rows(
        "SELECT COUNT(*) AS n FROM sl_versions WHERE account=? AND strategy=?",
        (acc, strat))

    buys = [d for d in today if d["action"] == "buy"]
    sells = [d for d in today if d["action"] == "sell"]
    stats = DecisionLog(acc, strat).stats(window=60)

    # 预测层:今天预测了几只、累计准不准、哪个因子在骗人
    predict: dict = {}
    try:
        from . import predict_learn as pl
        pl._db()
        plog = pl.PredictLog(acc)
        pt = rows("SELECT COUNT(*) AS n FROM sl_predictions WHERE account=? AND day=?",
                  (acc, day))
        predict = {
            "today": int((pt[0] or {}).get("n") or 0),
            "stats": plog.stats(window=200),
            "factors": plog.factor_report(window=300)[:5],
        }
    except Exception:  # noqa: BLE001
        pass

    # 当日盈亏:权益快照差值 + 拆解 + 操作原因
    pnl: dict = {}
    try:
        from . import daily_review as dr
        pnl = dr.report(acc, day)
    except Exception:  # noqa: BLE001
        pass

    # 策略归因:跨策略比谁在赚钱,顺便给降权建议
    attribution: dict = {}
    if sr is not None:
        try:
            attribution = sr.build(account=acc, days=60)
        except Exception:  # noqa: BLE001
            attribution = {}
    # 今天一条都没有时,说清楚库里到底写了什么,别让人对着 0 猜
    activity: dict = {}
    if not today and sr is not None:
        try:
            activity = sr.activity(acc, day)
        except Exception:  # noqa: BLE001
            activity = {}

    # 观察池 + 搜索空间:自动化这一层的现状
    promote_sum: dict = {}
    try:
        from . import promote as pm
        promote_sum = pm.summary()
    except Exception:  # noqa: BLE001
        promote_sum = {}
    spaces: dict = {}
    try:
        from . import factor_space as fsp
        spaces = fsp.report()
    except Exception:  # noqa: BLE001
        spaces = {}

    return {
        "day": day,
        "account": acc,
        "strategy": strat,
        "key_note": key_note,
        "today": {
            "decisions": len(today),
            "buys": len(buys),
            "sells": len(sells),
            "filled": int((filled_today[0] or {}).get("n") or 0),
            "close_n": int((close_today[0] or {}).get("n") or 0),
            "close_avg": round(float((close_today[0] or {}).get("avg_out") or 0.0), 4),
            "close_hits": int((close_today[0] or {}).get("hits") or 0),
            "items": today,
        },
        "tranches": pending_tranches,
        "versions_today": versions_today,
        "stats": stats,
        "predict": predict,
        "pnl": pnl,
        "attribution": attribution,
        "activity": activity,
        "promote": promote_sum,
        "spaces": spaces,
        "total": {
            "decisions": int((total[0] or {}).get("total") or 0),
            "filled": int((total[0] or {}).get("filled") or 0),
            "versions": int((all_versions[0] or {}).get("n") or 0),
        },
    }


def render_markdown(rep: dict) -> str:
    """把日报渲染成人能读的 markdown。"""
    t = rep.get("today") or {}
    s = rep.get("stats") or {}
    tot = rep.get("total") or {}
    lines = [
        f"# {rep.get('day')} 自主学习日报",
        "",
        f"账户 `{rep.get('account')}` · 策略 `{rep.get('strategy') or '默认'}`",
        "",
    ]
    if rep.get("key_note"):
        lines += [f"> 口径说明:{rep['key_note']}", ""]
    lines += [
        "## 今日",
        "",
        f"- 决策 **{t.get('decisions', 0)}** 条（买 {t.get('buys', 0)} / 卖 {t.get('sells', 0)}）",
        f"- 回填旧决策 **{t.get('filled', 0)}** 条",
    ]
    # 收盘打分单独一行:这是当天就能看到结果的口径,和上面那行不是一回事
    if t.get("close_n"):
        lines.append(
            f"- 收盘打分 **{t['close_n']}** 条（对 {t.get('close_hits', 0)} / "
            f"错 {t['close_n'] - t.get('close_hits', 0)} · "
            f"平均 {t.get('close_avg', 0) * 100:+.2f}%）")
    # 当日盈亏放最前面:先看赚没赚,再看学了什么
    try:
        from . import daily_review as dr
        if rep.get("pnl"):
            lines = lines[:4] + [dr.render(rep["pnl"]), ""] + lines[4:]
    except Exception:  # noqa: BLE001
        pass
    if t.get("items"):
        lines.append("")
        lines.append("| 代码 | 名称 | 动作 | 数量 | 价格 | 批次 | 强度 |")
        lines.append("|---|---|---|---|---|---|---|")
        for d in t["items"]:
            lines.append(
                f"| {d.get('code')} | {d.get('name') or ''} | "
                f"{'买' if d.get('action') == 'buy' else '卖'} | {d.get('qty')} | "
                f"{float(d.get('price') or 0):.2f} | "
                f"{d.get('tranche')}/{d.get('tranches')} | "
                f"{float(d.get('strength') or 0):.2f} |")

    tr = rep.get("tranches") or []
    if tr:
        lines += ["", "## 分批进行中", ""]
        for x in tr:
            side = "买入" if x.get("side") == "buy" else "卖出"
            lines.append(
                f"- {x.get('code')} {side}:{x.get('done_qty')}/{x.get('target_qty')} 股,"
                f"已执行 {x.get('done_tranches')}/{x.get('tranches')} 批")

    lines += [
        "",
        "## 表现（最近 60 条已回填）",
        "",
        f"- 样本 **{s.get('n', 0)}** 条,均值 **{float(s.get('mean') or 0):+.2%}**,"
        f"胜率 **{float(s.get('win') or 0):.0%}**",
        f"- 买 {s.get('buys', 0)} 条 / 卖 {s.get('sells', 0)} 条",
    ]

    vt = rep.get("versions_today") or []
    lines += ["", "## 参数", ""]
    if vt:
        for v in vt:
            lines.append(f"- **今日调整**:{v.get('reason')}")
            lines.append(f"  - 依据 {v.get('n')} 条样本,评分 {float(v.get('score') or 0):+.2%}")
    else:
        lines.append("- 今日未调整（样本不足或未到滞回阈值）")

    pr = rep.get("predict") or {}
    if pr:
        ps = pr.get("stats") or {}
        lines += ["", "## 预测", ""]
        lines.append(f"- 今日预测 **{pr.get('today', 0)}** 只")
        if ps.get("n"):
            lines.append(f"- 累计准确率 **{float(ps.get('acc') or 0):.1%}**"
                         f"（{ps.get('n')} 条已回填）")
        else:
            lines.append("- 还没有回填样本,先攒几天")
        fs = pr.get("factors") or []
        if fs:
            lines += ["", "| 因子 | 准确率 | 样本 |", "|---|---|---|"]
            for f in fs:
                lines.append(f"| {f.get('name')} | "
                             f"{float(f.get('acc') or 0):.0%} | {f.get('n')} |")
            lines += ["", "> 准确率明显低于 50% 的因子,是在骗人。"]

    # 当日空转时,把库里实际写了什么摆出来 —— 0 条不该是个黑箱
    act = rep.get("activity") or {}
    if act.get("total"):
        lines += ["", "## 今天库里实际有什么", ""]
        for x in act.get("by_account") or []:
            lines.append(f"- 账户 `{x.get('account')}`:{x.get('n')} 条")
        for x in act.get("by_strategy") or []:
            lines.append(f"- 策略 `{x.get('strategy')}`:{x.get('n')} 条")
        lines += ["", "> 上面的「今日」按配置的账户+策略过滤,和这张表不一定是同一批。"]
    elif rep.get("today", {}).get("decisions") == 0:
        lines += ["", "> 今天这条 (账户, 策略) 一条决策都没有,库里也没有别的记录。"]

    at = rep.get("attribution") or {}
    if at.get("n"):
        lines += ["", "## 策略归因（最近 60 天,扣费后）", ""]
        if at.get("by_strategy"):
            lines += ["| 策略 | 样本 | 胜率 | 扣费均值 | t | 结论 |",
                      "|---|---|---|---|---|---|"]
            for s in at["by_strategy"]:
                nm = float(s.get("net_mean") or 0) * 100
                lines.append(
                    f"| {s.get('key')} | {s.get('n')} | "
                    f"{float(s.get('win') or 0):.0%} | {nm:+.2f}%/笔 | "
                    f"{s.get('t') if s.get('t') is not None else '-'} | "
                    f"{s.get('state')} |")
        dims = at.get("dims") or {}
        for dim, label in (("strength", "信号强度"), ("hold", "持有天数"),
                           ("action", "方向")):
            rows = dims.get(dim) or []
            if not rows:
                continue
            parts = [f"{r['key']} {float(r.get('net_mean') or 0) * 100:+.2f}%"
                     f"(n={r['n']})" for r in rows]
            lines.append(f"- 按{label}:{' · '.join(parts)}")
        fac = dims.get("factor") or []
        if fac:
            parts = [f"{f.get('name') or f.get('key')} {float(f.get('acc') or 0):.0%}"
                     f"(n={f.get('n')})" for f in fac[:6]]
            lines.append(f"- 按因子(预测层命中率):{' · '.join(parts)}")
        for a in at.get("advice") or []:
            lines.append(f"- **建议**:{a}")

    pm = rep.get("promote") or {}
    if pm.get("n"):
        lines += ["", "## 观察池", ""]
        by = pm.get("by_status") or {}
        lines.append("- 池内候选 **%d** 个:%s" % (
            pm.get("n", 0),
            " · ".join(f"{k} {v}" for k, v in by.items())))
        if pm.get("eligible"):
            lines.append("- 可提升(需人工确认):" + "、".join(pm["eligible"]))
        if pm.get("promoted"):
            lines.append("- 已提升:" + "、".join(pm["promoted"]))
    elif pm:
        lines += ["", "## 观察池", "", "- 池子是空的（还没有因子通过审计）"]

    sp = rep.get("spaces") or {}
    if sp.get("spaces"):
        lines += ["", "## 搜索空间", ""]
        lines += ["| 空间 | 已测 | 通过 | 判定 | 今日预算 |", "|---|---|---|---|---|"]
        for r in sp["spaces"]:
            lines.append(f"| {r.get('label')} | {r.get('tested')} | "
                         f"{r.get('promoted')} | {r.get('verdict')} | "
                         f"{r.get('budget')} |")
        if sp.get("verdict"):
            lines += ["", f"> {sp['verdict']}"]

    lines += [
        "",
        "## 累计",
        "",
        f"- 决策 **{tot.get('decisions', 0)}** 条,已回填 **{tot.get('filled', 0)}** 条",
        f"- 参数版本 **{tot.get('versions', 0)}** 个",
        "",
        "> 回填口径是按现价的短期对比,不是平仓归集。调参数够用,算业绩看复盘页。",
    ]
    return "\n".join(lines)


def save_report(rep: dict, outdir=None) -> tuple[str, str]:
    """写 markdown + json。返回 (md 路径, json 路径)。"""
    import datetime as _dt
    from ..config import USER_DIR

    d = Path(outdir) if outdir else (USER_DIR / "reports")
    d.mkdir(parents=True, exist_ok=True)
    day = rep.get("day") or _dt.date.today().isoformat()
    md = d / f"{day}-selflearn.md"
    js = d / f"{day}-selflearn.json"
    md.write_text(render_markdown(rep), encoding="utf-8")
    js.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(md), str(js)


# ---------------------------------------------------------------- 汇总
def summary(account: str = "default", strategy: str = "") -> dict:
    """给界面看的一屏:记忆多少条、表现如何、参数改过几次。"""
    log = DecisionLog(account, strategy)
    s = log.stats(window=60)
    tuner = ParamTuner(account, strategy)
    vs = tuner.versions(limit=1)
    st = _db()
    r = st.conn.execute(
        "SELECT COUNT(*) AS n FROM sl_versions WHERE account=? AND strategy=?",
        (account or "default", strategy or "")).fetchone()
    return {
        "decisions": s,
        "versions": int((r or {})["n"] or 0),
        "last_change": (vs[0].get("reason") if vs else ""),
        "last_change_ts": (vs[0].get("ts") if vs else 0),
    }
