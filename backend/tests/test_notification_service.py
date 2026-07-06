"""Unit tests for NotificationService CRUD operations.

Covers: notify, list_for_user, mark_read, mark_all_read, unread_count.
"""

from __future__ import annotations

import sqlite3
import time

from backend.services.notification_service import NotificationService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _insert_user(path: str, *, user_id: int = 1, username: str = "alice") -> None:
    c = _conn(path)
    c.execute(
        "INSERT INTO users (id, username, api_key) VALUES (?, ?, ?)",
        (user_id, username, f"ak_{username}"),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


class TestNotify:
    def test_creates_row_with_correct_fields(self, temp_db):
        _insert_user(temp_db, user_id=1)

        notif_id = NotificationService.notify(
            1,
            type="order_approved",
            title="Order approved",
            body="Your order #123 has been approved.",
            metadata={"order_id": 123, "credits": 50},
        )

        c = _conn(temp_db)
        row = c.execute("SELECT * FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        c.close()

        assert row is not None
        assert row["user_id"] == 1
        assert row["type"] == "order_approved"
        assert row["title"] == "Order approved"
        assert row["content"] == "Your order #123 has been approved."

    def test_returns_notification_id(self, temp_db):
        _insert_user(temp_db, user_id=1)
        notif_id = NotificationService.notify(
            1, type="low_balance", title="Low", body="Low balance"
        )
        assert isinstance(notif_id, int)
        assert notif_id > 0

    def test_is_read_zero_by_default(self, temp_db):
        _insert_user(temp_db, user_id=1)
        notif_id = NotificationService.notify(1, type="low_balance", title="Low", body="Low")

        c = _conn(temp_db)
        row = c.execute("SELECT is_read FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        c.close()
        assert row["is_read"] == 0

    def test_metadata_stored_as_json(self, temp_db):
        _insert_user(temp_db, user_id=1)
        meta = {"key": "value", "num": 42}
        NotificationService.notify(1, type="test", title="T", body="B", metadata=meta)

        # list_for_user should deserialize it
        items = NotificationService.list_for_user(1)
        assert len(items) == 1
        assert items[0]["metadata"] == meta


# ---------------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------------


class TestListForUser:
    def test_returns_only_own_notifications(self, temp_db):
        _insert_user(temp_db, user_id=1, username="alice")
        _insert_user(temp_db, user_id=2, username="bob")

        NotificationService.notify(1, type="a", title="A1", body="b1")
        NotificationService.notify(2, type="b", title="B1", body="b2")
        NotificationService.notify(1, type="c", title="A2", body="b3")

        alice_items = NotificationService.list_for_user(1)
        bob_items = NotificationService.list_for_user(2)

        assert len(alice_items) == 2
        assert all(n["user_id"] == 1 for n in alice_items)
        assert len(bob_items) == 1
        assert bob_items[0]["user_id"] == 2

    def test_limit_param(self, temp_db):
        _insert_user(temp_db, user_id=1)
        for i in range(10):
            NotificationService.notify(1, type="t", title=f"T{i}", body=f"B{i}")

        items = NotificationService.list_for_user(1, limit=3)
        assert len(items) == 3

    def test_unread_only_filter(self, temp_db):
        _insert_user(temp_db, user_id=1)
        id1 = NotificationService.notify(1, type="t", title="T1", body="B1")
        NotificationService.notify(1, type="t", title="T2", body="B2")

        NotificationService.mark_read(1, id1)

        unread = NotificationService.list_for_user(1, unread_only=True)
        assert len(unread) == 1
        assert unread[0]["title"] == "T2"

    def test_newest_first_ordering(self, temp_db):
        _insert_user(temp_db, user_id=1)
        NotificationService.notify(1, type="t", title="First", body="B1")
        time.sleep(0.05)
        NotificationService.notify(1, type="t", title="Second", body="B2")
        time.sleep(0.05)
        NotificationService.notify(1, type="t", title="Third", body="B3")

        items = NotificationService.list_for_user(1)
        assert len(items) == 3
        assert items[0]["title"] == "Third"
        assert items[1]["title"] == "Second"
        assert items[2]["title"] == "First"


# ---------------------------------------------------------------------------
# mark_read
# ---------------------------------------------------------------------------


class TestMarkRead:
    def test_marks_unread_as_read(self, temp_db):
        _insert_user(temp_db, user_id=1)
        notif_id = NotificationService.notify(1, type="t", title="T", body="B")

        result = NotificationService.mark_read(1, notif_id)
        assert result is True

        c = _conn(temp_db)
        row = c.execute("SELECT is_read FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        c.close()
        assert row["is_read"] == 1

    def test_already_read_returns_false(self, temp_db):
        _insert_user(temp_db, user_id=1)
        notif_id = NotificationService.notify(1, type="t", title="T", body="B")

        NotificationService.mark_read(1, notif_id)
        result = NotificationService.mark_read(1, notif_id)
        assert result is False

    def test_other_user_returns_false(self, temp_db):
        _insert_user(temp_db, user_id=1, username="alice")
        _insert_user(temp_db, user_id=2, username="bob")
        notif_id = NotificationService.notify(1, type="t", title="T", body="B")

        # Bob tries to mark Alice's notification as read
        result = NotificationService.mark_read(2, notif_id)
        assert result is False

        # Verify it's still unread
        c = _conn(temp_db)
        row = c.execute("SELECT is_read FROM notifications WHERE id = ?", (notif_id,)).fetchone()
        c.close()
        assert row["is_read"] == 0


# ---------------------------------------------------------------------------
# mark_all_read
# ---------------------------------------------------------------------------


class TestMarkAllRead:
    def test_marks_all_unread_as_read(self, temp_db):
        _insert_user(temp_db, user_id=1)
        for i in range(5):
            NotificationService.notify(1, type="t", title=f"T{i}", body=f"B{i}")

        NotificationService.mark_all_read(1)

        count = NotificationService.unread_count(1)
        assert count == 0

    def test_returns_updated_count(self, temp_db):
        _insert_user(temp_db, user_id=1)
        for i in range(4):
            NotificationService.notify(1, type="t", title=f"T{i}", body=f"B{i}")

        # Mark one as read first
        items = NotificationService.list_for_user(1)
        NotificationService.mark_read(1, items[0]["id"])

        # mark_all_read should only update the remaining 3
        count = NotificationService.mark_all_read(1)
        assert count == 3

    def test_does_not_affect_other_users(self, temp_db):
        _insert_user(temp_db, user_id=1, username="alice")
        _insert_user(temp_db, user_id=2, username="bob")

        NotificationService.notify(1, type="t", title="A1", body="B")
        NotificationService.notify(2, type="t", title="B1", body="B")
        NotificationService.notify(2, type="t", title="B2", body="B")

        NotificationService.mark_all_read(1)

        # Bob's notifications are still unread
        assert NotificationService.unread_count(2) == 2


# ---------------------------------------------------------------------------
# unread_count
# ---------------------------------------------------------------------------


class TestUnreadCount:
    def test_mixed_read_unread(self, temp_db):
        _insert_user(temp_db, user_id=1)
        id1 = NotificationService.notify(1, type="t", title="T1", body="B")
        NotificationService.notify(1, type="t", title="T2", body="B")
        NotificationService.notify(1, type="t", title="T3", body="B")

        NotificationService.mark_read(1, id1)

        assert NotificationService.unread_count(1) == 2

    def test_all_read_returns_zero(self, temp_db):
        _insert_user(temp_db, user_id=1)
        id1 = NotificationService.notify(1, type="t", title="T1", body="B")
        id2 = NotificationService.notify(1, type="t", title="T2", body="B")

        NotificationService.mark_read(1, id1)
        NotificationService.mark_read(1, id2)

        assert NotificationService.unread_count(1) == 0

    def test_no_notifications_returns_zero(self, temp_db):
        _insert_user(temp_db, user_id=1)
        assert NotificationService.unread_count(1) == 0
