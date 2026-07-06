"""Tests for the M8 quota warning notification path.

Covers:
  * maybe_warn_on_quota fires a notification when usage crosses 80% / 95%
  * cooldown suppresses repeat notifications within the same window
  * unlimited quotas (quota == 0) are never warned on
  * snapshot read failures are swallowed silently
  * the 95% warning is not suppressed by the 80% cooldown (separate type)
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from backend.services import quota_service
from backend.services.notification_service import NotificationService


def _seed_user(conn, *, quota_5h=0, quota_week=0, quota_month=0, used_5h=0, used_week=0, used_month=0):
    """Insert a minimal user + wallet + usage_rollups rows and return the user_id.

    ``get_quota_snapshot`` prefers ``usage_rollups`` over ``usage_logs``
    (falling back only when the rollup table is missing). We seed the
    rollup table directly so the snapshot sees the test's usage figures.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (username, email, api_key, is_active, quota_5h, quota_week, quota_month)
        VALUES (?, ?, ?, 1, ?, ?, ?)
        """,
        ("quota_test_user", "qt@example.com", "sk_test_qwerty", quota_5h, quota_week, quota_month),
    )
    user_id = cur.lastrowid
    cur.execute(
        "INSERT INTO wallets (user_id, balance, total_recharged, total_consumed) VALUES (?, 1000, 1000, 0)",
        (user_id,),
    )

    # Write a single rollup row at the current minute bucket. ``get_quota_snapshot``
    # filters by ``bucket_minute >= threshold_{5h,week,month}``; the current
    # minute is inside all three windows, so the same row is counted for
    # every window. We pick the max usage value to seed — the test asserts
    # on the percentage of whichever window it configured.
    now_minute = int(time.time() // 60)
    max_used = max(used_5h, used_week, used_month, 0)
    if max_used:
        cur.execute(
            """
            INSERT INTO usage_rollups
                (user_id, bucket_minute, request_count, prompt_tokens, completion_tokens, total_tokens)
            VALUES (?, ?, 1, 0, ?, ?)
            """,
            (user_id, now_minute, max_used, max_used),
        )
    conn.commit()
    return user_id


def _list_notifications(conn, user_id):
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, type, title, content FROM notifications WHERE user_id = ? ORDER BY id",
        (user_id,),
    )
    return cur.fetchall()


def test_quota_warning_80_percent_fires_notification(temp_db):
    """Crossing 80% of the 5h quota fires a quota_warning_5h_80 notification."""
    conn = temp_db
    user_id = _seed_user(conn, quota_5h=1000, used_5h=850)  # 85%

    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    assert len(notifs) == 1
    assert notifs[0]["type"] == "quota_warning_5h_80"
    assert "85%" in notifs[0]["title"]


def test_quota_warning_95_percent_fires_critical_notification(temp_db):
    """Crossing 95% fires the 95-level notification, not the 80-level one."""
    conn = temp_db
    user_id = _seed_user(conn, quota_5h=1000, used_5h=970)  # 97%

    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    assert len(notifs) == 1
    assert notifs[0]["type"] == "quota_warning_5h_95"


def test_unlimited_quota_never_warns(temp_db):
    """quota == 0 means unlimited — never fires a warning."""
    conn = temp_db
    # quota_5h=0 (unlimited) but huge usage — should NOT warn
    user_id = _seed_user(conn, quota_5h=0, used_5h=10_000_000)

    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    assert len(notifs) == 0


def test_cooldown_suppresses_repeat(temp_db):
    """A second call within the cooldown window is suppressed."""
    conn = temp_db
    user_id = _seed_user(conn, quota_5h=1000, used_5h=850)

    quota_service.maybe_warn_on_quota(user_id)
    quota_service.maybe_warn_on_quota(user_id)  # second call — suppressed

    notifs = _list_notifications(conn, user_id)
    assert len(notifs) == 1, "cooldown should suppress the second notification"


def test_95_warning_not_suppressed_by_80_cooldown(temp_db):
    """The 95% warning uses a different type than 80%, so the 80% cooldown
    doesn't suppress the 95% warning when usage climbs past 95%.
    """
    conn = temp_db
    user_id = _seed_user(conn, quota_5h=1000, used_5h=850)  # 85%

    # First call fires the 80% warning.
    quota_service.maybe_warn_on_quota(user_id)

    # Simulate usage climbing to 97% before the next call.
    cur = conn.cursor()
    cur.execute(
        "UPDATE usage_rollups SET completion_tokens = 970, total_tokens = 970 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()

    # Second call should fire the 95% warning even though the 80% cooldown
    # hasn't elapsed, because they use different notification types.
    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    types = {n["type"] for n in notifs}
    assert "quota_warning_5h_80" in types
    assert "quota_warning_5h_95" in types


def test_snapshot_failure_swallowed(temp_db, monkeypatch):
    """If get_quota_snapshot raises, maybe_warn_on_quota must NOT propagate
    the exception — it should swallow and return silently.
    """
    conn = temp_db
    user_id = _seed_user(conn, quota_5h=1000, used_5h=850)

    def _boom(_user_id):
        raise sqlite3.OperationalError("simulated DB failure")

    monkeypatch.setattr(quota_service, "get_quota_snapshot", _boom)

    # Must not raise.
    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    assert len(notifs) == 0


def test_multiple_windows_fire_separately(temp_db):
    """When usage crosses the threshold in multiple windows, each window
    fires its own notification (separate types, separate cooldowns).

    All three windows share the same rollup row (current-minute bucket),
    so we configure them with the same quota to make the percentages match.
    """
    conn = temp_db
    user_id = _seed_user(
        conn,
        quota_5h=1000, used_5h=850,
        quota_week=1000, used_week=850,
        quota_month=1000, used_month=850,
    )

    quota_service.maybe_warn_on_quota(user_id)

    notifs = _list_notifications(conn, user_id)
    types = {n["type"] for n in notifs}
    assert "quota_warning_5h_80" in types
    assert "quota_warning_week_80" in types
    assert "quota_warning_month_80" in types


# ---------------------------------------------------------------------------
# Fixture: a temp SQLite DB with the minimal schema needed by the test.
# Reuses the conftest.py parallel schema via the standard pytest fixture
# indirection — we just need to ensure the tables exist before seeding.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Provide a fresh SQLite DB with the relevant schema and patch
    ``backend.database.DATABASE_PATH`` to point at it.
    """
    db_path = str(tmp_path / "quota_warn.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    # Force the connection pool to reset so the new path takes effect.
    try:
        from backend.utils import db_pool
        if db_pool._POOL is not None:
            db_pool._POOL.close_all()
            db_pool._POOL = None
    except Exception:
        pass

    from backend.database import get_db_context
    from backend.tests.conftest import _SCHEMA  # type: ignore[attr-defined]

    # Build the schema explicitly. The conftest _SCHEMA string already
    # covers every table we need (users, wallets, usage_logs, notifications,
    # notification_cooldowns, token_reservations).
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    # notification_cooldowns is in _SCHEMA via migration 36 mirror, but
    # be defensive — the tests above rely on it existing.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_cooldowns (
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            last_sent_at TIMESTAMP NOT NULL,
            UNIQUE(user_id, type)
        )
        """
    )
    conn.commit()
    conn.close()

    # Reset the cooldown-initialised flag so the service re-creates the
    # table on first use (no-op when migration 36 mirror already did it).
    import backend.services.notification_service as ns
    ns._cooldowns_initialized = False

    with get_db_context() as conn:
        yield conn
