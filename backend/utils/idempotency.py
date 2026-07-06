"""Idempotency-key store for state-changing endpoints.

Why
----
Charge / refund / wallet-adjust / order-create are *non-idempotent* by
default — replaying the same request twice double-debits the user. The
HTTP convention is to let the client supply an ``Idempotency-Key`` and
have the server cache the response keyed by that value. If a duplicate
request arrives within the retention window, the cached response is
returned verbatim.

Implementation
--------------
* Backed by a SQLite table (``idempotency_keys``) so the cache survives
  restarts and works across the (single-process) FastAPI worker.
* Stored fields: ``key``, ``method``, ``route``, ``request_hash``,
  ``status_code``, ``response_body``, ``created_at``.
* Retention: 24 hours. A background-free cleanup runs opportunistically
  inside :func:`check_or_reserve` — cheap because it's a single
  ``DELETE`` with a WHERE clause and runs at most once per minute.
* Locking: the ``BEGIN IMMEDIATE`` transaction guarantees only one
  writer wins the "reserve" race, even under SQLite WAL + concurrent
  worker threads.

This is *not* intended as a distributed lock. For multi-replica
deployments swap the SQLite layer for Redis / DynamoDB.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from backend.database import get_db

logger = logging.getLogger(__name__)

# 24h retention. Long enough to cover retries but short enough that
# the table doesn't grow unbounded.
RETENTION_SECONDS = 24 * 60 * 60
_last_cleanup_ts: float = 0.0
CLEANUP_INTERVAL = 60.0  # seconds

# ---------------------------------------------------------------------------
# 表创建缓存（M8 修复）
# ---------------------------------------------------------------------------
# ``_ensure_table`` 在每次幂等键命中前都会被调用。原始实现每次都执行
# ``CREATE TABLE IF NOT EXISTS`` —— 在 SQLite 单写连接下，这条语句即便
# 是无操作（no-op）也意味着一次 PRAGMA / 系统表查询 + 写事务提交，对
# 高频热路径是不必要的开销。
#
# 这里用一个模块级 ``_table_ready`` 标志短路掉热路径：第一次成功创建后
# 直接 return。为了兼容测试场景（每个测试用例都会换一个临时 DB 文件），
# 同时记录 ``_table_ready_db_path``：当 ``DATABASE_PATH`` 发生变化时
# 自动失效，让新文件第一次访问时仍然会建表。
_table_ready: bool = False
_table_ready_db_path: Optional[str] = None
_table_ready_lock = threading.Lock()


@dataclass
class IdempotencyResult:
    """Outcome of an idempotency lookup.

    * ``hit`` is True when we found a previously stored response for
      the same key+route+body-hash. The caller MUST return the cached
      response without re-running the side effect.
    * ``reserved`` is True when we successfully claimed the key for
      the current request. The caller proceeds and *must* eventually
      call :func:`finalize` to persist the response.
    """

    hit: bool
    reserved: bool
    status_code: int = 0
    response_body: Any = None


@dataclass
class StreamIdempotencyResult:
    """Outcome of a streaming idempotency lookup.

    * ``status="proceed"`` — key was free and is now reserved; start
      streaming and call :func:`finalize_stream` when done.
    * ``status="in_progress"`` — another stream is actively running
      with this key; the caller should return 409 Conflict.
    * ``status="completed"`` — a previous stream finished with this
      key; ``cached_usage`` contains the token counts and model so
      the caller can synthesise a short replay.
    * ``status="body_mismatch"`` — same key, different request body;
      the caller should return 422.
    """

    status: str  # "proceed" | "in_progress" | "completed" | "body_mismatch"
    cached_usage: Optional[Dict[str, Any]] = None


def _hash_body(body: Any) -> str:
    try:
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canonical = repr(body)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ensure_stream_column(conn) -> None:
    """Add the ``stream_status`` column used by streaming idempotency.

    Values: ``NULL`` (non-stream / legacy), ``'pending'`` (stream in
    progress), ``'completed'`` (stream finished, usage cached).  The
    column is added via ALTER TABLE if absent — idempotent across
    restarts.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(idempotency_keys)")
    cols = {row[1] for row in cur.fetchall()}
    if "stream_status" not in cols:
        try:
            cur.execute("ALTER TABLE idempotency_keys ADD COLUMN stream_status TEXT")
            conn.commit()
        except Exception:
            # Another thread may have added it concurrently.
            try:
                conn.rollback()
            except Exception:
                pass


def check_or_reserve_stream(
    *,
    key: str,
    method: str,
    route: str,
    body: Any,
) -> StreamIdempotencyResult:
    """Reserve or look up an idempotency key for a *streaming* endpoint.

    Unlike :func:`check_or_reserve` which stores a full HTTP response,
    streaming endpoints only cache a lightweight completion marker with
    the final token counts.  This function manages a ``stream_status``
    column:

    * ``NULL / absent`` → key is free → reserve with ``'pending'``.
    * ``'pending'`` → another stream is running → return ``in_progress``.
    * ``'completed'`` → stream already finished → return ``completed``
      with the cached usage dict from ``response_body``.
    """
    if not key:
        return StreamIdempotencyResult(status="proceed")

    safe_key = key.strip()[:120]
    safe_method = method.upper()[:10]
    safe_route = route[:200]
    body_hash = _hash_body(body)

    conn = get_db()
    try:
        _ensure_table(conn)
        _ensure_stream_column(conn)
        _maybe_cleanup(conn)
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                """
                SELECT request_hash, status_code, response_body, stream_status
                  FROM idempotency_keys
                 WHERE key = ? AND method = ? AND route = ?
                """,
                (safe_key, safe_method, safe_route),
            )
            row = cur.fetchone()
            if row is not None:
                existing_hash = row[0]
                existing_status_code = int(row[1] or 0)
                existing_body = row[2]
                existing_stream_status = row[3]

                if existing_hash != body_hash:
                    conn.rollback()
                    return StreamIdempotencyResult(status="body_mismatch")

                if existing_stream_status == "pending":
                    # Another stream is actively running with this key.
                    conn.rollback()
                    return StreamIdempotencyResult(status="in_progress")

                if existing_stream_status == "completed":
                    # Stream finished — return cached usage.
                    conn.rollback()
                    cached = None
                    try:
                        cached = json.loads(existing_body) if existing_body else None
                    except Exception:
                        cached = None
                    return StreamIdempotencyResult(status="completed", cached_usage=cached)

                # Legacy row without stream_status (status_code == 0 means
                # reserved-but-not-finalized from a non-stream call). Treat
                # as in_progress to avoid double-execution.
                if existing_status_code == 0:
                    conn.rollback()
                    return StreamIdempotencyResult(status="in_progress")

                # Completed non-stream row — return its response_body as
                # cached usage so the caller can synthesise a replay.
                conn.rollback()
                cached = None
                try:
                    cached = json.loads(existing_body) if existing_body else None
                except Exception:
                    cached = None
                return StreamIdempotencyResult(status="completed", cached_usage=cached)

            # Reserve the key with stream_status='pending'.
            cur.execute(
                """
                INSERT INTO idempotency_keys
                    (key, method, route, request_hash, status_code,
                     response_body, stream_status)
                VALUES (?, ?, ?, ?, 0, NULL, 'pending')
                """,
                (safe_key, safe_method, safe_route, body_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return StreamIdempotencyResult(status="proceed")


def finalize_stream(
    *,
    key: str,
    method: str,
    route: str,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
) -> None:
    """Mark a streaming reservation as completed, caching the usage."""
    if not key:
        return
    safe_key = key.strip()[:120]
    safe_method = method.upper()[:10]
    safe_route = route[:200]
    usage_payload = json.dumps(
        {
            "status": "completed",
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens) + int(completion_tokens),
            "model": model,
        },
        ensure_ascii=False,
    )
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE idempotency_keys
               SET status_code = 200,
                   response_body = ?,
                   stream_status = 'completed'
             WHERE key = ? AND method = ? AND route = ?
            """,
            (usage_payload, safe_key, safe_method, safe_route),
        )
        conn.commit()
    except Exception as e:
        logger.warning("idempotency finalize_stream failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def _maybe_cleanup(conn) -> None:
    global _last_cleanup_ts
    now = time.time()
    if (now - _last_cleanup_ts) < CLEANUP_INTERVAL:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM idempotency_keys WHERE created_at < datetime('now', '-1 day')")
        conn.commit()
        _last_cleanup_ts = now
    except Exception:
        # Cleanup failure is non-fatal.
        try:
            conn.rollback()
        except Exception:
            pass


def _ensure_table(conn) -> None:
    # 热路径优化（M8）：表已建好且 DB 路径未变时直接返回，避免每次
    # 都跑一遍 ``CREATE TABLE IF NOT EXISTS``。用 DB 路径做 key 是
    # 为了让测试 fixture 切换临时文件时自动失效重建。
    global _table_ready, _table_ready_db_path
    try:
        from backend import database as _db_module

        current_path = getattr(_db_module, "DATABASE_PATH", None)
    except Exception:
        current_path = None
    if _table_ready and _table_ready_db_path == current_path:
        return
    with _table_ready_lock:
        # 双重检查：拿到锁后再核对一次，避免多个线程同时进入后
        # 重复建表。同时仍然要校验路径，防止另一个线程刚换库。
        if _table_ready and _table_ready_db_path == current_path:
            return
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key VARCHAR(120) NOT NULL,
                method VARCHAR(10) NOT NULL,
                route VARCHAR(200) NOT NULL,
                request_hash VARCHAR(64) NOT NULL,
                status_code INTEGER NOT NULL DEFAULT 0,
                response_body TEXT,
                stream_status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key, method, route)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_created ON idempotency_keys(created_at)"
        )
        conn.commit()
        _table_ready_db_path = current_path
        _table_ready = True


def check_or_reserve(
    *,
    key: str,
    method: str,
    route: str,
    body: Any,
) -> IdempotencyResult:
    """Look up an idempotency key, reserving it if free.

    See :class:`IdempotencyResult` for the return semantics.
    """
    if not key:
        # No key supplied — behave as if the caller disabled the
        # feature. Side effects will be non-idempotent.
        return IdempotencyResult(hit=False, reserved=False)

    safe_key = key.strip()[:120]
    safe_method = method.upper()[:10]
    safe_route = route[:200]
    body_hash = _hash_body(body)

    conn = get_db()
    try:
        _ensure_table(conn)
        _maybe_cleanup(conn)
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                """
                SELECT request_hash, status_code, response_body
                  FROM idempotency_keys
                 WHERE key = ? AND method = ? AND route = ?
                """,
                (safe_key, safe_method, safe_route),
            )
            row = cur.fetchone()
            if row is not None:
                if row[0] != body_hash:
                    # Same key, different body — that's a client bug.
                    # RFC draft: return 422.
                    conn.rollback()
                    return IdempotencyResult(
                        hit=True,
                        reserved=False,
                        status_code=422,
                        response_body={
                            "detail": "Idempotency-Key 已被用于不同的请求体",
                            "code": "IDEMPOTENCY_KEY_REUSED",
                        },
                    )
                # Cache hit — return the stored response.
                conn.rollback()
                raw = row[2]
                try:
                    payload = json.loads(raw) if raw else None
                except Exception:
                    payload = raw
                return IdempotencyResult(
                    hit=True,
                    reserved=False,
                    status_code=int(row[1] or 200),
                    response_body=payload,
                )
            # Reserve the key with an empty body; finalize() will
            # update it once the operation completes.
            cur.execute(
                """
                INSERT INTO idempotency_keys
                    (key, method, route, request_hash, status_code, response_body)
                VALUES (?, ?, ?, ?, 0, NULL)
                """,
                (safe_key, safe_method, safe_route, body_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return IdempotencyResult(hit=False, reserved=True)


def finalize(
    *,
    key: str,
    method: str,
    route: str,
    status_code: int,
    response_body: Any,
) -> None:
    """Persist the final response for a previously reserved key."""
    if not key:
        return
    safe_key = key.strip()[:120]
    safe_method = method.upper()[:10]
    safe_route = route[:200]
    try:
        payload = json.dumps(response_body, ensure_ascii=False, default=str)
    except Exception:
        payload = json.dumps({"detail": "response not serializable"})

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE idempotency_keys
               SET status_code = ?, response_body = ?
             WHERE key = ? AND method = ? AND route = ?
            """,
            (int(status_code), payload, safe_key, safe_method, safe_route),
        )
        conn.commit()
    except Exception as e:
        logger.warning("idempotency finalize failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def release(*, key: str, method: str, route: str) -> None:
    """Drop a reserved key. Use when a reserved request errored out
    *before* producing a final response (e.g. upstream timeout) so the
    client can retry the same key safely."""
    if not key:
        return
    safe_key = key.strip()[:120]
    safe_method = method.upper()[:10]
    safe_route = route[:200]
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM idempotency_keys WHERE key = ? AND method = ? AND route = ? AND status_code = 0",
            (safe_key, safe_method, safe_route),
        )
        conn.commit()
    except Exception as e:
        logger.warning("idempotency release failed: %s", e)
    finally:
        conn.close()


__all__ = [
    "IdempotencyResult",
    "StreamIdempotencyResult",
    "check_or_reserve",
    "check_or_reserve_stream",
    "finalize",
    "finalize_stream",
    "release",
    "RETENTION_SECONDS",
]
