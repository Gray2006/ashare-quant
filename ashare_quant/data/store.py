"""本地 SQLite 行情缓存。

数据只落在用户自己的机器上，默认不入库、不上传、不再分发。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    industry TEXT,
    region TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, amplitude REAL,
    pct_chg REAL, change REAL, turnover REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
CREATE TABLE IF NOT EXISTS index_daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, pct_chg REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

BAR_COLUMNS = ["code", "date", "open", "high", "low", "close",
               "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- stocks ----------
    def save_stocks(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        now = pd.Timestamp.now().isoformat(timespec="seconds")
        rows = []
        for record in frame.to_dict("records"):
            code = str(record.get("code") or "")
            if not code:
                continue
            rows.append((
                code,
                record.get("name"),
                str(record.get("market") or ""),
                record.get("industry"),
                record.get("region"),
                now,
            ))
        self.conn.executemany(
            """INSERT INTO stocks(code,name,market,industry,region,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 name=excluded.name, market=excluded.market,
                 industry=excluded.industry, region=excluded.region,
                 updated_at=excluded.updated_at""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def stocks(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM stocks ORDER BY code", self.conn)

    # ---------- daily bars ----------
    def save_bars(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        frame = frame.copy()
        for column in BAR_COLUMNS:
            if column not in frame.columns:
                frame[column] = None
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        frame = frame[BAR_COLUMNS]
        rows = [tuple(None if pd.isna(v) else v for v in row)
                for row in frame.itertuples(index=False, name=None)]
        self.conn.executemany(
            """INSERT INTO daily(code,date,open,high,low,close,volume,amount,
                                 amplitude,pct_chg,change,turnover)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code,date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume,
                 amount=excluded.amount, amplitude=excluded.amplitude,
                 pct_chg=excluded.pct_chg, change=excluded.change,
                 turnover=excluded.turnover""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def save_index_bars(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        columns = ["code", "date", "open", "high", "low", "close",
                   "volume", "amount", "pct_chg"]
        frame = frame.copy()
        for column in columns:
            if column not in frame.columns:
                frame[column] = None
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
        rows = [tuple(None if pd.isna(v) else v for v in row)
                for row in frame[columns].itertuples(index=False, name=None)]
        self.conn.executemany(
            """INSERT INTO index_daily(code,date,open,high,low,close,volume,amount,pct_chg)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code,date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume,
                 amount=excluded.amount, pct_chg=excluded.pct_chg""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def last_dates(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT code, MAX(date) FROM daily GROUP BY code").fetchall()
        return {code: date for code, date in rows if date}

    def latest_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(date) FROM daily").fetchone()
        return row[0] if row and row[0] else None

    def count_bars(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM daily").fetchone()
        return int(row[0]) if row else 0

    def load_bars(
        self,
        codes: Sequence[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        sql = "SELECT * FROM daily WHERE 1=1"
        params: list = []
        if codes:
            if isinstance(codes, str):
                codes = [codes]
            placeholders = ",".join("?" for _ in codes)
            sql += f" AND code IN ({placeholders})"
            params.extend(codes)
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date, code"
        frame = pd.read_sql_query(sql, self.conn, params=params)
        return frame

    def panel(
        self,
        start: str | None = None,
        end: str | None = None,
        codes: Sequence[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """返回 {字段: DataFrame(index=date, columns=code)}，便于横截面计算。"""
        long = self.load_bars(codes=codes, start=start, end=end)
        if long.empty:
            return {}
        long["date"] = pd.to_datetime(long["date"])
        panel: dict[str, pd.DataFrame] = {}
        for field in ("open", "high", "low", "close", "volume", "amount",
                      "pct_chg", "turnover", "amplitude"):
            if field not in long.columns:
                continue
            panel[field] = long.pivot(index="date", columns="code", values=field).sort_index()
        return panel

    def index_panel(self, code: str, start: str | None = None,
                    end: str | None = None) -> pd.DataFrame:
        sql = "SELECT * FROM index_daily WHERE code = ?"
        params: list = [code]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date"
        frame = pd.read_sql_query(sql, self.conn, params=params)
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date")
        return frame

    def coverage(self) -> dict:
        stocks = self.conn.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
        with_bars = self.conn.execute(
            "SELECT COUNT(DISTINCT code) FROM daily"
        ).fetchone()[0]
        dates = self.conn.execute(
            "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM daily"
        ).fetchone()
        return {
            "stocks": int(stocks),
            "codes_with_bars": int(with_bars),
            "first_date": dates[0],
            "last_date": dates[1],
            "trading_days": int(dates[2] or 0),
            "rows": self.count_bars(),
        }

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
