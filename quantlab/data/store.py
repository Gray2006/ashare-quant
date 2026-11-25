# -*- coding: utf-8 -*-
"""SQLite 本地存储:自选股、K线缓存、交易记录、回测记录、筛选结果。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from ..config import DATA_DIR

DB_PATH = DATA_DIR / "quantlab.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY,
    name TEXT,
    note TEXT,
    added_at REAL,
    group_name TEXT DEFAULT '默认'
);
CREATE TABLE IF NOT EXISTS kline_cache (
    code TEXT, klt INTEGER, fqt INTEGER, bar_time TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
    PRIMARY KEY (code, klt, fqt, bar_time)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, code TEXT, name TEXT, side TEXT, price REAL, qty INTEGER,
    status TEXT, filled_price REAL, filled_qty INTEGER, fee REAL,
    gateway TEXT, remark TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    code TEXT PRIMARY KEY, name TEXT, qty INTEGER, avail_qty INTEGER,
    cost REAL, opened_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS account (
    key TEXT PRIMARY KEY, value TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, name TEXT, params TEXT, metrics TEXT, equity TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, source TEXT, code TEXT, name TEXT, score REAL, reason TEXT
);
"""


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init()

    # --- 连接(每线程一个) ---
    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def _init(self) -> None:
        with self.conn as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    # --- 维护:WAL 会一直涨(实测 2.8MB),定期 checkpoint 截断 ---
    def checkpoint(self, truncate: bool = True) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(%s)" %
                              ("TRUNCATE" if truncate else "PASSIVE"))
        except Exception:
            pass

    def vacuum(self) -> None:
        """重建库文件,回收碎片。只在空闲时调用(会锁库)。"""
        try:
            self.conn.execute("VACUUM")
        except Exception:
            pass

    def stats(self) -> dict:
        """表行数与文件大小,给设置页/自检用。"""
        out = {"size_mb": 0.0}
        try:
            out["size_mb"] = round(self.path.stat().st_size / 1e6, 2)
        except Exception:
            pass
        for t in ("orders", "positions", "kline_cache", "signals", "backtests",
                  "sim_accounts", "watchlist"):
            try:
                out[t] = int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except Exception:
                pass
        return out

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """补列:多模拟账户需要 orders/positions 带 account。"""
        for table in ("orders", "positions"):
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if "account" not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN account TEXT DEFAULT 'default'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS sim_accounts (
            name TEXT PRIMARY KEY,
            initial_cash REAL,
            created_at REAL,
            note TEXT
        )""")
        # 自选股分组:老库补列,默认归到「默认」组
        wcols = {r[1] for r in c.execute("PRAGMA table_info(watchlist)").fetchall()}
        if "group_name" not in wcols:
            c.execute("ALTER TABLE watchlist ADD COLUMN group_name TEXT DEFAULT '默认'")

    # ---------------------------------------------------------- 自选股
    def get_watchlist(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM watchlist ORDER BY added_at").fetchall()
        return [dict(r) for r in rows]

    def add_watch(self, code: str, name: str = "", note: str = "",
                  group: str = "默认") -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO watchlist(code,name,note,added_at,group_name) "
                      "VALUES(?,?,?,?,?)",
                      (code, name, note, time.time(), group or "默认"))

    def set_watch_group(self, code: str, group: str) -> None:
        with self.conn as c:
            c.execute("UPDATE watchlist SET group_name=? WHERE code=?",
                      (group or "默认", code))

    def remove_watch(self, code: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM watchlist WHERE code=?", (code,))

    # ---------------------------------------------------------- K线缓存
    def save_kline(self, code: str, klt: int, fqt: int, bars: list[dict]) -> None:
        if not bars:
            return
        rows = [(code, klt, fqt, b["datetime"], b.get("open"), b.get("high"), b.get("low"),
                 b.get("close"), b.get("volume"), b.get("amount")) for b in bars]
        with self.conn as c:
            c.executemany("INSERT OR REPLACE INTO kline_cache VALUES(?,?,?,?,?,?,?,?,?,?)", rows)

    def load_kline(self, code: str, klt: int = 101, fqt: int = 1, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM kline_cache WHERE code=? AND klt=? AND fqt=? "
            "ORDER BY bar_time DESC LIMIT ?", (code, klt, fqt, limit)).fetchall()
        out = [dict(r) for r in reversed(rows)]
        for r in out:
            r["datetime"] = r.pop("bar_time")
        return out

    def last_kline_time(self, code: str, klt: int = 101, fqt: int = 1) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(bar_time) AS t FROM kline_cache WHERE code=? AND klt=? AND fqt=?",
            (code, klt, fqt)).fetchone()
        return row["t"] if row else None

    # ---------------------------------------------------------- 交易
    def insert_order(self, o: dict) -> int:
        with self.conn as c:
            cur = c.execute(
                "INSERT INTO orders(ts,code,name,side,price,qty,status,filled_price,"
                "filled_qty,fee,gateway,remark,raw,account) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o.get("ts", time.time()), o.get("code"), o.get("name"), o.get("side"),
                 o.get("price"), o.get("qty"), o.get("status", "new"), o.get("filled_price"),
                 o.get("filled_qty", 0), o.get("fee", 0.0), o.get("gateway"),
                 o.get("remark", ""), json.dumps(o.get("raw", {}), ensure_ascii=False),
                 o.get("account") or "default"))
            return int(cur.lastrowid)

    def update_order(self, oid: int, **kw) -> None:
        if not kw:
            return
        cols = ",".join(f"{k}=?" for k in kw)
        with self.conn as c:
            c.execute(f"UPDATE orders SET {cols} WHERE id=?", (*kw.values(), oid))

    def orders(self, limit: int = 200, account: str | None = None,
               since: float | None = None) -> list[dict]:
        where, args = [], []
        if account:
            where.append("account=?")
            args.append(account)
        if since is not None:
            where.append("ts>=?")
            args.append(float(since))
        sql = "SELECT * FROM orders"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def delete_orders_by_day(self, day: str, account: str | None = None) -> int:
        """删某一天的委托记录。返回删了几条。"""
        import datetime as _dt
        d = _dt.datetime.strptime(str(day)[:10], "%Y-%m-%d")
        start = d.timestamp()
        end = (d + _dt.timedelta(days=1)).timestamp()
        where, args = ["ts>=?", "ts<?"], [start, end]
        if account:
            where.append("account=?")
            args.append(account)
        sql = "DELETE FROM orders WHERE " + " AND ".join(where)
        with self.conn as c:
            cur = c.execute(sql, tuple(args))
            return int(cur.rowcount or 0)

    def orders_today_count(self, account: str | None = None,
                           code: str | None = None) -> int:
        """当日已发委托数。可按账户和/或股票过滤。"""
        start = time.time() - (time.time() % 86400)
        sql = "SELECT COUNT(*) AS n FROM orders WHERE ts>=? AND status!='rejected'"
        args: list = [start]
        if account:
            sql += " AND account=?"
            args.append(account)
        if code:
            sql += " AND code=?"
            args.append(str(code))
        row = self.conn.execute(sql, args).fetchone()
        return int(row["n"]) if row else 0

    def save_positions(self, positions: list[dict], account: str = "default") -> None:
        with self.conn as c:
            c.execute("DELETE FROM positions WHERE account=?", (account,))
            c.executemany(
                "INSERT OR REPLACE INTO positions(code,name,qty,avail_qty,cost,opened_at,"
                "updated_at,account) VALUES(?,?,?,?,?,?,?,?)",
                [(p["code"], p.get("name"), p.get("qty", 0), p.get("avail_qty", 0),
                  p.get("cost", 0.0), p.get("opened_at", time.time()), time.time(), account)
                 for p in positions])

    def load_positions(self, account: str = "default") -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM positions WHERE account=?", (account,)).fetchall()]

    # ---------------------------------------------------------- 模拟账户
    def sim_accounts(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM sim_accounts ORDER BY created_at").fetchall()]

    def add_sim_account(self, name: str, initial_cash: float, note: str = "") -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO sim_accounts(name,initial_cash,created_at,note)"
                      " VALUES(?,?,?,?)", (name, float(initial_cash), time.time(), note))

    def remove_sim_account(self, name: str) -> None:
        with self.conn as c:
            c.execute("DELETE FROM sim_accounts WHERE name=?", (name,))
            c.execute("DELETE FROM positions WHERE account=?", (name,))
            c.execute("DELETE FROM account WHERE key LIKE ?", (f"sim_cash:{name}",))
            c.execute("DELETE FROM orders WHERE account=?", (name,))

    def set_account(self, key: str, value) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO account(key,value,updated_at) VALUES(?,?,?)",
                      (key, json.dumps(value, ensure_ascii=False), time.time()))

    def get_account(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM account WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    # ---------------------------------------------------------- 回测
    def save_backtest(self, name: str, params: dict, metrics: dict, equity: list) -> int:
        with self.conn as c:
            cur = c.execute("INSERT INTO backtests(ts,name,params,metrics,equity) VALUES(?,?,?,?,?)",
                            (time.time(), name, json.dumps(params, ensure_ascii=False),
                             json.dumps(metrics, ensure_ascii=False),
                             json.dumps(equity, ensure_ascii=False)))
            return int(cur.lastrowid)

    def backtests(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM backtests ORDER BY id DESC LIMIT ?",
                                 (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- 信号
    def save_signals(self, source: str, items: list[dict]) -> None:
        with self.conn as c:
            c.executemany(
                "INSERT INTO signals(ts,source,code,name,score,reason) VALUES(?,?,?,?,?,?)",
                [(time.time(), source, i.get("code"), i.get("name"), i.get("score"),
                  i.get("reason", "")) for i in items])

    def add_signal(self, item: dict) -> None:
        """写一条信号(自动交易/选股留痕)。"""
        self.save_signals(item.get("source") or "manual", [item])

    def signals(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?",
                                 (limit,)).fetchall()
        return [dict(r) for r in rows]


_store: Store | None = None
_store_lock = threading.Lock()


def store() -> Store:
    global _store
    if _store is None:
        # 双检锁:UI 线程和 pilot 线程可能同时首次调用,
        # 无锁时两个 Store 会同时 _init() 写库,报 database is locked
        with _store_lock:
            if _store is None:
                _store = Store()
    return _store
