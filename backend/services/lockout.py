"""Brute-force / credential-stuffing lockout.

A tiny, SQLite-backed counter that bumps on every failed login and
auto-resets after a successful one. The lockout is **per identifier**
(username *or* ip) and is intentionally separate from the per-minute
HTTP rate limiter in :mod:`backend.main` — the rate limiter is
anonymous and coarse, this one is targeted at the auth surface.

Two identifiers are tracked so a brute-forcer can't bypass the
counter by rotating usernames; both windows must be open for a
login to be allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

from backend.database import get_db


@dataclass
class LockoutDecision:
    allowed: bool
    retry_after: float
    failure_count: int


_FAILURE_TABLE = "auth_failures"

# Defaults — overridable by env if we need to dial them in prod.
WINDOW_SECONDS = 15 * 60
MAX_FAILURES = 8


def _ensure_table(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {_FAILURE_TABLE} (
            identifier VARCHAR(120) NOT NULL,
            scope VARCHAR(20) NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            first_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (identifier, scope)
        )
    """)


def _parse_ts(ts) -> Optional[datetime]:
    """Parse a SQLite ``CURRENT_TIMESTAMP`` string (UTC) to a datetime.

    Returns ``None`` on malformed input so the caller can fall back to
    the conservative full-window default.
    """
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _remaining_seconds(first_failure_at, window_seconds: int) -> float:
    """Compute remaining lockout seconds from ``first_failure_at``.

    Returns ``max(0, window_seconds - elapsed)`` so the value decays
    toward zero as the lockout window expires. Falls back to the full
    window when the timestamp can't be parsed (defensive: never tell
    the user an over-optimistic number).
    """
    ts = _parse_ts(first_failure_at)
    if ts is None:
        return float(window_seconds)
    now = datetime.now(timezone.utc)
    elapsed = (now - ts).total_seconds()
    remaining = float(window_seconds) - elapsed
    return max(0.0, remaining)


def _bump(identifier: str, scope: str) -> Tuple[int, Optional[str]]:
    if not identifier:
        return 0, None
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO {_FAILURE_TABLE} (identifier, scope, failure_count)
            VALUES (?, ?, 1)
            ON CONFLICT(identifier, scope) DO UPDATE SET
                failure_count = failure_count + 1,
                last_failure_at = CURRENT_TIMESTAMP
            RETURNING failure_count, first_failure_at
            """,
            (identifier[:120], scope),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return 0, None
        return int(row[0]), row[1] if len(row) > 1 else None
    finally:
        conn.close()


def _reset(identifier: str, scope: str) -> None:
    if not identifier:
        return
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {_FAILURE_TABLE} WHERE identifier = ? AND scope = ?",
            (identifier[:120], scope),
        )
        conn.commit()
    finally:
        conn.close()


def _check(identifier: str, scope: str, max_failures: int, window_seconds: int) -> LockoutDecision:
    if not identifier:
        return LockoutDecision(True, 0.0, 0)
    # Evict failures older than the lockout window so the lockout
    # actually expires after the configured duration.
    _prune_window(window_seconds)
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute(
            f"SELECT failure_count, first_failure_at FROM {_FAILURE_TABLE} WHERE identifier = ? AND scope = ?",
            (identifier[:120], scope),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return LockoutDecision(True, 0.0, 0)
    count = int(row[0] or 0)
    if count >= max_failures:
        # Decay retry_after toward zero as the lockout window elapses,
        # measured from the first failure in the current burst.
        remaining = _remaining_seconds(row[1], window_seconds)
        return LockoutDecision(False, remaining, count)
    return LockoutDecision(True, 0.0, count)


def _prune_window(window_seconds: int) -> None:
    """Delete failure rows older than the lockout window.

    Cheap and runs opportunistically from :func:`record_failure`.
    """
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {_FAILURE_TABLE} WHERE last_failure_at < datetime('now', ?)",
            (f"-{int(window_seconds)} seconds",),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


_VALID_SCOPES = {"user", "ip", "key", "verify_email", "admin_pw"}


def check_allowed(
    identifier: str,
    *,
    scope: str = "user",
    max_failures: int = MAX_FAILURES,
    window_seconds: int = WINDOW_SECONDS,
) -> LockoutDecision:
    """Return the current lockout decision for ``identifier``."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of: {sorted(_VALID_SCOPES)}")
    return _check(identifier, scope, max_failures, window_seconds)


def record_failure(
    identifier: str,
    *,
    scope: str = "user",
    max_failures: int = MAX_FAILURES,
    window_seconds: int = WINDOW_SECONDS,
) -> LockoutDecision:
    """Increment the failure counter and return the new decision."""
    if scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of: {sorted(_VALID_SCOPES)}")
    if not identifier:
        return LockoutDecision(True, 0.0, 0)
    _prune_window(window_seconds)
    count, first_failure_at = _bump(identifier, scope)
    if count >= max_failures:
        remaining = _remaining_seconds(first_failure_at, window_seconds)
        return LockoutDecision(False, remaining, count)
    return LockoutDecision(True, 0.0, count)


def record_success(identifier: str, *, scope: str = "user") -> None:
    """Reset the failure counter on a successful login."""
    _reset(identifier, scope)


__all__ = [
    "LockoutDecision",
    "check_allowed",
    "record_failure",
    "record_success",
    "WINDOW_SECONDS",
    "MAX_FAILURES",
]
