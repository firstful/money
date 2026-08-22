"""SQLite storage for bills/finance uploads + normalized transactions."""

from __future__ import annotations

import os
import json
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

CREATE TABLE IF NOT EXISTS plaid_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT UNIQUE,
    access_token TEXT,
    institution_id TEXT,
    institution_name TEXT,
    institution_products TEXT,
    accounts_json TEXT,
    cursor TEXT,
    env TEXT,
    linked_at TEXT
);
CREATE TABLE IF NOT EXISTS plaid_transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT UNIQUE,
    item_id TEXT,
    account_id TEXT,
    date TEXT,
    name TEXT,
    merchant_name TEXT,
    amount REAL,
    iso_currency TEXT,
    category TEXT,
    pending INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ptx_item ON plaid_transactions(item_id);
CREATE INDEX IF NOT EXISTS idx_ptx_date ON plaid_transactions(date);
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT,
    retailer TEXT,
    date TEXT,
    total REAL,
    items_json TEXT,
    source TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS correlations(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id TEXT,
    order_no TEXT,
    score REAL,
    status TEXT,
    split_json TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_corr_order ON correlations(order_no);
CREATE INDEX IF NOT EXISTS idx_corr_status ON correlations(status);
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
        self._migrate()

    def _migrate(self):
        """Additive column migrations for DBs created by older builds."""
        conn = self._conn()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(plaid_items)")}
        if "institution_name" not in cols:
            conn.execute("ALTER TABLE plaid_items ADD COLUMN institution_name TEXT")
        if "institution_products" not in cols:
            conn.execute("ALTER TABLE plaid_items ADD COLUMN institution_products TEXT")
        conn.commit()
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

    # ---- plaid items ----
    def upsert_plaid_item(self, item_id: str, access_token: str, institution_id: str,
                          accounts: list[dict], env: str,
                          institution_name: str = "", institution_products: str = "") -> int:
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO plaid_items(item_id, access_token, institution_id,"
                    " institution_name, institution_products,"
                    " accounts_json, cursor, env, linked_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(item_id) DO UPDATE SET institution_id=excluded.institution_id,"
                    " institution_name=excluded.institution_name,"
                    " institution_products=excluded.institution_products,"
                    " accounts_json=excluded.accounts_json, env=excluded.env",
                    (item_id, access_token, institution_id,
                     institution_name, institution_products,
                     json.dumps(accounts, default=str), "", env, _now()),
                )
                conn.commit()
                row = conn.execute("SELECT id FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
                return int(row["id"])
            except Exception:
                conn.rollback()
                raise

    def set_plaid_institution(self, item_id: str, name: str, products: str):
        """Backfill institution_name/products on an already-stored item."""
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE plaid_items SET institution_name=?, institution_products=?"
                    " WHERE item_id=?",
                    (name, products, item_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def list_plaid_items(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, item_id, institution_id, institution_name, institution_products,"
            " accounts_json, cursor, env, linked_at,"
            " (SELECT COUNT(*) FROM plaid_transactions t WHERE t.item_id=plaid_items.item_id) AS tx_count"
            " FROM plaid_items ORDER BY linked_at"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["accounts"] = json.loads(d.get("accounts_json") or "[]")
            except Exception:
                d["accounts"] = []
            d["accounts_json"] = None  # don't echo raw json
            out.append(d)
        return out

    def get_plaid_item(self, item_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT * FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def set_plaid_cursor(self, item_id: str, cursor: str):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("UPDATE plaid_items SET cursor=? WHERE item_id=?", (cursor, item_id))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def list_plaid_items_for_sync(self, env: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM plaid_items WHERE env=?", (env,)).fetchall()
        return [dict(r) for r in rows]

    # ---- plaid transactions ----
    def upsert_plaid_tx(self, tx: dict, item_id: str) -> bool:
        """Returns True if this is a NEW row (added), False if an update."""
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO plaid_transactions(tx_id, item_id, account_id, date, name,"
                    " merchant_name, amount, iso_currency, category, pending)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(tx_id) DO UPDATE SET date=excluded.date, name=excluded.name,"
                    " merchant_name=excluded.merchant_name, amount=excluded.amount,"
                    " category=excluded.category, pending=excluded.pending",
                    (tx["transaction_id"], item_id, tx.get("account_id", ""),
                     tx.get("date", ""), tx.get("name", ""), tx.get("merchant_name") or "",
                     tx.get("amount") or 0.0, tx.get("iso_currency") or "",
                     _first_category(tx.get("category")), 1 if tx.get("pending") else 0),
                )
                conn.commit()
                return cur.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def delete_plaid_tx(self, tx_id: str):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM plaid_transactions WHERE tx_id=?", (tx_id,))
                conn.execute("DELETE FROM correlations WHERE tx_id=?", (tx_id,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def query_plaid(self, item_id: str = "", merchant: str = "",
                    start: str = "", end: str = "", limit: int = 2000) -> list[dict]:
        conn = self._conn()
        sql = "SELECT * FROM plaid_transactions WHERE 1=1"
        args = []
        if item_id:
            sql += " AND item_id=?"
            args.append(item_id)
        if merchant:
            sql += " AND (LOWER(name) LIKE ? OR LOWER(merchant_name) LIKE ?)"
            args += [f"%{merchant.lower()}%", f"%{merchant.lower()}%"]
        if start:
            sql += " AND date>=?"
            args.append(start)
        if end:
            sql += " AND date<=?"
            args.append(end)
        sql += " ORDER BY date DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # ---- orders ----
    def add_order(self, order_no: str, retailer: str, date: str, total: float,
                  items: list[dict], source: str = "manual") -> int:
        with self._wlock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "INSERT INTO orders(order_no, retailer, date, total, items_json, source, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (order_no, retailer, date, total, json.dumps(items), source, _now()),
                )
                conn.commit()
                return cur.lastrowid
            except Exception:
                conn.rollback()
                raise

    def list_orders(self, retailer: str = "") -> list[dict]:
        conn = self._conn()
        sql = "SELECT * FROM orders WHERE 1=1"
        args = []
        if retailer:
            sql += " AND LOWER(retailer)=LOWER(?)"
            args.append(retailer)
        sql += " ORDER BY date DESC"
        out = []
        for r in conn.execute(sql, args).fetchall():
            d = dict(r)
            try:
                d["items"] = json.loads(d.get("items_json") or "[]")
            except Exception:
                d["items"] = []
            d["items_json"] = None
            out.append(d)
        return out

    # ---- correlations ----
    def upsert_correlation(self, tx_id: str, order_no: str, score: float, status: str,
                           split: list[dict] | None = None):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO correlations(tx_id, order_no, score, status, split_json)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT DO NOTHING",
                    (tx_id, order_no, score, status, json.dumps(split) if split else None),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_correlation(self, tx_id: str, order_no: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM correlations WHERE tx_id=? AND order_no=?", (tx_id, order_no)
        ).fetchone()
        return dict(row) if row else None

    def resolve_correlation(self, corr_id: int, status: str, resolved_by: str):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE correlations SET status=?, resolved_by=?, resolved_at=? WHERE id=?",
                    (status, resolved_by, _now(), corr_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def query_correlations(self, status: str = "") -> list[dict]:
        conn = self._conn()
        sql = ("SELECT c.*, t.name AS tx_name, t.merchant_name AS tx_merchant,"
               " t.amount AS tx_amount, t.date AS tx_date, o.retailer, o.total AS order_total, o.date AS order_date, o.items_json"
               " FROM correlations c"
               " LEFT JOIN plaid_transactions t ON t.tx_id=c.tx_id"
               " LEFT JOIN orders o ON o.order_no=c.order_no WHERE 1=1")
        args = []
        if status:
            sql += " AND c.status=?"
            args.append(status)
        sql += " ORDER BY c.score DESC"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def clear_correlations(self):
        with self._wlock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM correlations")
                conn.commit()
            except Exception:
                conn.rollback()
                raise



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first_category(cat) -> str:
    """Plaid `category` is a list like ['Travel','Airlines'] → keep the leaf."""
    try:
        if isinstance(cat, (list, tuple)):
            return cat[-1] if cat else ""
        return str(cat or "")
    except Exception:
        return ""
