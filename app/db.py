"""SQLite storage for bills/finance uploads + normalized transactions."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    source TEXT,
    uploaded_at TEXT,
    row_count INTEGER,
    status TEXT
);
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES files(id),
    date TEXT,
    amount REAL,
    direction TEXT,
    description TEXT,
    category TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_file ON transactions(file_id);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
"""


class Store:
    def __init__(self, path: str = "/data/money.db"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._local = threading.local()
        self._wlock = threading.Lock()  # serialize writers (single process)
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init(self):
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ---- files ----
    def add_file(self, filename: str, source: str, rows: int) -> int:
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO files(filename, source, uploaded_at, row_count, status)"
                    " VALUES (?,?,?,?,?)",
                    (filename, source, _now(), rows, "ok"),
                )
                conn.commit()
                return cur.lastrowid
            except Exception:
                conn.rollback()
                raise

    def list_files(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT f.*, (SELECT COUNT(*) FROM transactions t WHERE t.file_id=f.id) AS tx_count"
            " FROM files f ORDER BY f.uploaded_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_file(self, file_id: int) -> bool:
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM transactions WHERE file_id=?", (file_id,))
                cur = conn.execute("DELETE FROM files WHERE id=?", (file_id,))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                conn.rollback()
                raise

    def delete_all(self):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM transactions")
                conn.execute("DELETE FROM files")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ---- transactions ----
    def bulk_insert(self, file_id: int, txs: list[dict]):
        with self._wlock:
            conn = self._conn()
            try:
                conn.executemany(
                    "INSERT INTO transactions(file_id, date, amount, direction, description, category, source)"
                    " VALUES (?,?,?,?,?,?,?)",
                    [
                        (file_id, t["date"], t["amount"], t["direction"],
                         t["description"], t["category"], t["source"])
                        for t in txs
                    ],
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def query(self, source=None, category=None, direction=None,
              start=None, end=None, description=None) -> list[dict]:
        conn = self._conn()
        sql = "SELECT * FROM transactions WHERE 1=1"
        args = []
        if source:
            sql += " AND source=?"
            args.append(source)
        if category:
            sql += " AND LOWER(category)=LOWER(?)"
            args.append(category)
        if direction:
            sql += " AND direction=?"
            args.append(direction)
        if start:
            sql += " AND date>=?"
            args.append(start)
        if end:
            sql += " AND date<=?"
            args.append(end)
        if description:
            sql += " AND LOWER(description) LIKE ?"
            args.append(f"%{description.lower()}%")
        sql += " ORDER BY date"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def distinct(self, field: str) -> list[str]:
        conn = self._conn()
        rows = conn.execute(
            f"SELECT DISTINCT {field} AS v FROM transactions WHERE {field} IS NOT NULL AND {field}<>'' ORDER BY v"
        ).fetchall()
        return [r["v"] for r in rows]

    def totals(self) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT"
            " SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) AS outflow,"
            " SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) AS inflow,"
            " COUNT(*) AS n FROM transactions"
        ).fetchone()
        return dict(row)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
