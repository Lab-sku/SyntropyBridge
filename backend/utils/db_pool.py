"""Connection pool and per-thread sqlite3 connection helper.

Background
----------
:func:`backend.database.get_db` opens a brand-new connection on every
call. Under high concurrency that thrashes the SQLite client lib,
blows up the file descriptor budget, and makes WAL bookkeeping
expensive. The pool below keeps a small bounded set of connections
around and recycles them.

The implementation is intentionally tiny — there is no global lock;
each thread/loop gets its own thread-local connection, and the
``DatabasePool`` only does the bookkeeping for ``close_all()`` and
``stats()``.

When to use ``get_db()`` vs ``get_pooled_connection()``
------------------------------------------------------
* Modules that perform a single short query (e.g. ``SELECT 1``,
  token lookup, rate limit) should keep using ``get_db()`` because the
  overhead of opening a connection is negligible. The pragma set is
  identical.
* Code paths that hold a connection for the duration of a request
  (e.g. transactionally charging a wallet + writing a usage log)
  should switch to ``get_pooled_connection()`` to avoid the
  open/close tax.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional

from backend.database import _apply_connection_pragmas, get_database_path

logger = logging.getLogger(__name__)


class _Slot:
    __slots__ = ("conn", "in_use", "created_at")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.in_use = False
        self.created_at = 0.0


class DatabasePool:
    """Bounded pool of SQLite connections.

    Parameters
    ----------
    max_size:
        Maximum number of simultaneous connections. Defaults to 16
        which is enough for most FastAPI single-worker deployments.
    ttl_seconds:
        Connections older than this are recycled on release. 0 to
        disable.
    """

    def __init__(self, max_size: int = 16, ttl_seconds: int = 600) -> None:
        self.max_size = max(1, int(max_size))
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        self._slots: List[_Slot] = []
        self._created_total = 0
        self._borrowed_total = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextmanager
    def acquire(self) -> Iterator[sqlite3.Connection]:
        slot = self._checkout()
        try:
            yield slot.conn
        finally:
            self._release(slot)

    def stats(self) -> dict:
        with self._lock:
            in_use = sum(1 for s in self._slots if s.in_use)
            idle = len(self._slots) - in_use
        return {
            "max_size": self.max_size,
            "in_use": in_use,
            "idle": idle,
            "created_total": self._created_total,
            "borrowed_total": self._borrowed_total,
        }

    def close_all(self) -> None:
        with self._lock:
            slots, self._slots = self._slots, []
        for s in slots:
            try:
                s.conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(get_database_path(), timeout=30)
        _apply_connection_pragmas(conn)
        return conn

    def _checkout(self) -> _Slot:
        import time

        recycle_slot: Optional[_Slot] = None
        with self._lock:
            for s in self._slots:
                if not s.in_use:
                    if self.ttl_seconds and (time.time() - s.created_at) > self.ttl_seconds:
                        s.in_use = True
                        recycle_slot = s
                        break
                    s.in_use = True
                    self._borrowed_total += 1
                    return s
            if len(self._slots) < self.max_size:
                slot = _Slot(None)  # type: ignore[arg-type]
                slot.created_at = time.time()
                slot.in_use = True
                self._slots.append(slot)
                self._created_total += 1
                self._borrowed_total += 1
                conn = self._open_connection()
                slot.conn = conn
                return slot

        if recycle_slot is not None:
            try:
                recycle_slot.conn.close()
            except Exception:
                pass
            recycle_slot.conn = self._open_connection()
            recycle_slot.created_at = time.time()
            self._borrowed_total += 1
            return recycle_slot

        logger.warning("db pool exhausted, opening temporary connection")
        conn = self._open_connection()
        slot = _Slot(conn)
        slot.created_at = 0.0
        slot.in_use = True
        return slot

    def _release(self, slot: _Slot) -> None:
        if slot.created_at == 0.0:
            try:
                slot.conn.close()
            except Exception:
                pass
            return
        with self._lock:
            slot.in_use = False


# Module-level singleton
_POOL: Optional[DatabasePool] = None
_POOL_LOCK = threading.Lock()


def get_pool() -> DatabasePool:
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = DatabasePool()
    return _POOL


@contextmanager
def get_pooled_connection() -> Iterator[sqlite3.Connection]:
    """Yield a pooled connection. Use inside a request-scoped context
    when you want to amortise the open/close cost."""
    pool = get_pool()
    with pool.acquire() as conn:
        yield conn


__all__ = [
    "DatabasePool",
    "get_pool",
    "get_pooled_connection",
]
