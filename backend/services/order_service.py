"""Order, promo code, and redeem code service layer.

All public functions raise :class:`ValueError` on validation / business
errors so the route layer can translate them to HTTP 4xx responses.

Side effects are wrapped in transactions (``BEGIN IMMEDIATE``) wherever
money moves to guarantee atomicity.
"""

from __future__ import annotations

import json
import logging
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.database import get_db_context, grant_credits

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_subscription_note(note_raw: str) -> Optional[Dict[str, Any]]:
    """从 ``orders.note`` 中解析订阅元信息。

    返回 ``{"plan_id": ..., "auto_renew": ..., "subscription_id": ...}``
    字典；如果 note 不是有效 JSON 或不含 ``plan_id`` 字段则返回 ``None``。

    M3: 用 ``json.loads`` 替代 regex，避免误匹配嵌入字符串中的
    数字。同时支持 ``COALESCE(note, '') || ...`` 拼接场景——
    按 ``}{`` 切片后逐段尝试解析。
    """
    if not note_raw or not isinstance(note_raw, str):
        return None

    # 拼接场景：note 可能由多段 JSON 串接而成，按 ``}{`` 切分后
    # 逐段补全大括号尝试解析。
    for segment in note_raw.split("}{"):
        candidate = segment if segment.startswith("{") else "{" + segment
        if not candidate.endswith("}"):
            candidate = candidate + "}"
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "plan_id" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue

    # 整段直接解析（最常见的路径）。
    try:
        parsed = json.loads(note_raw)
        if isinstance(parsed, dict) and "plan_id" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        return None

    return None


def _maybe_cancel_subscription_on_refund(order_id: int, user_id: int, admin_id: Optional[int]) -> None:
    """If ``order_id`` activated a subscription, cancel it on refund.

    Reads the ``plan_id`` / ``subscription_id`` that
    :func:`_handle_subscription_activation` (via ``SubscriptionService.upgrade``)
    embedded in ``orders.note`` and undoes the activation:

    * subscription status -> ``cancelled``
    * ``auto_renew`` cleared
    * ``users.plan_id`` cleared so :func:`get_user_plan` falls back to
      the free-plan defaults.

    Best-effort: any failure is logged and swallowed so the surrounding
    ``refund_order`` transaction is not disturbed.
    """
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT note FROM orders WHERE id = ?", (int(order_id),))
            note_row = cursor.fetchone()
            if not note_row:
                return
            note_raw = note_row[0] if isinstance(note_row, tuple) else note_row.get("note")
            if not note_raw or not isinstance(note_raw, str):
                return

            # M3: 用 json.loads 替代 regex，避免误匹配嵌入字符串。
            plan_info = _parse_subscription_note(note_raw)
            if not plan_info:
                return

            plan_id_raw = plan_info.get("plan_id")
            if plan_id_raw is None:
                return
            plan_id = int(plan_id_raw)
            sub_id_raw = plan_info.get("subscription_id")
            sub_id: Optional[int] = int(sub_id_raw) if sub_id_raw is not None else None

            target_sub: Optional[int] = None
            if sub_id:
                cursor.execute(
                    "SELECT id FROM subscriptions WHERE id = ? AND user_id = ?",
                    (sub_id, user_id),
                )
                row = cursor.fetchone()
                if row:
                    target_sub = int(row[0])

            if target_sub is None:
                cursor.execute(
                    """
                    SELECT id FROM subscriptions
                    WHERE user_id = ? AND plan_id = ?
                      AND status IN ('active', 'pending_payment')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id, plan_id),
                )
                row = cursor.fetchone()
                if row:
                    target_sub = int(row[0])

            if target_sub is None:
                return

            now_str = _now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                UPDATE subscriptions
                SET status = 'cancelled',
                    auto_renew = 0,
                    cancelled_at = ?
                WHERE id = ?
                """,
                (now_str, target_sub),
            )
            cursor.execute(
                "UPDATE users SET plan_id = NULL WHERE id = ? AND plan_id = ?",
                (user_id, plan_id),
            )

            try:
                from backend.services.audit import log_action

                log_action(
                    actor_id=admin_id,
                    actor_type="admin" if admin_id else "system",
                    action="subscription_cancelled_on_refund",
                    target_type="subscription",
                    target_id=target_sub,
                    details={
                        "order_id": int(order_id),
                        "user_id": user_id,
                        "plan_id": plan_id,
                    },
                )
            except Exception:
                logger.debug("audit log for refund subscription cancel failed", exc_info=True)
    except Exception:
        logger.exception(
            "cancel-subscription-on-refund failed order=%s user=%s", order_id, user_id
        )
        # P1.6: 退款撤销订阅失败会导致订阅状态与退款金额不一致
        # （用户已退钱但订阅仍 active），属于资金安全事件，必须告警。
        try:
            from backend.services.alert_service import AlertService

            AlertService.send_alert_sync(
                level="CRITICAL",
                message="refund subscription cancel failed",
                metadata={
                    "order_id": int(order_id),
                    "user_id": int(user_id),
                    "admin_id": admin_id,
                },
            )
        except Exception:
            logger.debug("AlertService.send_alert_sync failed", exc_info=True)


def _gen_order_no() -> str:
    """Generate ``ORD`` + 14-digit timestamp + 8-char hex suffix (4B possibilities)."""
    ts = _now().strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(4).upper()
    return f"ORD{ts}{suffix}"


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    try:
        return {k: row[k] for k in row.keys()}
    except (AttributeError, IndexError):
        return {}


def _get_user_email_info(user_id: int) -> Optional[Dict[str, str]]:
    """Return ``{"email": ..., "username": ...}`` if the user has an email on file."""
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT email, username FROM users WHERE id = ?",
                (int(user_id),),
            )
            row = cursor.fetchone()
            if row and row["email"]:
                return {"email": row["email"], "username": row["username"]}
    except Exception:
        logger.debug("user email lookup failed for user_id=%s", user_id, exc_info=True)
    return None


def _fire_email(coro) -> None:
    """Schedule an async email coroutine on the running event loop.

    Uses ``asyncio.create_task`` when called from within an async context
    (e.g. a FastAPI request handler).  Falls back to ``asyncio.run`` in a
    dedicated thread when no loop is available so the caller never blocks.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running event loop — fire in a background thread so the
        # synchronous caller is never blocked.
        import threading

        def _run():
            asyncio.run(coro)

        threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

# 1 元 = 100 credits
CREDITS_PER_YUAN = 100.0


def create_order(
    user_id: int,
    amount: float,
    payment_method: str = "admin_grant",
    promo_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a pending order, applying any promo code.

    Promo types:
      - ``discount_percent``  – percentage off the order (0-100)
      - ``discount_fixed``    – flat credits off the order
      - ``bonus_credits``     – extra credits on top of the base

    Promo resolution happens *inside* the same ``BEGIN IMMEDIATE``
    transaction that inserts the order, so concurrent requests cannot
    exceed ``max_uses`` or ``per_user_limit``.

    Returns the order row as a dict.
    """
    if amount is None or float(amount) <= 0:
        raise ValueError("充值金额必须大于 0")

    base_credits = round(float(amount) * CREDITS_PER_YUAN, 4)
    order_no = _gen_order_no()

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            bonus_credits = 0.0
            discount_credits = 0.0
            applied_promo: Optional[str] = None
            promo_id: Optional[int] = None

            # --- P1.5: auto_recharge TOCTOU dedup guard -----------------
            # ``billing_service._maybe_trigger_auto_recharge`` performs a
            # best-effort pending-order check *before* calling us, but
            # that check is unlocked. Two concurrent debits can both pass
            # it and then both INSERT, flooding the user with pending
            # auto_recharge orders. Re-check here under the RESERVED
            # lock: if the same user already has a pending auto_recharge
            # order created within the last hour, refuse the new one.
            if (payment_method or "") == "auto_recharge":
                one_hour_ago = (_now() - timedelta(hours=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM orders
                    WHERE user_id = ?
                      AND payment_method = 'auto_recharge'
                      AND status = 'pending'
                      AND created_at >= ?
                    """,
                    (int(user_id), one_hour_ago),
                )
                existing = int(cursor.fetchone()[0] or 0)
                if existing > 0:
                    raise ValueError(
                        "auto_recharge 订单已存在（1 小时内），请先完成或取消该订单"
                    )

            # --- Resolve promo INSIDE this transaction (Fix 5) -----------
            if promo_code:
                code = (promo_code or "").strip()
                if code:
                    cursor.execute(
                        """
                        SELECT * FROM promo_codes
                        WHERE code = ? AND is_active = 1
                        """,
                        (code,),
                    )
                    promo = cursor.fetchone()
                    if promo:
                        # Expiry checks
                        if promo["max_uses"] and promo["used_count"] >= promo["max_uses"]:
                            promo = None
                        elif promo["valid_from"] and _parse_dt(promo["valid_from"]) > _now():
                            promo = None
                        elif promo["valid_until"] and _parse_dt(promo["valid_until"]) < _now():
                            promo = None
                        else:
                            # Per-user limit check (under the RESERVED lock)
                            if promo["per_user_limit"] and promo["per_user_limit"] > 0:
                                cursor.execute(
                                    """
                                    SELECT COUNT(*) FROM promo_code_usage
                                    WHERE promo_code_id = ? AND user_id = ?
                                    """,
                                    (promo["id"], int(user_id)),
                                )
                                used = int(cursor.fetchone()[0] or 0)
                                if used >= promo["per_user_limit"]:
                                    promo = None

                    if not promo:
                        raise ValueError(f"优惠码 {promo_code} 无效或已失效")

                    applied_promo = promo["code"]
                    promo_id = promo["id"]
                    ptype = promo["type"]
                    value = float(promo["value"] or 0)
                    if ptype == "discount_percent":
                        discount_credits = round(base_credits * (value / 100.0), 4)
                    elif ptype == "discount_fixed":
                        discount_credits = round(value, 4)
                    elif ptype == "bonus_credits":
                        bonus_credits = round(value, 4)
                    else:
                        raise ValueError(f"不支持的优惠码类型: {ptype}")

                    # Atomically increment usage under the lock
                    cursor.execute(
                        """
                        UPDATE promo_codes SET used_count = used_count + 1
                        WHERE id = ?
                        """,
                        (promo_id,),
                    )

            if discount_credits >= base_credits:
                raise ValueError("优惠金额不能超过订单金额")

            final_credits = base_credits - discount_credits

            # Fix 7: ``plan_subscription`` orders are paid from wallet
            # balance (the subscribe route debits the wallet directly
            # via SubscriptionService.upgrade). Persist the provider so
            # /billing/orders/{no}/pay can detect "already provisioned"
            # orders and skip the checkout flow. ``payment_session_id``
            # stays NULL — balance payments don't need a session.
            payment_provider_for_insert: Optional[str] = None
            if (payment_method or "") == "plan_subscription":
                payment_provider_for_insert = "balance"

            cursor.execute(
                """
                INSERT INTO orders
                    (order_no, user_id, amount, credits, bonus_credits,
                     payment_method, payment_provider, status, promo_code, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
                (
                    order_no,
                    int(user_id),
                    float(amount),
                    final_credits,
                    bonus_credits,
                    payment_method or "admin_grant",
                    payment_provider_for_insert,
                    applied_promo,
                    json.dumps({"promo_value": discount_credits}) if applied_promo else None,
                ),
            )
            order_id = int(cursor.lastrowid)

            if promo_id is not None:
                cursor.execute(
                    """
                    INSERT INTO promo_code_usage
                        (promo_code_id, user_id, order_id, credits_granted)
                    VALUES (?, ?, ?, ?)
                """,
                    (promo_id, int(user_id), order_id, bonus_credits or discount_credits),
                )

            # Re-read the row to surface a consistent snapshot.
            cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            cursor.execute("COMMIT")
            return _row_to_dict(row)
        except Exception:
            cursor.execute("ROLLBACK")
            raise


def _resolve_promo(code: str, user_id: int) -> Optional[Dict[str, Any]]:
    code = (code or "").strip()
    if not code:
        return None
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM promo_codes
            WHERE code = ? AND is_active = 1
        """,
            (code,),
        )
        promo = cursor.fetchone()
        if not promo:
            return None
        if promo["max_uses"] and promo["used_count"] >= promo["max_uses"]:
            return None
        if promo["valid_from"] and _parse_dt(promo["valid_from"]) > _now():
            return None
        if promo["valid_until"] and _parse_dt(promo["valid_until"]) < _now():
            return None
        if promo["per_user_limit"] and promo["per_user_limit"] > 0:
            cursor.execute(
                """
                SELECT COUNT(*) FROM promo_code_usage
                WHERE promo_code_id = ? AND user_id = ?
            """,
                (promo["id"], int(user_id)),
            )
            used = int(cursor.fetchone()[0] or 0)
            if used >= promo["per_user_limit"]:
                return None
        return _row_to_dict(promo)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        # SQLite default format: "YYYY-MM-DD HH:MM:SS"
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def approve_order(order_id: int, admin_id: int) -> bool:
    """Mark an order paid and credit the wallet atomically.

    Both the order status update and wallet credit happen in a single
    ``BEGIN IMMEDIATE`` transaction to prevent inconsistency. Returns
    True on success, False when the order is missing or already in a
    terminal state (Fix 7: idempotency guard).

    Fix 1: ``credits`` and ``bonus_credits`` are written as two separate
    ``wallet_transactions`` rows (``type='recharge'`` for the base,
    ``type='bonus'`` for the bonus) so they can be tracked and reversed
    independently. When ``bonus_credits == 0`` only the recharge row is
    written (legacy behaviour preserved).

    Fix 3: ``pending_review`` orders can be re-evaluated and approved
    (e.g. after an admin reviews a partial-payment dispute). The
    idempotency guard now accepts both ``pending`` and ``pending_review``.
    """
    _notif_user_id: Optional[int] = None
    _notif_order_no: Optional[str] = None
    _notif_amount: float = 0.0
    _notif_credits: float = 0.0
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT id, user_id, amount, credits, bonus_credits, status, order_no FROM orders WHERE id = ?",
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK")
                return False
            # Fix 3: pending_review orders can be re-evaluated and approved.
            if row[5] not in ("pending", "pending_review"):
                cursor.execute("ROLLBACK")
                return False

            user_id = int(row[1])
            _notif_amount = float(row[2] or 0)
            base_credits = float(row[3] or 0)
            bonus_credits = float(row[4] or 0)
            total_credits = base_credits + bonus_credits
            _notif_credits = total_credits
            order_no = str(row[6] or "")

            # Update order status
            cursor.execute(
                """
                UPDATE orders
                SET status = 'paid', paid_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE id = ?
            """,
                (int(admin_id) if admin_id else None, int(order_id)),
            )

            # Credit wallet atomically within the same transaction via
            # grant_credits (single entry point — stamps expires_at,
            # updates total_recharged, writes the ledger row).
            if base_credits > 0:
                grant_credits(
                    user_id,
                    base_credits,
                    "recharge",
                    related_type="order",
                    related_id=int(order_id),
                    note=f"order #{order_id} approved",
                    conn=conn,
                )
            if bonus_credits > 0:
                grant_credits(
                    user_id,
                    bonus_credits,
                    "bonus",
                    related_type="order",
                    related_id=int(order_id),
                    note=f"order #{order_id} bonus",
                    conn=conn,
                )

            _handle_subscription_activation(cursor, int(order_id), user_id)

            cursor.execute("COMMIT")
            _notif_user_id = user_id
            _notif_order_no = order_no
            return True
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    # Emit notification outside the transaction so a notification
    # failure never rolls back a successful order approval.
    if _notif_user_id is not None:
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                _notif_user_id,
                type="order_approved",
                title="订单已通过",
                body=f"订单 {_notif_order_no} 已通过, credits 已入账",
                metadata={"order_no": _notif_order_no},
            )
        except Exception:
            logger.exception("notification emit failed for approve_order %s", order_id)

        # Send email notification if user has email on file
        try:
            user_info = _get_user_email_info(_notif_user_id)
            if user_info:
                from backend.services.email_service import EmailService

                _fire_email(
                    EmailService.send_order_approved(
                        email=user_info["email"],
                        username=user_info["username"],
                        order_no=_notif_order_no or "",
                        amount=_notif_amount,
                        credits=_notif_credits,
                    )
                )
        except Exception:
            logger.exception("email send failed for approve_order %s", order_id)


def approve_mismatched_order(order_id: int, admin_id: int, paid_amount: float) -> bool:
    """Approve an order with amount mismatch by adjusting credits proportionally.

    adjusted_credits = order.credits * (paid_amount / expected_amount)
    """
    _notif_user_id: Optional[int] = None
    _notif_order_no: Optional[str] = None
    _notif_amount: float = 0.0
    _notif_credits: float = 0.0
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT id, user_id, amount, credits, bonus_credits, status, order_no FROM orders WHERE id = ?",
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK")
                return False
            if row[5] not in ("pending", "pending_review"):
                cursor.execute("ROLLBACK")
                return False

            user_id = int(row[1])
            expected_amount = float(row[2] or 0)
            original_credits = float(row[3] or 0)
            original_bonus = float(row[4] or 0)
            order_no = str(row[6] or "")

            if expected_amount <= 0:
                cursor.execute("ROLLBACK")
                return False

            ratio = paid_amount / expected_amount
            adjusted_credits = round(original_credits * ratio, 4)
            adjusted_bonus = round(original_bonus * ratio, 4)

            cursor.execute(
                """
                UPDATE orders
                SET credits = ?, bonus_credits = ?,
                    status = 'paid', paid_at = CURRENT_TIMESTAMP,
                    approved_by = ?
                WHERE id = ?
            """,
                (adjusted_credits, adjusted_bonus, int(admin_id) if admin_id else None, int(order_id)),
            )

            total_credits = adjusted_credits + adjusted_bonus
            _notif_credits = total_credits
            _notif_amount = paid_amount

            if adjusted_credits > 0:
                grant_credits(
                    user_id,
                    adjusted_credits,
                    "recharge",
                    related_type="order",
                    related_id=int(order_id),
                    note=f"order #{order_id} approved (mismatch adjusted: paid {paid_amount}/{expected_amount})",
                    conn=conn,
                )
            if adjusted_bonus > 0:
                grant_credits(
                    user_id,
                    adjusted_bonus,
                    "bonus",
                    related_type="order",
                    related_id=int(order_id),
                    note=f"order #{order_id} bonus (mismatch adjusted: paid {paid_amount}/{expected_amount})",
                    conn=conn,
                )

            _handle_subscription_activation(cursor, int(order_id), user_id)

            cursor.execute("COMMIT")
            _notif_user_id = user_id
            _notif_order_no = order_no
            return True
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    if _notif_user_id is not None:
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                _notif_user_id,
                type="order_approved",
                title="订单已通过(金额调整)",
                body=f"订单 {_notif_order_no} 已通过, credits 按比例调整为 {_notif_credits:.2f}",
                metadata={"order_no": _notif_order_no, "adjusted": True},
            )
        except Exception:
            logger.exception("notification emit failed for approve_mismatched_order %s", order_id)


def _handle_subscription_activation(
    cursor, order_id: int, user_id: int
) -> None:
    """Parse order.note for plan_id and activate/create subscription if present.

    Must be called inside an existing BEGIN IMMEDIATE transaction.

    H2: 订阅激活后发放 plan.monthly_credits，走 grant_credits 同款逻辑
    （显式 stamp expires_at / expiry_debited），让 CREDITS_EXPIRE_DAYS
    对在线支付订阅路径同样生效。
    """
    cursor.execute("SELECT note FROM orders WHERE id = ?", (int(order_id),))
    note_row = cursor.fetchone()
    if not note_row:
        return
    note_raw = note_row[0] if isinstance(note_row, tuple) else note_row.get("note")
    if not note_raw:
        return

    plan_info = _parse_subscription_note(note_raw)
    if not plan_info or "plan_id" not in plan_info:
        return

    plan_id = int(plan_info["plan_id"])
    auto_renew = 1 if plan_info.get("auto_renew", True) else 0

    cursor.execute(
        "SELECT id, monthly_credits, code FROM plans WHERE id = ? AND is_active = 1",
        (plan_id,),
    )
    plan_row = cursor.fetchone()
    if not plan_row:
        return
    if isinstance(plan_row, tuple):
        monthly_credits = float(plan_row[1] or 0)
        plan_code = plan_row[2] or ""
    else:
        monthly_credits = float(plan_row["monthly_credits"] or 0)
        plan_code = plan_row["code"] or ""

    now = _now()
    started_str = now.strftime("%Y-%m-%d %H:%M:%S")
    expires_str = (now + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        SELECT id FROM subscriptions
        WHERE user_id = ? AND status IN ('active', 'pending_payment') AND plan_id = ?
        ORDER BY id DESC LIMIT 1
    """,
        (user_id, plan_id),
    )
    existing = cursor.fetchone()

    if existing:
        sub_id = existing[0] if isinstance(existing, tuple) else existing["id"]

        # Decide between extending (active + unexpired) and resetting
        # (cancelled / expired / pending_payment). When the user pays
        # again for the same plan while their subscription is still
        # live, they expect the paid period to *extend* the existing
        # one — resetting expires_at to now+30d would silently discard
        # the remaining days they already paid for.
        cursor.execute(
            "SELECT status, expires_at FROM subscriptions WHERE id = ?",
            (sub_id,),
        )
        sub_row = cursor.fetchone()
        sub_status: Optional[str] = None
        sub_expires_raw: Optional[str] = None
        if sub_row:
            if isinstance(sub_row, tuple):
                sub_status = sub_row[0]
                sub_expires_raw = sub_row[1]
            else:
                sub_status = sub_row["status"]
                sub_expires_raw = sub_row["expires_at"]

        extend = False
        old_expires_dt: Optional[datetime] = None
        if sub_status == "active" and sub_expires_raw:
            try:
                expires_to_parse = sub_expires_raw
                if expires_to_parse.endswith("Z"):
                    expires_to_parse = expires_to_parse[:-1]
                old_expires_dt = datetime.fromisoformat(expires_to_parse)
                if old_expires_dt.tzinfo is None:
                    old_expires_dt = old_expires_dt.replace(tzinfo=timezone.utc)
                if old_expires_dt > now:
                    extend = True
            except (ValueError, TypeError):
                extend = False

        if extend:
            # Extend: started_at unchanged, credits_used_this_period
            # unchanged, expires_at += 30 days. The user already paid
            # for the existing period — discarding the remaining days
            # would be a silent double-charge.
            new_expires_dt = old_expires_dt + timedelta(days=30)
            new_expires_str = new_expires_dt.strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                UPDATE subscriptions
                SET status = 'active', expires_at = ?, auto_renew = ?
                WHERE id = ?
                """,
                (new_expires_str, auto_renew, sub_id),
            )
            # Record the extension in orders.note. _parse_subscription_note
            # splits concatenated JSON by ``}{`` so appending a second
            # JSON object keeps the original plan_id segment readable.
            extension_payload = json.dumps({
                "subscription_extension": {
                    "days_added": 30,
                    "previous_expires_at": sub_expires_raw,
                }
            })
            cursor.execute(
                "UPDATE orders SET note = COALESCE(note, '') || ? WHERE id = ?",
                (extension_payload, int(order_id)),
            )
            # Audit-log the extension so admins can tell apart "new
            # sub" from "extension" in the order history.
            try:
                cursor.execute(
                    """INSERT INTO audit_logs
                       (actor_type, action, target_type, target_id, metadata)
                       VALUES ('system', 'subscription_extended',
                               'subscription', ?, ?)""",
                    (
                        str(sub_id),
                        json.dumps({
                            "days_added": 30,
                            "previous_expires_at": sub_expires_raw,
                            "new_expires_at": new_expires_str,
                            "order_id": int(order_id),
                            "user_id": user_id,
                            "plan_id": plan_id,
                        }),
                    ),
                )
            except Exception:
                logger.debug(
                    "audit log for subscription extension failed",
                    exc_info=True,
                )
        else:
            # Reset (cancelled / expired / pending_payment): start a
            # fresh billing period from now.
            cursor.execute(
                """
                UPDATE subscriptions
                SET status = 'active', started_at = ?, expires_at = ?,
                    auto_renew = ?, credits_used_this_period = 0
                WHERE id = ?
            """,
                (started_str, expires_str, auto_renew, sub_id),
            )
    else:
        cursor.execute(
            """
            INSERT INTO subscriptions
                (user_id, plan_id, status, started_at, expires_at,
                 credits_used_this_period, auto_renew)
            VALUES (?, ?, 'active', ?, ?, 0, ?)
        """,
            (user_id, plan_id, started_str, expires_str, auto_renew),
        )
        sub_id = int(cursor.lastrowid)

    cursor.execute("UPDATE users SET plan_id = ? WHERE id = ?", (plan_id, user_id))

    # H2: 在线支付订阅路径同样需要发放 monthly_credits。走 grant_credits
    # helper 以统一 expires_at stamp / expiry_debited / ledger type，
    # 让 CREDITS_EXPIRE_DAYS 对在线支付订阅路径同样生效。复用调用方
    # 事务（cursor.connection）以保证原子性。
    if monthly_credits > 0:
        from backend.database import grant_credits

        grant_credits(
            user_id,
            monthly_credits,
            "renew",
            related_type="subscription",
            related_id=sub_id,
            note=f"订阅激活赠送: {plan_code}",
            conn=cursor.connection,
        )


def reject_order(order_id: int, admin_id: int, reason: str = "") -> bool:
    """Reject a pending order.

    Fix 7: only orders in ``pending`` state can be rejected. Returns
    False for missing orders or orders in any other state.
    """
    _notif_user_id: Optional[int] = None
    _notif_order_no: Optional[str] = None
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT user_id, order_no FROM orders WHERE id = ?",
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK")
                return False
            _notif_user_id = int(row[0])
            _notif_order_no = str(row[1] or "")
            cursor.execute(
                """
                UPDATE orders
                SET status = 'failed', approved_by = ?, note = ?
                WHERE id = ? AND status = 'pending'
            """,
                (int(admin_id) if admin_id else None, reason or "", int(order_id)),
            )
            if cursor.rowcount == 0:
                cursor.execute("ROLLBACK")
                return False
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    # Emit notification after successful rejection.
    if _notif_user_id is not None:
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                _notif_user_id,
                type="order_rejected",
                title="订单被拒绝",
                body=f"订单 {_notif_order_no} 已被拒绝" + (f", 原因: {reason}" if reason else ""),
                metadata={"order_no": _notif_order_no, "reason": reason},
            )
        except Exception:
            logger.exception("notification emit failed for reject_order %s", order_id)

        # Send email notification if user has email on file
        try:
            user_info = _get_user_email_info(_notif_user_id)
            if user_info:
                from backend.services.email_service import EmailService

                _fire_email(
                    EmailService.send_order_rejected(
                        email=user_info["email"],
                        username=user_info["username"],
                        order_no=_notif_order_no or "",
                        reason=reason or None,
                    )
                )
        except Exception:
            logger.exception("email send failed for reject_order %s", order_id)
    return True


def refund_order(
    order_id: int,
    admin_id: int,
    reason: str = "",
    *,
    partial_credits: Optional[float] = None,
    source: str = "admin",
) -> bool:
    """Refund a paid order atomically.

    Wallet debit, wallet_transaction record, and order status update
    all happen in a single ``BEGIN IMMEDIATE`` transaction (Fix 1).
    ``balance_after`` reflects the actual post-debit balance (Fix 3).
    Only orders in ``paid`` state can be refunded (Fix 7).

    Partial refund: if the wallet can't cover the full ``total_credits``
    (e.g. the user already spent most of them), the order is still
    marked ``refunded`` but only ``min(total_credits, balance)`` is
    debited. The shortfall is recorded in ``orders.note`` as a JSON
    blob and an ``order_partial_refund`` audit log row is written so
    operators can reconcile. The order never stays ``paid`` just
    because the wallet ran low.

    P1.3: ``partial_credits`` lets the caller request a *proportional*
    partial refund (e.g. Stripe refunded half the charge → refund half
    the credits). When provided, only ``partial_credits`` (capped at
    the wallet balance) is debited instead of the full
    ``total_credits``. The bonus/recharge ``expiry_debited`` flipping
    naturally scales down because it uses ``actual_refund`` as its
    budget.

    P3.4: ``source`` records who initiated the refund
    (``"admin"`` / ``"stripe_webhook"`` / ``"usdt_chargeback"``) on
    the audit row so finance can distinguish gateway-driven refunds
    from manual admin actions.
    """
    _notif_user_id: Optional[int] = None
    _notif_order_no: Optional[str] = None
    _notif_amount: float = 0.0
    _partial_shortfall: float = 0.0
    _partial_total: float = 0.0
    _partial_debited: float = 0.0
    # P1.3: track the original order credits + the actually-debited
    # amount for Stripe partial refunds so the audit row records the
    # proportional breakdown even when the wallet covered the debit.
    _stripe_original_credits: float = 0.0
    _stripe_refunded_credits: float = 0.0
    _stripe_debited: float = 0.0
    _is_stripe_partial: bool = partial_credits is not None
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            # 1. Read the order under RESERVED lock
            cursor.execute(
                "SELECT id, user_id, amount, credits, bonus_credits, status, order_no FROM orders WHERE id = ?",
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row or row[5] != "paid":
                cursor.execute("ROLLBACK")
                return False

            user_id = int(row[1])
            _notif_amount = float(row[2] or 0)
            total_credits = float(row[3] or 0) + float(row[4] or 0)
            order_no = str(row[6] or "")

            # P1.3: when ``partial_credits`` is provided (e.g. Stripe
            # partial refund), only debit that amount instead of the
            # full order total. Cap at the full total_credits so a
            # misbehaving caller can't refund more than was granted.
            if partial_credits is not None:
                refund_target = min(float(partial_credits), total_credits)
            else:
                refund_target = total_credits

            # 2. Debit wallet (ensure wallet row exists)
            cursor.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (user_id,),
            )
            wallet_row = cursor.fetchone()
            if not wallet_row:
                cursor.execute(
                    "INSERT INTO wallets (user_id, balance) VALUES (?, 0)",
                    (user_id,),
                )
                current_balance = 0.0
            else:
                current_balance = float(wallet_row[0] or 0)

            # Partial refund: debit whatever the wallet can cover. The
            # order still transitions to 'refunded' so the admin can
            # close it; the shortfall is recorded for reconciliation.
            actual_refund = min(refund_target, current_balance)
            new_balance = current_balance - actual_refund
            shortfall = refund_target - actual_refund

            if actual_refund > 0:
                cursor.execute(
                    """
                    UPDATE wallets
                    SET balance = ?,
                        total_consumed = total_consumed + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (new_balance, actual_refund, user_id),
                )

                # 3. Record wallet transaction with correct balance_after (Fix 3)
                cursor.execute(
                    """
                    INSERT INTO wallet_transactions
                        (user_id, type, amount, balance_after, related_type, related_id, note)
                        VALUES (?, 'refund', ?, ?, 'order', ?, ?)
                    """,
                    (
                        user_id,
                        -actual_refund,
                        new_balance,
                        int(order_id),
                        reason or f"refund order #{order_id}",
                    ),
                )

                # Fix 1: prioritize debiting type='bonus' rows tied to
                # this order. We flip ``expiry_debited`` to 1 on bonus
                # rows (in ascending id order) up to ``actual_refund``,
                # so the bonus credits are "used up" first and can no
                # longer be expired-swept or treated as live. The
                # wallet balance itself is unaffected — this is purely
                # bookkeeping on the wallet_transactions ledger.
                #
                # P0.1: 同样需要翻转 type='recharge' 的积分条，否则
                # sweep_expired_credits() 在这些条目过期后会再次扣
                # 减余额，造成双扣资金漏洞。先处理 bonus，再用剩余
                # 额度处理 recharge，按 id 升序逐条翻转 expiry_debited。
                remaining_to_reverse = actual_refund
                cursor.execute(
                    """
                    SELECT id, amount FROM wallet_transactions
                    WHERE user_id = ?
                      AND type = 'bonus'
                      AND related_type = 'order'
                      AND related_id = ?
                      AND expiry_debited = 0
                    ORDER BY id ASC
                    """,
                    (user_id, int(order_id)),
                )
                for tx_row in cursor.fetchall():
                    if remaining_to_reverse <= 0:
                        break
                    tx_id = int(tx_row[0])
                    tx_amount = float(tx_row[1] or 0)
                    cursor.execute(
                        "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                        (tx_id,),
                    )
                    remaining_to_reverse -= tx_amount

                # P0.1: bonus 行翻转完后，剩余额度继续翻转 recharge 行。
                # 这些行由 approve_order / approve_mismatched_order 写入，
                # 同样会被 sweep_expired_credits() 二次扣费。
                if remaining_to_reverse > 0:
                    cursor.execute(
                        """
                        SELECT id, amount FROM wallet_transactions
                        WHERE user_id = ?
                          AND type = 'recharge'
                          AND related_type = 'order'
                          AND related_id = ?
                          AND expiry_debited = 0
                        ORDER BY id ASC
                        """,
                        (user_id, int(order_id)),
                    )
                    for tx_row in cursor.fetchall():
                        if remaining_to_reverse <= 0:
                            break
                        tx_id = int(tx_row[0])
                        tx_amount = float(tx_row[1] or 0)
                        cursor.execute(
                            "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                            (tx_id,),
                        )
                        remaining_to_reverse -= tx_amount

            # 4. Update order status. Always flip to 'refunded' so the
            #    admin isn't blocked from closing the order; record the
            #    partial-refund details in the note when applicable.
            note_parts: List[str] = []
            if reason:
                note_parts.append(reason)
            # P1.3 / P3.4: record partial-refund provenance + source on
            # every refund so finance can distinguish Stripe-driven
            # partial refunds from admin-initiated full refunds.
            partial_meta: Dict[str, Any] = {"source": source}
            if _is_stripe_partial:
                partial_meta["partial_refund"] = True
                partial_meta["original_credits"] = total_credits
                partial_meta["refunded_credits"] = refund_target
                _stripe_original_credits = total_credits
                _stripe_refunded_credits = refund_target
                _stripe_debited = actual_refund
            if shortfall > 0:
                partial_meta["original"] = total_credits if not _is_stripe_partial else refund_target
                partial_meta["debited"] = actual_refund
                partial_meta["short"] = shortfall
                _partial_shortfall = shortfall
                _partial_total = total_credits
                _partial_debited = actual_refund
            if partial_meta:
                note_parts.append(json.dumps(partial_meta, ensure_ascii=False))
            final_note = " | ".join(note_parts) if note_parts else f"refund source={source}"

            cursor.execute(
                "UPDATE orders SET status = 'refunded', note = ?, approved_by = ? WHERE id = ?",
                (final_note, int(admin_id) if admin_id else None, int(order_id)),
            )

            cursor.execute("COMMIT")
            _notif_user_id = user_id
            _notif_order_no = order_no
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    # Audit-log partial refunds so finance can reconcile the shortfall.
    # Best-effort — a failure here must not mask the successful refund.
    if _partial_shortfall > 0:
        try:
            from backend.services.audit import log_action

            log_action(
                actor_id=int(admin_id) if admin_id else None,
                actor_type="admin" if admin_id else "system",
                action="order_partial_refund",
                target_type="order",
                target_id=int(order_id),
                details={
                    "order_no": _notif_order_no,
                    "user_id": _notif_user_id,
                    "original": _partial_total,
                    "debited": _partial_debited,
                    "short": _partial_shortfall,
                    "reason": reason,
                    "source": source,
                },
                ip_address=None,
            )
        except Exception:
            logger.exception("audit log write failed for partial refund %s", order_id)
        logger.warning(
            "partial refund order %s: debited %.6f of %.6f credits (short %.6f) source=%s",
            _notif_order_no, _partial_debited, _partial_total, _partial_shortfall, source,
        )

    # P1.3: Audit-log Stripe-driven partial refunds (proportional
    # refund with no wallet shortfall) so finance has a record even
    # when the wallet covered the debited amount in full.
    if _is_stripe_partial and _partial_shortfall <= 0:
        try:
            from backend.services.audit import log_action

            log_action(
                actor_id=int(admin_id) if admin_id else None,
                actor_type="system",
                action="order_partial_refund_stripe",
                target_type="order",
                target_id=int(order_id),
                details={
                    "order_no": _notif_order_no,
                    "user_id": _notif_user_id,
                    "original_credits": _stripe_original_credits,
                    "refunded_credits": _stripe_refunded_credits,
                    "debited": _stripe_debited,
                    "reason": reason,
                    "source": source,
                },
                ip_address=None,
            )
        except Exception:
            logger.exception("audit log write failed for stripe partial refund %s", order_id)

    # Reverse any subscription activation performed by this order
    # (best-effort, outside the main transaction).
    _maybe_cancel_subscription_on_refund(int(order_id), _notif_user_id or 0, admin_id)

    # Push an alert so super-admins see refunds in real time — refunds
    # move credits out of the user's wallet and may cancel an active
    # subscription, so they're worth surfacing even when Slack / email
    # is unconfigured (the AlertService still logs at CRITICAL).
    try:
        from backend.services.alert_service import AlertService

        AlertService.send_alert_sync(
            "WARNING",
            f"Order refunded: order={_notif_order_no} user={_notif_user_id} amount={_notif_amount:.2f} source={source}",
            {
                "order_no": _notif_order_no,
                "user_id": _notif_user_id,
                "amount": _notif_amount,
                "admin_id": admin_id,
                "reason": reason,
                "source": source,
            },
        )
    except Exception:
        logger.exception("alert send failed for refund_order %s", order_id)

    # Emit notification outside the transaction.
    if _notif_user_id is not None:
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                _notif_user_id,
                type="order_refunded",
                title="订单已退款",
                body=f"订单 {_notif_order_no} 已退款" + (f", 原因: {reason}" if reason else ""),
                metadata={"order_no": _notif_order_no, "reason": reason},
            )
        except Exception:
            logger.exception("notification emit failed for refund_order %s", order_id)

        # Send email notification if user has email on file
        try:
            user_info = _get_user_email_info(_notif_user_id)
            if user_info:
                from backend.services.email_service import EmailService

                _fire_email(
                    EmailService.send_order_refunded(
                        email=user_info["email"],
                        username=user_info["username"],
                        order_no=_notif_order_no or "",
                        amount=_notif_amount,
                    )
                )
        except Exception:
            logger.exception("email send failed for refund_order %s", order_id)

    return True


def get_order(order_id: int) -> Optional[Dict[str, Any]]:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM orders WHERE id = ?", (int(order_id),))
        return _row_to_dict(cursor.fetchone())


def get_order_by_no(order_no: str, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_db_context() as conn:
        cursor = conn.cursor()
        if user_id is not None:
            cursor.execute(
                "SELECT * FROM orders WHERE order_no = ? AND user_id = ?",
                (order_no, int(user_id)),
            )
        else:
            cursor.execute("SELECT * FROM orders WHERE order_no = ?", (order_no,))
        return _row_to_dict(cursor.fetchone())


def list_orders(
    user_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    clauses = []
    params: List[Any] = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(int(user_id))
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT * FROM orders {where}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([int(limit), int(offset)])
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return [_row_to_dict(r) for r in cursor.fetchall()]


def process_expired_orders() -> int:
    """Expire stale pending orders and roll back promo code usage.

    - Non-admin_grant orders expire after 30 minutes.
    - admin_grant orders expire after 7 days.

    Returns the count of orders expired.
    """
    count = 0
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cutoff_short = (_now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            cutoff_long = (_now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute(
                """
                SELECT id, order_no, user_id, promo_code
                FROM orders
                WHERE status = 'pending'
                  AND (
                    (payment_method != 'admin_grant' AND created_at < ?)
                    OR
                    (payment_method = 'admin_grant' AND created_at < ?)
                  )
                """,
                (cutoff_short, cutoff_long),
            )
            expired_orders = cursor.fetchall()

            for order_row in expired_orders:
                oid = order_row[0]
                promo_code = order_row[3]

                cursor.execute(
                    "UPDATE orders SET status = 'expired' WHERE id = ?",
                    (oid,),
                )

                if promo_code:
                    cursor.execute(
                        """
                        UPDATE promo_codes SET used_count = MAX(0, used_count - 1)
                        WHERE code = ? AND used_count > 0
                        """,
                        (promo_code,),
                    )
                    cursor.execute(
                        "DELETE FROM promo_code_usage WHERE order_id = ?",
                        (oid,),
                    )

                count += 1

            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    if count > 0:
        logger.info("process_expired_orders: expired %d pending orders", count)
    return count


def process_pending_payment_subscriptions() -> int:
    """Expire subscriptions in 'pending_payment' status that have passed
    the 3-day grace period without payment.

    Returns the count of subscriptions expired.
    """
    count = 0
    grace_cutoff = (_now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")

    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                """
                SELECT id, user_id, plan_id
                FROM subscriptions
                WHERE status = 'pending_payment'
                  AND started_at < ?
                """,
                (grace_cutoff,),
            )
            expired_subs = cursor.fetchall()

            for sub_row in expired_subs:
                sub_id = sub_row[0]
                uid = sub_row[1]
                pid = sub_row[2]

                cursor.execute(
                    "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                    (sub_id,),
                )
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM subscriptions
                    WHERE user_id = ? AND status = 'active' AND id > ?
                    """,
                    (uid, sub_id),
                )
                newer = cursor.fetchone()
                if not newer or newer[0] == 0:
                    # P1.8 同步：清空 plan_id 时也清空 plan_expires_at，
                    # 避免 get_my_subscription 路由读到陈旧的过期时间。
                    cursor.execute(
                        "UPDATE users SET plan_id = NULL, plan_expires_at = NULL "
                        "WHERE id = ? AND plan_id = ?",
                        (uid, pid),
                    )
                    # P2.7：通知用户订阅因未及时付款已失效。
                    try:
                        from backend.services.notification_service import NotificationService

                        NotificationService.notify(
                            user_id=uid,
                            type="subscription_payment_timeout",
                            title="订阅已过期",
                            body="您的订阅因未在宽限期内完成付款已过期，请重新订阅。",
                        )
                    except Exception:
                        logger.exception(
                            "failed to send subscription_payment_timeout notification "
                            "for user=%s sub=%s",
                            uid,
                            sub_id,
                        )
                count += 1

            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    if count > 0:
        logger.info("process_pending_payment_subscriptions: expired %d subscriptions", count)
    return count


def cancel_order(user_id: int, order_no: str) -> bool:
    """Cancel a pending order on user's request.

    Only orders in 'pending' status belonging to the user can be cancelled.
    Rolls back any promo code usage.
    Returns True on success, False if the order doesn't exist or isn't pending.
    """
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT id, user_id, status, promo_code FROM orders WHERE order_no = ? AND user_id = ?",
                (order_no, int(user_id)),
            )
            row = cursor.fetchone()
            if not row or row[2] != "pending":
                cursor.execute("ROLLBACK")
                return False

            oid = row[0]
            promo_code = row[3]

            cursor.execute(
                "UPDATE orders SET status = 'cancelled' WHERE id = ?",
                (oid,),
            )

            if promo_code:
                cursor.execute(
                    """
                    UPDATE promo_codes SET used_count = MAX(0, used_count - 1)
                    WHERE code = ? AND used_count > 0
                    """,
                    (promo_code,),
                )
                cursor.execute(
                    "DELETE FROM promo_code_usage WHERE order_id = ?",
                    (oid,),
                )

            cursor.execute("COMMIT")
            return True
        except Exception:
            cursor.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------------------
# Redeem codes
# ---------------------------------------------------------------------------


def _gen_redeem_code(prefix: str = "") -> str:
    body = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
    return f"{prefix}{body}" if prefix else body


def create_redeem_codes(
    count: int,
    code_type: str,
    value: float,
    *,
    prefix: str = "",
    plan_id: Optional[int] = None,
    max_uses: int = 1,
    expires_at: Optional[str] = None,
    admin_id: Optional[int] = None,
) -> List[str]:
    """Bulk-create redeem codes and return the list of generated strings."""
    if int(count) <= 0:
        raise ValueError("count 必须 > 0")
    if code_type not in ("credits", "plan_days", "plan_upgrade"):
        raise ValueError("type 必须是 credits / plan_days / plan_upgrade")
    if float(value) <= 0:
        raise ValueError("value 必须 > 0")

    codes: List[str] = []
    with get_db_context() as conn:
        cursor = conn.cursor()
        for _ in range(int(count)):
            code = _gen_redeem_code(prefix)
            cursor.execute(
                """
                INSERT INTO redeem_codes
                    (code, type, value, plan_id, max_uses, expires_at,
                     is_active, created_by)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
                (
                    code,
                    code_type,
                    float(value),
                    plan_id,
                    int(max_uses),
                    expires_at,
                    int(admin_id) if admin_id else None,
                ),
            )
            codes.append(code)
    return codes


def redeem_code(code: str, user_id: int) -> Dict[str, Any]:
    """Redeem a code for a user. Atomic: validates, increments usage,
    applies the effect (credits / plan_days / plan_upgrade).
    """
    code = (code or "").strip()
    if not code:
        raise ValueError("兑换码不能为空")
    user_id = int(user_id)

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                """
                SELECT * FROM redeem_codes
                WHERE code = ? AND is_active = 1
            """,
                (code,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                raise ValueError("兑换码不存在或已停用")

            expires_at = _parse_dt(row["expires_at"])
            if expires_at and expires_at < _now():
                conn.rollback()
                raise ValueError("兑换码已过期")

            if row["max_uses"] and row["used_count"] >= row["max_uses"]:
                conn.rollback()
                raise ValueError("兑换码已被用完")

            # Per-user single-use constraint (typical: max_uses = 1)
            cursor.execute(
                """
                SELECT COUNT(*) FROM redeem_code_usage
                WHERE redeem_code_id = ? AND user_id = ?
            """,
                (row["id"], user_id),
            )
            already = int(cursor.fetchone()[0] or 0)
            if already > 0:
                conn.rollback()
                raise ValueError("您已经使用过该兑换码")

            rtype = row["type"]
            value = float(row["value"] or 0)
            plan_id = row["plan_id"]
            credits_granted = 0.0

            if rtype == "credits":
                credits_granted = value
            elif rtype == "plan_days":
                # Create / extend a subscription. Plan id is required.
                if not plan_id:
                    conn.rollback()
                    raise ValueError("兑换码未配置 plan_id")
                cursor.execute(
                    """
                    SELECT id, expires_at FROM subscriptions
                    WHERE user_id = ? AND plan_id = ? AND status = 'active'
                    ORDER BY id DESC LIMIT 1
                """,
                    (user_id, plan_id),
                )
                existing = cursor.fetchone()
                days = int(value)
                base = _now()
                if existing and existing["expires_at"]:
                    current_expiry = _parse_dt(existing["expires_at"])
                    if current_expiry and current_expiry > base:
                        base = current_expiry
                new_expiry = base + timedelta(days=days)
                expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
                if existing:
                    cursor.execute(
                        """
                        UPDATE subscriptions
                        SET expires_at = ?, status = 'active',
                            auto_renew = COALESCE(auto_renew, 1)
                        WHERE id = ?
                    """,
                        (expiry_str, int(existing["id"])),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO subscriptions
                            (user_id, plan_id, status, started_at, expires_at, auto_renew)
                        VALUES (?, ?, 'active', CURRENT_TIMESTAMP, ?, 1)
                    """,
                        (user_id, plan_id, expiry_str),
                    )
            elif rtype == "plan_upgrade":
                if not plan_id:
                    conn.rollback()
                    raise ValueError("兑换码未配置 plan_id")
                cursor.execute(
                    "SELECT monthly_credits, code FROM plans WHERE id = ? AND is_active = 1",
                    (plan_id,),
                )
                plan_row = cursor.fetchone()
                if plan_row is None:
                    conn.rollback()
                    raise ValueError("兑换码关联的套餐不存在或已停用")
                if isinstance(plan_row, tuple):
                    upgrade_monthly_credits = float(plan_row[0] or 0)
                    upgrade_plan_code = plan_row[1] or ""
                else:
                    upgrade_monthly_credits = float(plan_row["monthly_credits"] or 0)
                    upgrade_plan_code = plan_row["code"] or ""
                # H2: 创建 active subscription 记录，否则用户拿到 plan_id
                # 但没有任何订阅行——daily expiry job 无法感知、
                # /user/subscription 也读不到。参考 plan_days 的写法。
                now_str = _now().strftime("%Y-%m-%d %H:%M:%S")
                expires_str = (_now() + timedelta(days=30)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                cursor.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, plan_id, status, started_at, expires_at,
                         credits_used_this_period, auto_renew)
                    VALUES (?, ?, 'active', ?, ?, 0, 0)
                """,
                    (user_id, plan_id, now_str, expires_str),
                )
                upgrade_sub_id = int(cursor.lastrowid)
                cursor.execute("UPDATE users SET plan_id = ? WHERE id = ?", (plan_id, user_id))
                if upgrade_monthly_credits > 0:
                    grant_credits(
                        user_id,
                        upgrade_monthly_credits,
                        "renew",
                        related_type="subscription",
                        related_id=upgrade_sub_id,
                        note=f"Redeem code plan upgrade monthly credits: {upgrade_plan_code}",
                        conn=conn,
                    )
            else:
                conn.rollback()
                raise ValueError(f"不支持的兑换码类型: {rtype}")

            # Mark the code as used.
            cursor.execute(
                """
                UPDATE redeem_codes SET used_count = used_count + 1
                WHERE id = ?
            """,
                (row["id"],),
            )
            cursor.execute(
                """
                INSERT INTO redeem_code_usage
                    (redeem_code_id, user_id, credits_granted)
                VALUES (?, ?, ?)
            """,
                (row["id"], user_id, credits_granted),
            )

            # H3: Credit wallet IN THE SAME TRANSACTION as the redeem code
            # consumption. grant_credits stamps expires_at so redeemed
            # credits don't live forever.
            if credits_granted > 0:
                grant_credits(
                    user_id,
                    credits_granted,
                    "redeem",
                    related_type="redeem_code",
                    related_id=int(row["id"]),
                    note=f"redeem code {code}",
                    conn=conn,
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {
        "type": rtype,
        "value": value,
        "plan_id": plan_id,
        "credits_granted": credits_granted,
    }


# ---------------------------------------------------------------------------
# Fix 4: USDT partially_paid handling
# ---------------------------------------------------------------------------


def handle_partial_payment(order_id: int, paid_amount: float) -> bool:
    """Mark an order as ``pending_review`` after a partial USDT payment.

    NOWPayments can deliver a ``partially_paid`` IPN when the user
    under-pays the invoice (wrong amount, insufficient gas, etc.).
    The USDT provider maps that to ``pending`` today, which the webhook
    silently ignores — leaving the order stuck and the user with no
    feedback.

    This helper:
      1. Flips ``orders.status`` to ``pending_review`` (so the admin
         reconciliation console surfaces it).
      2. Writes an ``order_partial_payment`` audit log row with the
         expected vs paid amounts.
      3. Notifies the user (in-app) of the shortfall and the action
         required (top up the difference or contact support).

    Returns ``True`` if the order was transitioned, ``False`` if the
    order was missing or already in a terminal state. The method is
    safe to call multiple times — once ``status == 'pending_review'``
    it is a no-op (only the first call writes the audit row and
    notification).

    Designed to be called from ``routes/billing.py::usdt_webhook``
    when the provider reports ``partially_paid``.
    """
    if paid_amount is None or float(paid_amount) < 0:
        paid_amount = 0.0
    paid_amount = float(paid_amount)

    _notif_user_id: Optional[int] = None
    _notif_order_no: Optional[str] = None
    _expected_amount: float = 0.0
    transitioned: bool = False
    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "SELECT id, user_id, amount, status, order_no FROM orders WHERE id = ?",
                (int(order_id),),
            )
            row = cursor.fetchone()
            if not row:
                cursor.execute("ROLLBACK")
                return False
            current_status = row[3]
            # Only pending orders can be transitioned. Already-reviewed
            # orders stay as-is (the admin is already aware).
            if current_status != "pending":
                cursor.execute("ROLLBACK")
                return False

            _notif_user_id = int(row[1])
            _expected_amount = float(row[2] or 0)
            _notif_order_no = str(row[4] or "")

            shortfall = max(0.0, _expected_amount - paid_amount)
            partial_note = json.dumps(
                {
                    "partial_payment": True,
                    "expected": _expected_amount,
                    "paid": paid_amount,
                    "shortfall": shortfall,
                },
                ensure_ascii=False,
            )
            cursor.execute(
                "UPDATE orders SET status = 'pending_review', "
                "note = COALESCE(note, '') || ? WHERE id = ?",
                (partial_note, int(order_id)),
            )
            cursor.execute("COMMIT")
            transitioned = True
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    if not transitioned:
        return False

    # Audit log (best-effort).
    try:
        from backend.services.audit import log_action

        log_action(
            actor_id=None,
            actor_type="system",
            action="order_partial_payment",
            target_type="order",
            target_id=int(order_id),
            details={
                "order_no": _notif_order_no,
                "user_id": _notif_user_id,
                "expected": _expected_amount,
                "paid": paid_amount,
                "shortfall": max(0.0, _expected_amount - paid_amount),
            },
            ip_address=None,
        )
    except Exception:
        logger.exception(
            "audit log write failed for handle_partial_payment %s", order_id
        )

    # Notify the user (best-effort).
    if _notif_user_id is not None:
        try:
            from backend.services.notification_service import NotificationService

            shortfall_amt = max(0.0, _expected_amount - paid_amount)
            NotificationService.notify(
                _notif_user_id,
                type="partial_payment",
                title="支付金额不足",
                body=(
                    f"订单 {_notif_order_no} 收到的支付金额 "
                    f"{paid_amount:.4f} 低于应付金额 "
                    f"{_expected_amount:.4f}(差额 {shortfall_amt:.4f})。"
                    f"订单已进入待复核状态,请补足差额或联系管理员。"
                ),
                metadata={
                    "order_no": _notif_order_no,
                    "expected": _expected_amount,
                    "paid": paid_amount,
                    "shortfall": shortfall_amt,
                },
            )
        except Exception:
            logger.exception(
                "notification emit failed for handle_partial_payment %s", order_id
            )

    return True


# ---------------------------------------------------------------------------
# Fix 6: Per-admin 24h wallet-operations aggregate
# ---------------------------------------------------------------------------


def get_admin_daily_wallet_operations(admin_id: int) -> Dict[str, float]:
    """Aggregate the total credit value of all wallet-impacting
    operations an admin has performed in the last 24 hours.

    The existing cap in ``routes/admin_billing.py::admin_adjust_wallet``
    only counts ``type='admin_adjust'`` rows. That misses two other
    admin-driven credit paths:

      * ``approve_order`` writes ``type='recharge'`` rows tied to the
        order via ``related_type='order'``. The approving admin is
        recorded on ``orders.approved_by``.
      * ``redeem_code`` admin path writes ``type='redeem'`` rows tied
        to the redeem code via ``related_type='redeem_code'``. The
        creating admin is recorded on ``redeem_codes.created_by``.

    This helper returns a dict with the per-source breakdown plus a
    ``total`` sum so the route layer can enforce a unified cap that
    closes the multi-path bypass.

    Returns ``{"admin_adjust": float, "approve_order": float,
    "redeem_code": float, "total": float}``.
    """
    cutoff = (_now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    admin_id_int = int(admin_id) if admin_id else 0
    if admin_id_int <= 0:
        return {"admin_adjust": 0.0, "approve_order": 0.0, "redeem_code": 0.0, "total": 0.0}

    with get_db_context() as conn:
        cursor = conn.cursor()
        # admin_adjust rows carry the admin id in the note as
        # ``[admin:N]`` (see admin_adjust_wallet in admin_billing.py).
        cursor.execute(
            """
            SELECT COALESCE(SUM(ABS(amount)), 0)
            FROM wallet_transactions
            WHERE type = 'admin_adjust'
              AND note LIKE ?
              AND created_at >= ?
            """,
            (f"%admin:{admin_id_int}%", cutoff),
        )
        admin_adjust_total = float(cursor.fetchone()[0] or 0)

        # approve_order rows: wallet_transactions.type='recharge',
        # related_type='order', related_id = orders.id where
        # orders.approved_by = admin_id.
        cursor.execute(
            """
            SELECT COALESCE(SUM(ABS(wt.amount)), 0)
            FROM wallet_transactions wt
            JOIN orders o ON o.id = wt.related_id
            WHERE wt.type = 'recharge'
              AND wt.related_type = 'order'
              AND o.approved_by = ?
              AND wt.created_at >= ?
            """,
            (admin_id_int, cutoff),
        )
        approve_order_total = float(cursor.fetchone()[0] or 0)

        # redeem_code admin path: wallet_transactions.type='redeem',
        # related_type='redeem_code', related_id = redeem_codes.id
        # where redeem_codes.created_by = admin_id.
        cursor.execute(
            """
            SELECT COALESCE(SUM(ABS(wt.amount)), 0)
            FROM wallet_transactions wt
            JOIN redeem_codes rc ON rc.id = wt.related_id
            WHERE wt.type = 'redeem'
              AND wt.related_type = 'redeem_code'
              AND rc.created_by = ?
              AND wt.created_at >= ?
            """,
            (admin_id_int, cutoff),
        )
        redeem_code_total = float(cursor.fetchone()[0] or 0)

    total = admin_adjust_total + approve_order_total + redeem_code_total
    return {
        "admin_adjust": admin_adjust_total,
        "approve_order": approve_order_total,
        "redeem_code": redeem_code_total,
        "total": total,
    }
