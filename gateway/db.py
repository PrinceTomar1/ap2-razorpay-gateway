"""SQLite plumbing. One file, stdlib only, no other infrastructure.

Why SQLite and not Postgres: the properties this project actually needs from a
database are durability, a serialisable write path, and the ability to hand a
reviewer a single file they can open and inspect. SQLite has all three. Adding a
server would add operational surface without adding a guarantee.

Concurrency: every connection here runs in autocommit mode
(``isolation_level=None``) with explicit ``BEGIN IMMEDIATE`` around the handful
of read-modify-write sequences that must be atomic — reserving an idempotency
key, appending to the hash chain. ``check_same_thread=False`` plus a per-database
lock, because FastMCP runs sync tools in a worker thread pool.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MEMORY = ":memory:"


class Database:
    """A single SQLite database with a lock around its write transactions."""

    def __init__(self, path: str | Path = MEMORY) -> None:
        self.path = str(path)
        if self.path != MEMORY:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != MEMORY:
            # WAL keeps readers (the audit pretty-printer) from blocking the
            # writer (the payment path). Meaningless for an in-memory database.
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """An exclusive write transaction.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: the sequences
        wrapped in this are all read-then-write (does this idempotency key exist?
        then claim it), and a deferred transaction would let two of them read the
        same "no" before either writes.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
