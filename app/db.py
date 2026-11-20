"""SQLite connection handling and schema management.

Concurrency model
-----------------
SQLite in WAL mode allows multiple readers concurrently with one writer.
Every state-changing request runs inside an explicit ``BEGIN IMMEDIATE``
transaction, which acquires the reserved (write) lock up front. Combined
with the busy timeout, uploads and withdrawals are serialized against one
another and each sees a fully committed state. Reads use their own
snapshot transaction, so a query never observes a half-written path:
paths are *derived* from committed event rows at read time, there is no
separate path table that could be updated partially.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT    NOT NULL PRIMARY KEY,
    visitor_id    TEXT    NOT NULL,
    page_view_id  TEXT    NOT NULL,
    exhibit_id    TEXT    NOT NULL,
    event_type    TEXT    NOT NULL CHECK (event_type IN ('open', 'close')),
    event_time    TEXT    NOT NULL,
    received_at   TEXT    NOT NULL
);

-- Hot lookup paths: pairing by page view, path ordering by open time,
-- and per-visitor scans / consent checks.
CREATE INDEX IF NOT EXISTS idx_events_visitor_time
    ON events (visitor_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_visitor_page
    ON events (visitor_id, page_view_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    visitor_id  TEXT    NOT NULL PRIMARY KEY,
    withdrawn_at TEXT   NOT NULL
);

-- ---- 旧卡检索（legacy catalog cards） ----

-- 原始登记卡：品名以四位十六进制字码序列原样保存（JSON 数组）。
CREATE TABLE IF NOT EXISTS legacy_cards (
    batch_id    TEXT    NOT NULL,
    card_no     TEXT    NOT NULL,
    codes       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (batch_id, card_no)
);

-- 字码倒排：换表时按变动字码精确找出需要重建的卡，避免全批扫描。
CREATE TABLE IF NOT EXISTS legacy_card_codes (
    batch_id TEXT    NOT NULL,
    card_no  TEXT    NOT NULL,
    position INTEGER NOT NULL,
    code     TEXT    NOT NULL,
    PRIMARY KEY (batch_id, card_no, position)
);
CREATE INDEX IF NOT EXISTS idx_legacy_card_codes_code
    ON legacy_card_codes (batch_id, code);

-- 对照表版本历史；每批至多一张 active 表（部分唯一索引保证）。
CREATE TABLE IF NOT EXISTS legacy_mapping_tables (
    batch_id   TEXT    NOT NULL,
    version    INTEGER NOT NULL,
    entries    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    PRIMARY KEY (batch_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_mapping_active
    ON legacy_mapping_tables (batch_id) WHERE active = 1;

-- 派生转写索引：与对照表在同一事务内重建，读者不会看到表/索引错位。
CREATE TABLE IF NOT EXISTS legacy_card_index (
    batch_id      TEXT    NOT NULL,
    card_no       TEXT    NOT NULL,
    texts         TEXT    NOT NULL,
    full_name     TEXT,
    complete      INTEGER NOT NULL CHECK (complete IN (0, 1)),
    unmapped      TEXT    NOT NULL,
    table_version INTEGER,
    PRIMARY KEY (batch_id, card_no)
);
CREATE INDEX IF NOT EXISTS idx_legacy_card_index_name
    ON legacy_card_index (full_name) WHERE complete = 1;
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with the pragmas required for safe concurrency."""
    conn = sqlite3.connect(
        db_path or config.DB_PATH,
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # manual transaction control
        # Sync FastAPI endpoints run in uvicorn's worker threadpool, so the
        # connection created by the dependency may be touched from a pool
        # thread. Each request gets its own connection (never shared), which
        # makes cross-thread use safe; SQLite file locking + BEGIN IMMEDIATE
        # still serialize the actual writers.
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create tables and indexes if they do not yet exist."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_connection() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, closed afterwards.

    FastAPI consumes this generator directly: the yielded connection is
    injected into the endpoint, and the ``finally`` block runs when the
    request finishes. Do not wrap it in ``@contextmanager`` — doing so hides
    the generator signature from FastAPI's dependency analysis.
    """
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire the write lock immediately and commit/rollback atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
