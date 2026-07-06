"""Notification service: create, list, and manage user notifications.

All public methods are static for consistency with the other service
modules in this package.  Write operations use ``BEGIN IMMEDIATE`` to
prevent concurrent-write races; read operations use the standard
``get_db_context()`` helper.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_db_context

logger = logging.getLogger(__name__)

_cooldowns_initialized = False


def _ensure_cooldowns_table() -> None:
    """运行时兜底:正式迁移(migration 36)未跑或被回滚时仍保证表存在。

    migration 36 已将 ``notification_cooldowns`` 纳入 schema_migrations,
    正常部署下本函数是 no-op。保留它是为了在迁移未跑的边缘场景(如测试
    切换 DB 路径)下仍能工作。失败时记 WARNING —— 冷却失效会让通知
    重复发送,但不应阻断主流程。
    """
    global _cooldowns_initialized
    if _cooldowns_initialized:
        return
    _cooldowns_initialized = True
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_cooldowns (
                    user_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    last_sent_at TIMESTAMP NOT NULL,
                    UNIQUE(user_id, type)
                )
                """
            )
    except Exception:
        logger.warning(
            "failed to create notification_cooldowns table; "
            "cooldown checks will be skipped (should_notify will return True)",
            exc_info=True,
        )


class NotificationService:
    """Thin CRUD layer over the ``notifications`` table."""

    TYPES = {
        "order_approved": "订单已通过",
        "order_rejected": "订单被拒绝",
        "order_refunded": "订单已退款",
        "low_balance": "余额不足",
        "subscription_expiring": "订阅即将到期",
        "subscription_expired": "订阅已到期",
        "subscription_renewed": "订阅已续期",
        "auto_recharge_triggered": "自动充值已触发",
        "admin_announcement": "管理员公告",
        # M8: 配额预警。按 (window, threshold) 二级分级 —— 不同阈值用不同
        # type 名，避免 notify_with_cooldown 的 (user_id, type) 去重粒度
        # 互相抑制（95% 告警不应被 80% 告警的冷却压制）。
        "quota_warning_5h_80": "5小时配额已用 80%",
        "quota_warning_5h_95": "5小时配额已用 95%",
        "quota_warning_week_80": "周配额已用 80%",
        "quota_warning_week_95": "周配额已用 95%",
        "quota_warning_month_80": "月配额已用 80%",
        "quota_warning_month_95": "月配额已用 95%",
    }

    @staticmethod
    def notify(
        user_id: int,
        *,
        type: str,
        title: str,
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Insert a notification row.  Returns the notification id."""
        meta_json: Optional[str] = None
        if metadata:
            try:
                meta_json = json.dumps(metadata, ensure_ascii=False)
            except Exception:
                meta_json = None

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    """
                    INSERT INTO notifications
                        (user_id, type, title, content, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (int(user_id), type, title, body, meta_json),
                )
                notif_id = int(cursor.lastrowid)
                cursor.execute("COMMIT")
                return notif_id
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    @staticmethod
    def list_for_user(
        user_id: int,
        *,
        limit: int = 50,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return notifications for the user, newest first."""
        sql = """
            SELECT id, user_id, type, title, content, is_read, metadata, created_at
            FROM notifications
            WHERE user_id = ?
        """
        params: list = [int(user_id)]
        if unread_only:
            sql += " AND is_read = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            meta = None
            raw_meta = row["metadata"]
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                except Exception:
                    meta = None
            results.append(
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "type": row["type"],
                    "title": row["title"],
                    "body": row["content"],
                    "is_read": bool(row["is_read"]),
                    "metadata": meta,
                    "created_at": row["created_at"],
                }
            )
        return results

    @staticmethod
    def mark_read(user_id: int, notification_id: int) -> bool:
        """Mark a notification as read.  Returns True if it existed and was unread."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    """
                    UPDATE notifications
                    SET is_read = 1
                    WHERE id = ? AND user_id = ? AND is_read = 0
                    """,
                    (int(notification_id), int(user_id)),
                )
                updated = cursor.rowcount > 0
                cursor.execute("COMMIT")
                return updated
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    @staticmethod
    def mark_all_read(user_id: int) -> int:
        """Mark all as read.  Returns count updated."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    """
                    UPDATE notifications
                    SET is_read = 1
                    WHERE user_id = ? AND is_read = 0
                    """,
                    (int(user_id),),
                )
                count = cursor.rowcount
                cursor.execute("COMMIT")
                return count
            except Exception:
                cursor.execute("ROLLBACK")
                raise

    @staticmethod
    def unread_count(user_id: int) -> int:
        """Return unread count for the badge."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM notifications
                WHERE user_id = ? AND is_read = 0
                """,
                (int(user_id),),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def should_notify(user_id: int, type: str, cooldown_hours: int = 24) -> bool:
        """Check if enough time has passed since the last notification of this type."""
        _ensure_cooldowns_table()
        try:
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT last_sent_at FROM notification_cooldowns
                    WHERE user_id = ? AND type = ?
                    """,
                    (int(user_id), type),
                )
                row = cursor.fetchone()
                if not row:
                    return True
                last_sent = datetime.fromisoformat(str(row["last_sent_at"]).replace("Z", "+00:00"))
                if last_sent.tzinfo is None:
                    last_sent = last_sent.replace(tzinfo=timezone.utc)
                return datetime.now(timezone.utc) - last_sent > timedelta(hours=cooldown_hours)
        except Exception:
            logger.exception("failed to check notification cooldown for user %s type %s", user_id, type)
            return True

    @staticmethod
    def notify_with_cooldown(
        user_id: int,
        *,
        type: str,
        title: str,
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
        cooldown_hours: int = 24,
    ) -> Optional[int]:
        """Send a notification only if the cooldown period has elapsed.

        Returns the notification id if sent, None if suppressed by cooldown.
        """
        if not NotificationService.should_notify(user_id, type, cooldown_hours):
            return None

        notif_id = NotificationService.notify(
            user_id, type=type, title=title, body=body, metadata=metadata
        )

        _ensure_cooldowns_table()
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                try:
                    cursor.execute(
                        """
                        INSERT INTO notification_cooldowns (user_id, type, last_sent_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(user_id, type) DO UPDATE SET last_sent_at = ?
                        """,
                        (int(user_id), type, now_str, now_str),
                    )
                    cursor.execute("COMMIT")
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
        except Exception:
            logger.exception("failed to update notification cooldown for user %s type %s", user_id, type)

        return notif_id
