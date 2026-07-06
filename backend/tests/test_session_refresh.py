"""Tests for the sliding-window session refresh mechanism.

Covers:
  - Sessions in the second half of their TTL are refreshed (expires_at extended).
  - Sessions in the first half of their TTL are NOT refreshed.
  - Refresh respects the absolute timeout cap (never extends past it).
  - get_session returns the same dict shape regardless of refresh.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.database import get_db
from backend.session import (
    SESSION_TTL_SECONDS,
    _get_session_ttl,
    _maybe_refresh_session,
    get_session,
)


def _insert_session(
    conn: sqlite3.Connection,
    session_id: str,
    expires_at: datetime,
    absolute_expires_at: datetime | None = None,
    created_at: datetime | None = None,
    role: str = "user",
    csrf: str = "test-csrf-token",
) -> None:
    """Insert a minimal session row for testing."""
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, role, admin_id, user_id, username, email,
             csrf, expires_at, created_at, absolute_expires_at)
        VALUES (?, ?, NULL, 1, 'testuser', 'test@example.com', ?, ?, ?, ?)
        """,
        (
            session_id,
            role,
            csrf,
            expires_at.isoformat(),
            (created_at or datetime.now(timezone.utc)).isoformat(),
            absolute_expires_at.isoformat() if absolute_expires_at else None,
        ),
    )
    conn.commit()


def _read_expires_at(conn: sqlite3.Connection, session_id: str) -> datetime:
    """Read the current expires_at from the DB for a session."""
    cursor = conn.cursor()
    cursor.execute("SELECT expires_at FROM sessions WHERE session_id = ?", (session_id,))
    row = cursor.fetchone()
    assert row is not None, f"Session {session_id} not found"
    dt = datetime.fromisoformat(row["expires_at"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# get_session integration tests
# ---------------------------------------------------------------------------


def test_session_refreshed_when_in_second_half(temp_db):
    """A session with 20 minutes remaining (regular TTL=1h, threshold=30m)
    should be refreshed because 20m < 30m."""
    now = datetime.now(timezone.utc)
    session_id = "sess-refresh-20m"
    # Expires 20 minutes from now → in the second half of the 1h TTL.
    original_expires = now + timedelta(minutes=20)
    # Absolute cap: 8 hours from now (regular session).
    absolute_cap = now + timedelta(hours=8)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=original_expires,
        absolute_expires_at=absolute_cap,
        created_at=now - timedelta(minutes=40),  # created 40m ago
    )
    conn.close()

    # Call get_session — this should trigger a refresh.
    result = get_session(session_id)
    assert result is not None, "get_session returned None for valid session"

    # Verify expires_at was extended (should now be ~1h from now).
    conn = get_db()
    conn.row_factory = sqlite3.Row
    new_expires = _read_expires_at(conn, session_id)
    conn.close()

    # The new expiry should be roughly now + 1h (allow 5s tolerance for
    # the time elapsed between the get_session call and this check).
    expected_min = now + timedelta(minutes=55)
    expected_max = now + timedelta(seconds=SESSION_TTL_SECONDS + 5)
    assert expected_min <= new_expires <= expected_max, (
        f"Expected expires_at ~{expected_max}, got {new_expires}"
    )


def test_session_not_refreshed_when_in_first_half(temp_db):
    """A session with 40 minutes remaining (regular TTL=1h, threshold=30m)
    should NOT be refreshed because 40m > 30m."""
    now = datetime.now(timezone.utc)
    session_id = "sess-no-refresh-40m"
    # Expires 40 minutes from now → in the first half of the 1h TTL.
    original_expires = now + timedelta(minutes=40)
    absolute_cap = now + timedelta(hours=8)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=original_expires,
        absolute_expires_at=absolute_cap,
        created_at=now - timedelta(minutes=20),
    )
    conn.close()

    result = get_session(session_id)
    assert result is not None

    conn = get_db()
    conn.row_factory = sqlite3.Row
    new_expires = _read_expires_at(conn, session_id)
    conn.close()

    # expires_at should be unchanged (still the original 40m-from-now value).
    # Allow 2s tolerance for clock skew.
    diff = abs((new_expires - original_expires).total_seconds())
    assert diff < 2.0, (
        f"Session was unexpectedly refreshed: original={original_expires}, "
        f"new={new_expires} (diff={diff:.1f}s)"
    )


def test_session_refresh_capped_at_absolute_timeout(temp_db):
    """When now + TTL would exceed the absolute cap, expires_at is capped
    to absolute_expires_at and never extended past it."""
    now = datetime.now(timezone.utc)
    session_id = "sess-absolute-cap"
    # Expires 10 minutes from now → well inside the second half.
    original_expires = now + timedelta(minutes=10)
    # Absolute cap: only 15 minutes from now (contrived to test the cap).
    absolute_cap = now + timedelta(minutes=15)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=original_expires,
        absolute_expires_at=absolute_cap,
        created_at=now - timedelta(hours=7, minutes=45),
    )
    conn.close()

    result = get_session(session_id)
    assert result is not None

    conn = get_db()
    conn.row_factory = sqlite3.Row
    new_expires = _read_expires_at(conn, session_id)
    conn.close()

    # The new expires_at must not exceed the absolute cap (allow 2s tolerance).
    assert new_expires <= absolute_cap + timedelta(seconds=2), (
        f"expires_at {new_expires} exceeded absolute cap {absolute_cap}"
    )
    # But it should have been extended past the original 10-minute mark.
    assert new_expires > original_expires, (
        "expires_at was not extended at all despite being in the second half"
    )


def test_session_refresh_no_extension_when_at_cap(temp_db):
    """If expires_at is already at or past the absolute cap, no update
    should happen (new_expires <= expires_at after cap)."""
    now = datetime.now(timezone.utc)
    session_id = "sess-already-at-cap"
    # Expires 10 minutes from now but absolute cap is 5 minutes from now.
    # The absolute-timeout enforcement would normally delete this session,
    # so set absolute cap to 11 minutes (still in the future) and
    # expires_at to 10 minutes. Refresh would try now + 1h → capped to
    # 11m, which is > 10m, so it WILL extend. To truly test "no extension",
    # set expires_at equal to the absolute cap.
    original_expires = now + timedelta(minutes=11)
    absolute_cap = now + timedelta(minutes=11)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=original_expires,
        absolute_expires_at=absolute_cap,
        created_at=now - timedelta(hours=7, minutes=49),
    )
    conn.close()

    result = get_session(session_id)
    assert result is not None

    conn = get_db()
    conn.row_factory = sqlite3.Row
    new_expires = _read_expires_at(conn, session_id)
    conn.close()

    # No extension: capped new_expires (now+1h capped to absolute=now+11m)
    # is roughly equal to the original (now+11m), so the
    # new_expires <= expires_at guard prevents the write.
    diff = abs((new_expires - original_expires).total_seconds())
    assert diff < 2.0, (
        f"Session should NOT have been refreshed when at absolute cap, "
        f"but expires_at changed: original={original_expires}, new={new_expires}"
    )


def test_get_session_returns_same_shape_after_refresh(temp_db):
    """The returned dict must have exactly the same keys regardless of
    whether a refresh happened. No internal flags like _refreshed leak."""
    now = datetime.now(timezone.utc)
    session_id = "sess-shape-check"
    original_expires = now + timedelta(minutes=20)  # triggers refresh
    absolute_cap = now + timedelta(hours=8)

    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=original_expires,
        absolute_expires_at=absolute_cap,
        created_at=now - timedelta(minutes=40),
    )
    conn.close()

    result = get_session(session_id)
    assert result is not None

    expected_keys = {"role", "admin_id", "user_id", "username", "email", "csrf"}
    assert set(result.keys()) == expected_keys, (
        f"Unexpected keys in session dict: {set(result.keys()) - expected_keys}"
    )
    assert "_refreshed" not in result
    assert "_ttl_seconds" not in result


# ---------------------------------------------------------------------------
# _get_session_ttl unit tests
# ---------------------------------------------------------------------------


def test_get_session_ttl_regular():
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    # 8h absolute → regular session
    absolute = (now + timedelta(hours=8)).isoformat()
    assert _get_session_ttl(absolute, created) == SESSION_TTL_SECONDS


def test_get_session_ttl_remember_me():
    now = datetime.now(timezone.utc)
    created = now.isoformat()
    # 30 days absolute → remember-me session
    absolute = (now + timedelta(days=30)).isoformat()
    from backend.session import REMEMBER_ME_TTL_SECONDS

    assert _get_session_ttl(absolute, created) == REMEMBER_ME_TTL_SECONDS


def test_get_session_ttl_none_absolute():
    # No absolute_expires_at → defaults to regular TTL
    assert _get_session_ttl(None, None) == SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# _maybe_refresh_session unit tests (direct call, no get_session wrapper)
# ---------------------------------------------------------------------------


def test_maybe_refresh_returns_false_when_no_session(temp_db):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        result = _maybe_refresh_session(conn, "nonexistent-session", 3600, None)
        assert result is False
    finally:
        conn.close()


def test_maybe_refresh_returns_false_in_first_half(temp_db):
    now = datetime.now(timezone.utc)
    session_id = "unit-first-half"
    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=now + timedelta(minutes=40),  # 40m > 30m threshold
        absolute_expires_at=now + timedelta(hours=8),
    )
    try:
        result = _maybe_refresh_session(conn, session_id, 3600, None)
        assert result is False, "Should not refresh when in first half of TTL"
    finally:
        conn.close()


def test_maybe_refresh_returns_true_in_second_half(temp_db):
    now = datetime.now(timezone.utc)
    session_id = "unit-second-half"
    conn = get_db()
    conn.row_factory = sqlite3.Row
    _insert_session(
        conn,
        session_id,
        expires_at=now + timedelta(minutes=20),  # 20m < 30m threshold
        absolute_expires_at=now + timedelta(hours=8),
    )
    try:
        result = _maybe_refresh_session(conn, session_id, 3600, None)
        assert result is True, "Should refresh when in second half of TTL"
    finally:
        conn.close()
