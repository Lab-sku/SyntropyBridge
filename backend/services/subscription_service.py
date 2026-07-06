"""Subscription lifecycle management: upgrade, downgrade, cancel, renew,
expiry processing, and auto-renewal.

All write operations use ``BEGIN IMMEDIATE`` to prevent double-spend and
inconsistent-state race conditions on SQLite.

Proration logic
---------------
- **Upgrade**: refund ``(days_left / period_days) * old_plan_price`` to the
  wallet, then charge the new plan price. The old subscription is marked
  ``upgraded`` and a new active subscription is created immediately.
- **Downgrade**: no immediate change. The ``pending_plan_id`` column on the
  active subscription is set so the daily job can switch plans at period end.
- **Cancel**: toggles ``auto_renew`` off and records ``cancelled_at``. The
  active subscription runs until its natural expiry.
- **Renew**: creates a new subscription for the next billing period. If the
  wallet has enough balance the charge is immediate; otherwise a pending
  order is created for the user to pay manually.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from backend.database import (
    _credits_expire_at,
    get_db,
    get_db_context,
    get_wallet,
    grant_credits,
    update_wallet,
)

logger = logging.getLogger(__name__)

# Default billing period in days (monthly subscriptions).
_PERIOD_DAYS = 30


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Parse a timestamp string from SQLite into a timezone-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


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


def _get_plan_name(plan_id: int) -> str:
    """Return the plan name for a given plan_id, falling back to 'Unknown'."""
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM plans WHERE id = ?", (int(plan_id),))
            row = cursor.fetchone()
            if row:
                return row["name"]
    except Exception:
        pass
    return "Unknown"


def _fire_email(coro) -> None:
    """Schedule an async email coroutine on the running event loop."""
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        import threading

        def _run():
            asyncio.run(coro)

        threading.Thread(target=_run, daemon=True).start()


class SubscriptionService:
    """Static-method-only service for subscription lifecycle management."""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @staticmethod
    def get_active(user_id: int) -> Optional[dict]:
        """Return the user's currently active subscription (if any),
        joined with plan details."""
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT s.id, s.user_id, s.plan_id, s.status, s.started_at,
                       s.expires_at, s.credits_used_this_period, s.auto_renew,
                       s.cancelled_at, s.pending_plan_id,
                       p.name AS plan_name, p.code AS plan_code,
                       p.monthly_price AS plan_price,
                       p.monthly_credits AS plan_credits
                FROM subscriptions s
                LEFT JOIN plans p ON p.id = s.plan_id
                WHERE s.user_id = ? AND s.status = 'active'
                ORDER BY s.id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            # Attach pending plan name if set
            pending_id = result.get("pending_plan_id")
            if pending_id:
                cursor.execute(
                    "SELECT name, code, monthly_price FROM plans WHERE id = ?",
                    (pending_id,),
                )
                pp = cursor.fetchone()
                if pp:
                    result["pending_plan_name"] = pp["name"]
                    result["pending_plan_code"] = pp["code"]
                    result["pending_plan_price"] = float(pp["monthly_price"] or 0)
            return result

    # ------------------------------------------------------------------
    # Upgrade
    # ------------------------------------------------------------------

    @staticmethod
    def upgrade(user_id: int, new_plan_id: int) -> dict:
        """Subscribe to a plan, prorating any existing subscription.

        When the user already has an active subscription the remaining
        days are prorated (refund credited to wallet) before the new
        plan is charged.  When there is **no** existing subscription
        (fresh / first-time subscriber) the method creates one directly
        without any proration.

        Returns the new subscription dict.
        """
        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # 1. Find active subscription (may be None for first-time users)
            cursor.execute(
                """
                SELECT s.id, s.plan_id, s.started_at, s.expires_at,
                       s.credits_used_this_period, s.auto_renew
                FROM subscriptions s
                WHERE s.user_id = ? AND s.status = 'active'
                ORDER BY s.id DESC LIMIT 1
                """,
                (user_id,),
            )
            old_sub = cursor.fetchone()

            # 2. Load new plan
            cursor.execute(
                "SELECT id, name, monthly_price, monthly_credits, code FROM plans WHERE id = ? AND is_active = 1",
                (new_plan_id,),
            )
            new_plan = cursor.fetchone()
            if not new_plan:
                conn.rollback()
                raise ValueError("Plan not found or inactive")

            new_price = float(new_plan["monthly_price"] or 0)
            new_monthly_credits = float(new_plan.get("monthly_credits") or 0)

            # 2b. Load new plan's code so we can detect enterprise (custom
            # billing, monthly_price=0) and bypass the monotonicity guard.
            new_code = (new_plan.get("code") or "")
            is_enterprise_upgrade = new_code == "enterprise"

            if old_sub:
                cursor.execute(
                    "SELECT id, name, monthly_price, monthly_credits FROM plans WHERE id = ? AND is_active = 1",
                    (old_sub["plan_id"],),
                )
                old_plan = cursor.fetchone()
                old_price = float((old_plan or {}).get("monthly_price") or 0)
                old_code = ((old_plan or {}).get("code") or "") if old_plan else ""
                # Idempotent re-activation: if the user already has the
                # same free plan, just return the existing subscription.
                if new_plan_id == old_sub["plan_id"] and new_price <= 0 and old_price <= 0:
                    conn.rollback()
                    conn.close()
                    return SubscriptionService.get_active(user_id) or {"id": old_sub["id"]}
                # Enterprise upgrades are custom-billing (monthly_price=0)
                # and must be reachable from any plan, including free.
                if not is_enterprise_upgrade and new_price <= old_price:
                    conn.rollback()
                    raise ValueError("New plan must be more expensive than current plan for upgrade")
            else:
                old_plan = None
                old_price = 0.0

            # 3. Compute proration (only when upgrading from an existing plan)
            now = _now_utc()
            refund_amount = 0.0
            days_left = 0
            period_days = _PERIOD_DAYS

            if old_sub:
                expires_at = _parse_ts(old_sub["expires_at"])
                started_at = _parse_ts(old_sub["started_at"])

                if expires_at and started_at:
                    # P3.5: use total_seconds() to preserve sub-day precision
                    period_seconds = max((expires_at - started_at).total_seconds(), 1.0)
                    days_left = max((expires_at - now).total_seconds() / 86400, 0)
                    period_days = period_seconds / 86400
                else:
                    period_days = _PERIOD_DAYS
                    days_left = _PERIOD_DAYS

                refund_amount = (days_left / period_days) * old_price
                refund_amount = round(refund_amount, 4)

            # 4. P1.9: Check wallet balance BEFORE marking old sub upgraded.
            #    If the user can't afford the new plan, we leave the old
            #    active subscription intact and create a pending_payment
            #    sub + pending order. This avoids leaving the user with
            #    NO active subscription (old=upgraded, new=pending_payment).
            auto_renew = int(old_sub.get("auto_renew", 1)) if old_sub else 1
            new_started = now.strftime("%Y-%m-%d %H:%M:%S")
            new_expires = (now + timedelta(days=_PERIOD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

            can_afford = new_price <= 0
            if not can_afford:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
                )
                wrow = cursor.fetchone()
                current_balance = float(wrow["balance"]) if wrow else 0.0
                # Refund is credited before the charge in the same txn,
                # so the effective balance includes the proration refund.
                can_afford = (current_balance + refund_amount) >= new_price

            if not can_afford:
                # P1.9: insufficient balance — don't touch old sub.
                # Create a pending_payment sub + pending order.
                import secrets as _secrets

                cursor.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, plan_id, status, started_at, expires_at,
                         credits_used_this_period, auto_renew)
                    VALUES (?, ?, 'pending_payment', ?, ?, 0, ?)
                    """,
                    (user_id, new_plan_id, new_started, new_expires, auto_renew),
                )
                new_sub_id = cursor.lastrowid
                _order_no = f"ORD{_now_utc().strftime('%Y%m%d%H%M%S')}{_secrets.token_hex(4).upper()}"
                _sub_note = json.dumps({"plan_id": new_plan_id, "auto_renew": bool(auto_renew), "subscription_id": new_sub_id})
                cursor.execute(
                    """INSERT INTO orders
                       (order_no, user_id, amount, credits, payment_method, status, note, created_at)
                       VALUES (?, ?, ?, 0, 'plan_subscription', 'pending', ?, ?)""",
                    (_order_no, user_id, new_price, _sub_note,
                     _now_utc().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return SubscriptionService.get_active(user_id) or {
                    "id": new_sub_id,
                    "status": "pending_payment",
                    "payment_status": "pending_order",
                }

            # 5. Sufficient balance (or free plan): mark old sub upgraded,
            #    create new active sub, refund proration, charge, grant.
            if old_sub:
                # P1.7: clear pending_plan_id when marking old sub upgraded
                cursor.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'upgraded', cancelled_at = ?, pending_plan_id = NULL
                    WHERE id = ?
                    """,
                    (now.strftime("%Y-%m-%d %H:%M:%S"), old_sub["id"]),
                )

            cursor.execute(
                """
                INSERT INTO subscriptions
                    (user_id, plan_id, status, started_at, expires_at,
                     credits_used_this_period, auto_renew)
                VALUES (?, ?, 'active', ?, ?, 0, ?)
                """,
                (user_id, new_plan_id, new_started, new_expires, auto_renew),
            )
            new_sub_id = cursor.lastrowid

            # 5a. Refund proration for unused days on old plan.
            # Route through grant_credits so expires_at is stamped
            # consistently and the ledger row mirrors other refund
            # paths (migration 30: per-credit-entry expiration).
            if refund_amount > 0 and old_sub:
                grant_credits(
                    user_id,
                    refund_amount,
                    "refund",
                    related_type="subscription",
                    related_id=old_sub["id"],
                    note=f"Upgrade proration refund: {days_left:.4f}/{period_days:.4f} days remaining",
                    conn=conn,
                )

            # 5b. Charge new plan price from wallet
            if new_price > 0:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
                )
                wrow = cursor.fetchone()
                balance = float(wrow["balance"]) if wrow else 0.0
                new_balance = balance - new_price
                cursor.execute(
                    """UPDATE wallets SET balance = ?,
                       total_consumed = total_consumed + ?
                       WHERE user_id = ?""",
                    (new_balance, new_price, user_id),
                )
                cursor.execute(
                    """INSERT INTO wallet_transactions
                       (user_id, type, amount, balance_after, related_type, related_id, note)
                       VALUES (?, 'consume', ?, ?, 'subscription', ?, ?)""",
                    (user_id, -new_price, new_balance, new_sub_id,
                     f"Subscription upgrade to {new_plan['name']}"),
                )

            # 5c. Clawback unused monthly_credits from the old subscription.
            # Without this, a user could cycle subscribe→upgrade→subscribe
            # to farm monthly_credits: each upgrade grants a fresh batch
            # of credits while the unused portion of the old plan's
            # monthly_credits stays in the wallet. We debit the prorated
            # unused portion, capped at the current wallet balance so the
            # CHECK (balance >= 0) constraint is never violated.
            if old_sub and old_plan:
                old_monthly_credits = float(old_plan.get("monthly_credits") or 0)
                if old_monthly_credits > 0 and days_left > 0 and period_days > 0:
                    refund_credits = int(old_monthly_credits * days_left / period_days)
                    if refund_credits > 0:
                        cursor.execute(
                            "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
                        )
                        wrow = cursor.fetchone()
                        current_bal = float(wrow["balance"]) if wrow else 0.0
                        actual_debit = min(refund_credits, current_bal)
                        if actual_debit > 0:
                            new_bal = current_bal - actual_debit
                            cursor.execute(
                                """UPDATE wallets SET balance = ?,
                                   total_consumed = total_consumed + ?
                                   WHERE user_id = ?""",
                                (new_bal, actual_debit, user_id),
                            )
                            cursor.execute(
                                """INSERT INTO wallet_transactions
                                   (user_id, type, amount, balance_after,
                                    related_type, related_id, note)
                                   VALUES (?, 'upgrade', ?, ?, 'subscription', ?, ?)""",
                                (user_id, -actual_debit, new_bal, old_sub["id"],
                                 f"Upgrade clawback: unused credits from "
                                 f"{old_plan.get('name') or 'old plan'}"),
                            )
                        if actual_debit < refund_credits:
                            logger.warning(
                                "upgrade clawback capped at balance: user=%s "
                                "refund_credits=%s actual_debit=%s shortfall=%s",
                                user_id, refund_credits, actual_debit,
                                refund_credits - actual_debit,
                            )

                        # Flip expiry_debited on the original renew credits
                        # rows to prevent double-debit by sweep_expired_credits.
                        # The old subscription's monthly_credits were granted
                        # via grant_credits (tx_type='renew',
                        # related_type='subscription'). When
                        # CREDITS_EXPIRE_DAYS > 0 the daily sweep would
                        # otherwise re-debit the clawback portion after the
                        # TTL elapses. Since each renew grant creates a
                        # single ledger row, we mark it as fully debited
                        # when clawback covers any portion. Best-effort:
                        # a failure here does not abort the upgrade.
                        if actual_debit > 0 and old_sub:
                            try:
                                cursor.execute(
                                    """UPDATE wallet_transactions
                                       SET expiry_debited = 1
                                       WHERE user_id = ?
                                         AND type = 'renew'
                                         AND related_type = 'subscription'
                                         AND related_id = ?
                                         AND expiry_debited = 0""",
                                    (user_id, old_sub["id"]),
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to flip expiry_debited for clawback: %s", e
                                )

            # 6. Update users.plan_id + plan_expires_at (P1.8)
            cursor.execute(
                "UPDATE users SET plan_id = ?, plan_expires_at = ? WHERE id = ?",
                (new_plan_id, new_expires, user_id),
            )

            # 7. P1.6: grant monthly_credits via grant_credits helper so
            #    expires_at is stamped consistently and the ledger type
            #    is unified to 'renew' across upgrade/renew/order_service.
            if new_monthly_credits > 0:
                grant_credits(
                    user_id,
                    new_monthly_credits,
                    "renew",
                    related_type="subscription",
                    related_id=new_sub_id,
                    note=f"Upgrade credits: {new_plan['name']}",
                    conn=conn,
                )

            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Emit notification
        try:
            from backend.services.notification_service import NotificationService

            if old_sub:
                NotificationService.notify(
                    user_id,
                    type="subscription_upgraded",
                    title="Subscription upgraded",
                    body=f"Upgraded to {new_plan['name']}. Proration refund: {refund_amount:.2f} credits.",
                    metadata={"new_plan_id": new_plan_id, "refund": refund_amount},
                )
            else:
                NotificationService.notify(
                    user_id,
                    type="subscription_activated",
                    title="套餐已激活",
                    body=f"已激活 {new_plan['name']} 套餐。",
                    metadata={"new_plan_id": new_plan_id},
                )
        except Exception:
            pass

        # Return the new subscription
        return SubscriptionService.get_active(user_id) or {"id": new_sub_id}

    # ------------------------------------------------------------------
    # Downgrade
    # ------------------------------------------------------------------

    @staticmethod
    def downgrade(user_id: int, new_plan_id: int, sub_id: Optional[int] = None) -> dict:
        """Schedule a downgrade at period end. No immediate change to the
        active subscription — just sets ``pending_plan_id``.

        Returns a dict describing the scheduled change.
        """
        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # Find active subscription (P3.6: filter by sub_id if provided)
            if sub_id is not None:
                cursor.execute(
                    """
                    SELECT s.id, s.plan_id, s.expires_at
                    FROM subscriptions s
                    WHERE s.user_id = ? AND s.id = ? AND s.status = 'active'
                    ORDER BY s.id DESC LIMIT 1
                    """,
                    (user_id, sub_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.id, s.plan_id, s.expires_at
                    FROM subscriptions s
                    WHERE s.user_id = ? AND s.status = 'active'
                    ORDER BY s.id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            sub = cursor.fetchone()
            if not sub:
                conn.rollback()
                raise ValueError("No active subscription to downgrade")

            # Validate new plan
            cursor.execute(
                "SELECT id, name, monthly_price FROM plans WHERE id = ? AND is_active = 1",
                (new_plan_id,),
            )
            new_plan = cursor.fetchone()
            if not new_plan:
                conn.rollback()
                raise ValueError("Target plan not found or inactive")

            # Load current plan for price comparison
            cursor.execute(
                "SELECT monthly_price, code FROM plans WHERE id = ?",
                (sub["plan_id"],),
            )
            old_plan = cursor.fetchone()
            old_price = float((old_plan or {}).get("monthly_price") or 0)
            new_price = float(new_plan["monthly_price"] or 0)

            # Enterprise subscriptions are custom-billing; self-service
            # downgrades would bypass the negotiated deal. Admins can
            # still reassign via /admin/users/{id}/plan.
            old_code = (old_plan or {}).get("code") or ""
            if old_code == "enterprise":
                conn.rollback()
                raise ValueError("企业版套餐需联系管理员进行降级")

            if new_price >= old_price:
                conn.rollback()
                raise ValueError("New plan must be cheaper than current plan for downgrade")

            # Schedule the change
            cursor.execute(
                "UPDATE subscriptions SET pending_plan_id = ? WHERE id = ?",
                (new_plan_id, sub["id"]),
            )
            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Notification
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                user_id,
                type="subscription_downgrade_scheduled",
                title="Downgrade scheduled",
                body=f"Your subscription will switch to {new_plan['name']} at period end.",
                metadata={"new_plan_id": new_plan_id, "effective_at": sub.get("expires_at")},
            )
        except Exception:
            pass

        return {
            "subscription_id": sub["id"],
            "current_plan_id": sub["plan_id"],
            "pending_plan_id": new_plan_id,
            "pending_plan_name": new_plan["name"],
            "effective_at": sub.get("expires_at"),
        }

    # ------------------------------------------------------------------
    # Cancel (auto-renew off)
    # ------------------------------------------------------------------

    @staticmethod
    def cancel(user_id: int, sub_id: Optional[int] = None) -> bool:
        """Cancel auto-renewal. Does NOT terminate the active subscription —
        it runs until its natural expiry.

        Returns True if auto-renew was on and is now off.
        """
        had_pending_downgrade = False
        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # P3.6: filter by sub_id if provided
            if sub_id is not None:
                cursor.execute(
                    """
                    SELECT id, auto_renew, pending_plan_id
                    FROM subscriptions
                    WHERE user_id = ? AND id = ? AND status = 'active'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id, sub_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, auto_renew, pending_plan_id
                    FROM subscriptions
                    WHERE user_id = ? AND status = 'active'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            sub = cursor.fetchone()
            if not sub:
                conn.rollback()
                return False

            if not sub.get("auto_renew"):
                conn.rollback()
                return False

            had_pending_downgrade = sub.get("pending_plan_id") is not None

            now = _now_utc().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                UPDATE subscriptions
                SET auto_renew = 0, cancelled_at = ?, pending_plan_id = NULL
                WHERE id = ?
                """,
                (now, sub["id"]),
            )
            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Notification — Chinese, and explicitly call out that any
        # scheduled downgrade is revoked alongside the cancellation.
        try:
            from backend.services.notification_service import NotificationService

            if had_pending_downgrade:
                NotificationService.notify(
                    user_id,
                    type="subscription_cancelled",
                    title="订阅已取消自动续费",
                    body="订阅已取消，到期后将不再自动续费。您之前设置的降级已随取消自动撤销，订阅到期后将恢复为免费版。",
                )
            else:
                NotificationService.notify(
                    user_id,
                    type="subscription_cancelled",
                    title="订阅已取消自动续费",
                    body="订阅已取消，到期后将不再自动续费。",
                )
        except Exception:
            pass

        return True

    # ------------------------------------------------------------------
    # Renew
    # ------------------------------------------------------------------

    @staticmethod
    def renew(user_id: int, sub_id: Optional[int] = None) -> Optional[dict]:
        """Create a new subscription for the next billing period.

        Called by the renewal job when ``auto_renew`` is on, or manually
        by the user if auto-renewal failed.

        Charges the wallet if sufficient balance exists; otherwise creates
        a pending order.

        P0.1: Only selects subs in ``expired`` / ``pending_payment`` status.
        Returns ``None`` when no such sub exists (the user has already
        renewed manually or the sub was never expired) so that
        ``process_expiry`` can detect the no-op without raising.
        """
        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            # P0.1: only select expired / pending_payment subs so we never
            # pick up a freshly-created active sub (which would double-charge
            # the user when the auto-renew worker races a manual renew).
            if sub_id is not None:
                cursor.execute(
                    """
                    SELECT s.id, s.plan_id, s.expires_at, s.auto_renew, s.pending_plan_id
                    FROM subscriptions s
                    WHERE s.user_id = ? AND s.id = ?
                      AND s.status IN ('expired', 'pending_payment')
                    ORDER BY s.id DESC LIMIT 1
                    """,
                    (user_id, sub_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.id, s.plan_id, s.expires_at, s.auto_renew, s.pending_plan_id
                    FROM subscriptions s
                    WHERE s.user_id = ? AND s.status IN ('expired', 'pending_payment')
                    ORDER BY s.id DESC LIMIT 1
                    """,
                    (user_id,),
                )
            old_sub = cursor.fetchone()
            if not old_sub:
                # Already renewed by the user (or never expired). No-op.
                conn.rollback()
                return None

            plan_id = old_sub["plan_id"]

            # Load plan
            cursor.execute(
                "SELECT id, name, monthly_price, monthly_credits, code FROM plans WHERE id = ? AND is_active = 1",
                (plan_id,),
            )
            plan = cursor.fetchone()
            if not plan:
                conn.rollback()
                raise ValueError("Plan not found or inactive")

            price = float(plan["monthly_price"] or 0)
            monthly_credits = float(plan.get("monthly_credits") or 0)

            # Check for pending downgrade
            pending_plan_id = old_sub.get("pending_plan_id")
            if pending_plan_id:
                # Apply the scheduled downgrade: switch to the pending plan
                cursor.execute(
                    "SELECT id, name, monthly_price, monthly_credits, code FROM plans WHERE id = ? AND is_active = 1",
                    (pending_plan_id,),
                )
                pending_plan = cursor.fetchone()
                if pending_plan:
                    plan_id = pending_plan_id
                    plan = pending_plan
                    price = float(pending_plan["monthly_price"] or 0)
                    monthly_credits = float(pending_plan.get("monthly_credits") or 0)

            now = _now_utc()
            new_started = now.strftime("%Y-%m-%d %H:%M:%S")
            new_expires = (now + timedelta(days=_PERIOD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            auto_renew_val = 1 if old_sub.get("auto_renew", 1) else 0

            # P1.9: Check wallet balance BEFORE marking old sub renewed.
            #    If the user can't afford the new period, we leave the old
            #    sub in its current status (expired/pending_payment) and
            #    create a pending_payment sub + pending order. This avoids
            #    leaving the user with NO active subscription when the
            #    auto-renew worker can't charge the wallet.
            can_afford = price <= 0
            if not can_afford:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
                )
                wrow = cursor.fetchone()
                balance = float(wrow["balance"]) if wrow else 0.0
                can_afford = balance >= price

            if not can_afford:
                # P1.9: insufficient balance — don't mark old sub renewed.
                import secrets as _secrets

                cursor.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, plan_id, status, started_at, expires_at,
                         credits_used_this_period, auto_renew)
                    VALUES (?, ?, 'pending_payment', ?, ?, 0, ?)
                    """,
                    (user_id, plan_id, new_started, new_expires, auto_renew_val),
                )
                new_sub_id = cursor.lastrowid
                _order_no = f"ORD{_now_utc().strftime('%Y%m%d%H%M%S')}{_secrets.token_hex(4).upper()}"
                _sub_note = json.dumps({"plan_id": plan_id, "auto_renew": bool(auto_renew_val), "subscription_id": new_sub_id})
                cursor.execute(
                    """INSERT INTO orders
                       (order_no, user_id, amount, credits, payment_method, status, note, created_at)
                       VALUES (?, ?, ?, 0, 'plan_subscription', 'pending', ?, ?)""",
                    (_order_no, user_id, price, _sub_note,
                     _now_utc().strftime("%Y-%m-%d %H:%M:%S")),
                )
                conn.commit()
                return SubscriptionService.get_active(user_id) or {
                    "id": new_sub_id,
                    "status": "pending_payment",
                    "payment_status": "pending_order",
                }

            # Sufficient balance (or free plan): mark old sub renewed with
            # a status guard (P0.1: defense-in-depth against the race where
            # another worker renewed between our SELECT and UPDATE —
            # impossible under BEGIN IMMEDIATE but cheap to check).
            cursor.execute(
                "UPDATE subscriptions SET status = 'renewed' "
                "WHERE id = ? AND status IN ('expired', 'pending_payment')",
                (old_sub["id"],),
            )
            if cursor.rowcount == 0:
                # Lost the race — another tx already renewed this sub.
                conn.rollback()
                return None

            # Create new active subscription
            cursor.execute(
                """
                INSERT INTO subscriptions
                    (user_id, plan_id, status, started_at, expires_at,
                     credits_used_this_period, auto_renew)
                VALUES (?, ?, 'active', ?, ?, 0, ?)
                """,
                (user_id, plan_id, new_started, new_expires, auto_renew_val),
            )
            new_sub_id = cursor.lastrowid

            # Charge wallet INSIDE the same transaction
            payment_status = "charged"
            if price > 0:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
                )
                wrow = cursor.fetchone()
                balance = float(wrow["balance"]) if wrow else 0.0
                new_balance = balance - price
                cursor.execute(
                    """UPDATE wallets SET balance = ?,
                       total_consumed = total_consumed + ?
                       WHERE user_id = ?""",
                    (new_balance, price, user_id),
                )
                cursor.execute(
                    """INSERT INTO wallet_transactions
                       (user_id, type, amount, balance_after, related_type, related_id, note)
                       VALUES (?, 'consume', ?, ?, 'subscription', ?, ?)""",
                    (user_id, -price, new_balance, new_sub_id,
                     f"Subscription renewal: {plan['name']}"),
                )

            # P1.8: update users.plan_id + plan_expires_at together
            cursor.execute(
                "UPDATE users SET plan_id = ?, plan_expires_at = ? WHERE id = ?",
                (plan_id, new_expires, user_id),
            )

            # P1.6: grant monthly_credits via grant_credits helper (unified
            # ledger type 'renew', auto-stamped expires_at).
            if monthly_credits > 0:
                grant_credits(
                    user_id,
                    monthly_credits,
                    "renew",
                    related_type="subscription",
                    related_id=new_sub_id,
                    note=f"Monthly credits renewal: {plan['name']}",
                    conn=conn,
                )

            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Notification
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                user_id,
                type="subscription_renewed",
                title="Subscription renewed",
                body=f"Your {plan['name']} subscription has been renewed for another {_PERIOD_DAYS} days.",
                metadata={"plan_id": plan_id, "payment": payment_status},
            )
        except Exception:
            pass

        # Send email notification if user has email on file
        try:
            user_info = _get_user_email_info(user_id)
            if user_info:
                from backend.services.email_service import EmailService

                expires_dt = _parse_ts(new_expires) or _now_utc() + timedelta(days=_PERIOD_DAYS)
                _fire_email(
                    EmailService.send_subscription_renewed(
                        email=user_info["email"],
                        username=user_info["username"],
                        plan_name=plan["name"],
                        expires_at=expires_dt,
                    )
                )
        except Exception:
            logger.exception("email send failed for subscription renew user=%s", user_id)

        return SubscriptionService.get_active(user_id) or {
            "id": new_sub_id,
            "payment_status": payment_status,
        }

    # ------------------------------------------------------------------
    # Daily jobs
    # ------------------------------------------------------------------

    @staticmethod
    def process_expiry() -> int:
        """Run daily: for each subscription that expired today (or earlier)
        and is still marked active, attempt auto-renewal if enabled,
        otherwise mark as expired and clear the user's plan_id.

        Returns the count of subscriptions processed.
        """
        count = 0
        now = _now_utc()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")

            cursor.execute(
                """
                SELECT id, user_id, plan_id, expires_at, auto_renew, pending_plan_id
                FROM subscriptions
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now_str,),
            )
            expired = cursor.fetchall()

            auto_renew_subs = []
            no_renew_subs = []

            for sub in expired:
                if sub.get("auto_renew"):
                    cursor.execute(
                        """
                        SELECT COUNT(*) as cnt FROM subscriptions
                        WHERE user_id = ? AND status = 'active' AND id > ?
                        """,
                        (sub["user_id"], sub["id"]),
                    )
                    newer = cursor.fetchone()
                    if newer and newer["cnt"] > 0:
                        cursor.execute(
                            "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                            (sub["id"],),
                        )
                        no_renew_subs.append(sub)
                    else:
                        cursor.execute(
                            "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                            (sub["id"],),
                        )
                        auto_renew_subs.append(sub)
                else:
                    # Materialise any scheduled downgrade before marking
                    # the sub expired. Without this, a user who had
                    # both (a) scheduled a downgrade and (b) turned off
                    # auto-renew would lose the pending plan at expiry
                    # and be kicked to no-plan instead of to the
                    # downgraded tier.
                    pending_id = sub.get("pending_plan_id")
                    if pending_id:
                        cursor.execute(
                            "SELECT id, name, monthly_price FROM plans WHERE id = ? AND is_active = 1",
                            (pending_id,),
                        )
                        pending_plan = cursor.fetchone()
                        if pending_plan:
                            cursor.execute(
                                "UPDATE subscriptions SET status = 'expired', pending_plan_id = NULL WHERE id = ?",
                                (sub["id"],),
                            )
                            started_str = now.strftime("%Y-%m-%d %H:%M:%S")
                            expires_new = (now + timedelta(days=_PERIOD_DAYS)).strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            cursor.execute(
                                """
                                INSERT INTO subscriptions
                                    (user_id, plan_id, status, started_at, expires_at,
                                     credits_used_this_period, auto_renew)
                                VALUES (?, ?, 'active', ?, ?, 0, 0)
                                """,
                                (pending_id, sub["user_id"], started_str, expires_new),
                            )
                            # P1.8: keep plan_expires_at in sync with the new sub
                            cursor.execute(
                                "UPDATE users SET plan_id = ?, plan_expires_at = ? WHERE id = ?",
                                (pending_id, expires_new, sub["user_id"]),
                            )
                            # Rewrite the sub dict so the notification
                            # block below sees the *new* plan_id.
                            sub = {**sub, "plan_id": pending_id, "downgraded": True}
                        else:
                            # Pending plan no longer valid (deleted /
                            # deactivated). Fall through to plain expiry.
                            cursor.execute(
                                "UPDATE subscriptions SET status = 'expired', pending_plan_id = NULL WHERE id = ?",
                                (sub["id"],),
                            )
                            cursor.execute(
                                "UPDATE users SET plan_id = NULL, plan_expires_at = NULL WHERE id = ? AND plan_id = ?",
                                (sub["user_id"], sub["plan_id"]),
                            )
                    else:
                        cursor.execute(
                            "UPDATE subscriptions SET status = 'expired' WHERE id = ?",
                            (sub["id"],),
                        )
                        cursor.execute(
                            """
                            SELECT COUNT(*) as cnt FROM subscriptions
                            WHERE user_id = ? AND status = 'active' AND id > ?
                            """,
                            (sub["user_id"], sub["id"]),
                        )
                        newer = cursor.fetchone()
                        if not newer or newer["cnt"] == 0:
                            cursor.execute(
                                "UPDATE users SET plan_id = NULL, plan_expires_at = NULL WHERE id = ? AND plan_id = ?",
                                (sub["user_id"], sub["plan_id"]),
                            )
                    no_renew_subs.append(sub)
                count += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        for sub in auto_renew_subs:
            try:
                result = SubscriptionService.renew(sub["user_id"])
                if result is None:
                    # P0.1: renew() no-op'd because the sub is no longer
                    # in 'expired'/'pending_payment' status — the user
                    # most likely renewed manually. Nothing to do.
                    logger.info(
                        "renew() no-op for user %s sub %s (already renewed)",
                        sub["user_id"],
                        sub["id"],
                    )
                    continue
            except Exception:
                logger.warning(
                    "Auto-renewal failed for user %s sub %s, falling back to expiry",
                    sub["user_id"],
                    sub["id"],
                    exc_info=True,
                )
                # P1.8: clear plan_id + plan_expires_at together
                plan_name_for_notif = _get_plan_name(sub["plan_id"])
                try:
                    with get_db_context() as conn2:
                        cursor2 = conn2.cursor()
                        cursor2.execute(
                            "UPDATE users SET plan_id = NULL, plan_expires_at = NULL WHERE id = ?",
                            (sub["user_id"],),
                        )
                except Exception:
                    logger.warning(
                        "failed to clear plan_id for user %s after auto-renewal failure",
                        sub["user_id"],
                        exc_info=True,
                    )

                # P1.3: notify the user that auto-renewal failed and
                # their subscription has been downgraded to free.
                try:
                    from backend.services.notification_service import NotificationService

                    NotificationService.notify(
                        sub["user_id"],
                        type="auto_renew_failed",
                        title="自动续费失败",
                        body=(
                            f"您的订阅 {plan_name_for_notif} 自动续费失败，已转为免费版。"
                            f"请尽快充值后重新订阅。"
                        ),
                        metadata={"subscription_id": sub["id"], "plan_name": plan_name_for_notif},
                    )
                except Exception:
                    logger.exception(
                        "failed to send auto_renew_failed notification for user %s",
                        sub["user_id"],
                    )

                # Best-effort email — reuse the subscription_expired template
                # since no dedicated auto_renew_failed template exists.
                try:
                    user_info = _get_user_email_info(sub["user_id"])
                    if user_info:
                        from backend.services.email_service import EmailService

                        _fire_email(
                            EmailService.send_subscription_expired(
                                email=user_info["email"],
                                username=user_info["username"],
                                plan_name=plan_name_for_notif,
                            )
                        )
                except Exception:
                    logger.exception(
                        "email send failed for auto_renew_failed user=%s",
                        sub["user_id"],
                    )

        for sub in no_renew_subs:
            # Skip the "moved to free tier" notification when the sub
            # was downgraded — the user's already been told about the
            # scheduled change and is now on the downgraded plan, not
            # the free tier.
            if sub.get("downgraded"):
                continue

            try:
                from backend.services.notification_service import NotificationService

                NotificationService.notify(
                    sub["user_id"],
                    type="subscription_expired",
                    title="Subscription expired",
                    body="Your subscription has expired. You have been moved to the free tier.",
                    metadata={"subscription_id": sub["id"]},
                )
            except Exception:
                pass

            try:
                user_info = _get_user_email_info(sub["user_id"])
                if user_info:
                    plan_name = _get_plan_name(sub["plan_id"])
                    from backend.services.email_service import EmailService

                    _fire_email(
                        EmailService.send_subscription_expired(
                            email=user_info["email"],
                            username=user_info["username"],
                            plan_name=plan_name,
                        )
                    )
            except Exception:
                logger.exception("email send failed for subscription_expired sub=%s", sub["id"])

        return count

    @staticmethod
    def process_upcoming_renewals() -> int:
        """Run daily: for each subscription expiring within 3 days,
        send an expiry reminder email and in-app notification.

        Actual renewal (charge + new subscription) is handled by
        ``process_expiry()`` when the subscription reaches its
        ``expires_at`` date, to avoid creating overlapping subscriptions.

        Returns the count of reminders sent.
        """
        count = 0
        now = _now_utc()
        threshold = (now + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db()
        try:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT id, user_id, plan_id, expires_at, auto_renew
                FROM subscriptions
                WHERE status = 'active'
                  AND expires_at > ?
                  AND expires_at <= ?
                """,
                (now_str, threshold),
            )
            expiring = cursor.fetchall()
        finally:
            conn.close()

        for sub in expiring:
            expires_dt = _parse_ts(sub["expires_at"]) or now + timedelta(days=3)
            plan_name = _get_plan_name(sub["plan_id"])
            auto_renew = sub.get("auto_renew")

            # P1.1: use notify_with_cooldown so each user gets at most one
            # reminder per 24h window. run_hourly_jobs calls this hourly;
            # without cooldown a user in the 3-day window would receive
            # up to 72 notifications.
            notif_sent = False
            try:
                from backend.services.notification_service import NotificationService

                if auto_renew:
                    result = NotificationService.notify_with_cooldown(
                        sub["user_id"],
                        type="subscription_expiring",
                        title="订阅即将自动续费 / Subscription Auto-Renewal Soon",
                        body=(
                            f"Your {plan_name} subscription will auto-renew on "
                            f"{expires_dt.strftime('%Y-%m-%d %H:%M UTC')}."
                        ),
                        metadata={
                            "subscription_id": sub["id"],
                            "plan_name": plan_name,
                            "expires_at": sub["expires_at"],
                        },
                        cooldown_hours=24,
                    )
                else:
                    result = NotificationService.notify_with_cooldown(
                        sub["user_id"],
                        type="subscription_expiring",
                        title="订阅即将到期 / Subscription Expiring Soon",
                        body=(
                            f"Your {plan_name} subscription expires on "
                            f"{expires_dt.strftime('%Y-%m-%d %H:%M UTC')}. "
                            f"Auto-renew is OFF."
                        ),
                        metadata={
                            "subscription_id": sub["id"],
                            "plan_name": plan_name,
                            "expires_at": sub["expires_at"],
                        },
                        cooldown_hours=24,
                    )
                notif_sent = result is not None
            except Exception:
                pass

            try:
                user_info = _get_user_email_info(sub["user_id"])
                if user_info:
                    from backend.services.email_service import EmailService

                    _fire_email(
                        EmailService.send_subscription_expiry(
                            email=user_info["email"],
                            username=user_info["username"],
                            plan_name=plan_name,
                            expires_at=expires_dt,
                        )
                    )
            except Exception:
                logger.exception("email send failed for upcoming expiry sub=%s", sub["id"])

            if notif_sent:
                count += 1

        return count

    # ------------------------------------------------------------------
    # Convenience: run all daily jobs
    # ------------------------------------------------------------------

    @staticmethod
    def run_daily_jobs() -> dict:
        """Process expiry + upcoming renewals + housekeeping sweeps.

        Designed to be called from a cron one-liner::

            python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_daily_jobs()"

        P1.4: each sub-task is wrapped in its own try/except so a single
        failure doesn't abort the whole run. Failed steps are recorded
        in the ``steps`` list and a WARNING alert is sent at the end if
        any step failed (in addition to the CRITICAL alert for a total
        failure that re-raises from the outer guard).
        """
        from backend.config import Config as _Cfg

        result: Dict[str, Any] = {}
        steps: list = []
        any_failed = False

        def _run_step(name: str, fn):
            nonlocal any_failed
            try:
                value = fn()
                result[name] = value
                steps.append({"step": name, "ok": True})
                return value
            except Exception as exc:
                any_failed = True
                logger.exception("run_daily_jobs step '%s' failed", name)
                result[name] = None
                steps.append({"step": name, "ok": False, "error": str(exc)})
                return None

        # 1. Subscription expiry + auto-renewal
        expired = _run_step("process_expiry", SubscriptionService.process_expiry)

        # 2. Upcoming renewal reminders
        renewed = _run_step("process_upcoming_renewals", SubscriptionService.process_upcoming_renewals)

        # 3. Soft-delete purge (30-day hard-delete promise)
        def _purge_soft_deleted():
            from backend.services.user_service import UserService
            return UserService.purge_soft_deleted_users()
        purged = _run_step("purge_soft_deleted", _purge_soft_deleted)

        # 4. Expired credits sweep
        def _sweep_credits():
            from backend.database import sweep_expired_credits
            return sweep_expired_credits()
        expired_credits = _run_step("sweep_expired_credits", _sweep_credits)

        # 5. P1.2: sweep old usage_logs (high-frequency write table).
        #    USAGE_LOG_RETENTION_DAYS is not yet in Config — default 90.
        def _sweep_usage_logs():
            from backend.database import sweep_old_usage_logs
            retention = int(getattr(_Cfg, "USAGE_LOG_RETENTION_DAYS", 90) or 90)
            return sweep_old_usage_logs(retention)
        usage_logs_swept = _run_step("sweep_old_usage_logs", _sweep_usage_logs)

        # 5b. Sweep old audit_logs (default 365 days). Without this the
        #     audit_logs table grows unbounded — every admin write adds
        #     a row and there is no other purge path that targets the
        #     database.sweep_old_audit_logs helper directly.
        def _sweep_audit_logs():
            from backend.database import sweep_old_audit_logs
            retention = int(getattr(_Cfg, "AUDIT_LOG_RETENTION_DAYS", 365) or 365)
            return sweep_old_audit_logs(retention)
        audit_logs_swept = _run_step("sweep_old_audit_logs", _sweep_audit_logs)

        # 5c. Sweep old conversations (default 90 days). conversations is
        #     user content (chat history) — kept bounded to preserve
        #     query performance on the chat sidebar.
        def _sweep_conversations():
            from backend.database import sweep_old_conversations
            retention = int(getattr(_Cfg, "CONVERSATION_RETENTION_DAYS", 90) or 90)
            return sweep_old_conversations(retention)
        conversations_swept = _run_step("sweep_old_conversations", _sweep_conversations)

        # 6. P1.2: sweep old read notifications (default 30 days).
        def _sweep_notifications():
            from backend.database import sweep_old_notifications
            retention = int(getattr(_Cfg, "NOTIFICATION_RETENTION_DAYS", 30) or 30)
            return sweep_old_notifications(retention)
        notifications_swept = _run_step("sweep_old_notifications", _sweep_notifications)

        # 6b. P1.7: sweep expired sessions (sessions table grows unbounded
        #     without active logout). Daily is enough — sessions have TTL.
        def _sweep_sessions():
            from backend.database import sweep_expired_sessions
            return sweep_expired_sessions()
        sessions_swept = _run_step("sweep_expired_sessions", _sweep_sessions)

        # 6c. P2.12: sweep stale rate_limits rows (window_start > 1h old).
        def _sweep_rate_limits():
            from backend.database import sweep_old_rate_limits
            return sweep_old_rate_limits()
        rate_limits_swept = _run_step("sweep_old_rate_limits", _sweep_rate_limits)

        # 6d. P3.3: deterministic sweep of idempotency_keys (>24h old).
        def _sweep_idempotency():
            from backend.database import sweep_old_idempotency_keys
            return sweep_old_idempotency_keys()
        idempotency_swept = _run_step("sweep_old_idempotency_keys", _sweep_idempotency)

        # 7. Purge expired redeem_codes (unused)
        def _purge_redeem_codes():
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    DELETE FROM redeem_codes
                    WHERE expires_at IS NOT NULL
                      AND expires_at < datetime('now')
                      AND id NOT IN (
                          SELECT DISTINCT redeem_code_id FROM redeem_code_usage
                      )
                    """
                )
                return int(cursor.rowcount or 0)
        redeem_codes_purged = _run_step("purge_redeem_codes", _purge_redeem_codes)

        # 8. Audit log purge
        def _purge_audit():
            from backend.services.audit import purge_old_audit_logs
            return purge_old_audit_logs(int(_Cfg.AUDIT_RETENTION_DAYS))
        audit_purged = _run_step("purge_audit_logs", _purge_audit)

        # 9. Stripe reconciliation
        def _stripe_recon():
            from backend.services.stripe_reconciliation import StripeReconciliation
            return StripeReconciliation.run_daily_reconciliation()
        stripe_recon = _run_step("stripe_reconciliation", _stripe_recon)
        if stripe_recon is None:
            stripe_recon = {"error": "unhandled exception"}
            result["stripe_recon"] = stripe_recon

        # 10. USDT (NOWPayments) reconciliation — recovers pending USDT
        # orders whose IPN webhook was missed. Mirrors the Stripe recon
        # step but queries NOWPayments GET /v1/payment/{id} instead.
        def _usdt_recon():
            from backend.services.usdt_reconciliation import USDTReconciliation
            return USDTReconciliation.run_daily_reconciliation()
        usdt_recon = _run_step("usdt_reconciliation", _usdt_recon)
        if usdt_recon is None:
            usdt_recon = {"error": "unhandled exception"}
            result["usdt_recon"] = usdt_recon

        # 11. SQLite WAL checkpoint — fold the WAL back into the main
        # DB file so the ``-wal`` sidecar doesn't grow unbounded on
        # long-running deployments. TRUNCATE mode is safe here because
        # the daily window is the maintenance slot.
        def _wal_checkpoint():
            from backend.database import run_wal_checkpoint

            return run_wal_checkpoint()

        wal_checkpoint = _run_step("wal_checkpoint", _wal_checkpoint)
        if wal_checkpoint is None:
            wal_checkpoint = {"error": "unhandled exception"}
            result["wal_checkpoint"] = wal_checkpoint

        logger.info(
            "daily subscription jobs: expired=%s renewed=%s purged_soft_deleted=%s "
            "expired_credits=%s usage_logs_swept=%s audit_logs_swept=%s "
            "conversations_swept=%s notifications_swept=%s "
            "sessions_swept=%s rate_limits_swept=%s idempotency_swept=%s "
            "redeem_codes_purged=%s audit_purged=%s stripe_recon=%s "
            "usdt_recon=%s failed_steps=%d",
            expired,
            renewed,
            purged,
            expired_credits,
            usage_logs_swept,
            audit_logs_swept,
            conversations_swept,
            notifications_swept,
            sessions_swept,
            rate_limits_swept,
            idempotency_swept,
            redeem_codes_purged,
            audit_purged,
            stripe_recon,
            usdt_recon,
            sum(1 for s in steps if not s["ok"]),
        )
        result["steps"] = steps

        # P1.4: aggregate alert if any sub-task failed (but don't re-raise —
        # the run is still partially successful).
        if any_failed:
            failed_names = [s["step"] for s in steps if not s["ok"]]
            try:
                from backend.services.alert_service import AlertService

                AlertService.send_alert_sync(
                    "WARNING",
                    f"run_daily_jobs completed with failed steps: {failed_names}",
                    metadata={"steps": steps},
                )
            except Exception:
                logger.exception("failed to send aggregate alert for run_daily_jobs")

        return result

    @staticmethod
    def run_hourly_jobs() -> dict:
        """Process expired orders, pending_payment subscriptions, expired
        subscriptions (with auto-renewal), and upcoming renewal notifications.
        Designed to be called hourly::

            python -c "from backend.services.subscription_service import SubscriptionService; SubscriptionService.run_hourly_jobs()"

        Each sub-task is wrapped in its own try/except so a single
        failure doesn't abort the whole run (mirrors the ``_run_step``
        pattern used by :meth:`run_daily_jobs`). Failed steps are
        recorded in the ``steps`` list and a WARNING alert is sent at
        the end if any step failed (in addition to the CRITICAL alert
        for a total failure that re-raises from the outer guard).
        """
        result: Dict[str, Any] = {}
        steps: list = []
        any_failed = False

        def _run_step(name: str, fn):
            nonlocal any_failed
            try:
                value = fn()
                result[name] = value
                steps.append({"step": name, "ok": True})
                return value
            except Exception as exc:
                any_failed = True
                logger.exception("run_hourly_jobs step '%s' failed", name)
                result[name] = None
                steps.append({"step": name, "ok": False, "error": str(exc)})
                return None

        try:
            from backend.database import purge_expired_reservations
            from backend.services import order_service

            # 1. Expire stale pending orders + roll back promo usage
            expired_orders = _run_step(
                "expired_orders", order_service.process_expired_orders
            )

            # 2. Expire pending_payment subscriptions past the 3-day grace
            expired_pending_subs = _run_step(
                "expired_pending_subs",
                order_service.process_pending_payment_subscriptions,
            )

            # 3. Process expired active subs (auto-renew or downgrade)
            expired_subs = _run_step(
                "expired_subs", SubscriptionService.process_expiry
            )

            # 4. Upcoming renewal reminders (within 3 days)
            renewed = _run_step(
                "renewed", SubscriptionService.process_upcoming_renewals
            )

            # 5. Sweep stale token reservations (TTL-based)
            purged_reservations = _run_step(
                "purged_reservations", purge_expired_reservations
            )

            logger.info(
                "hourly jobs: expired_orders=%s expired_pending_subs=%s expired_subs=%s renewed=%s purged_reservations=%s failed_steps=%d",
                expired_orders,
                expired_pending_subs,
                expired_subs,
                renewed,
                purged_reservations,
                sum(1 for s in steps if not s["ok"]),
            )
            result["steps"] = steps

            # Aggregate alert if any sub-task failed (but don't re-raise —
            # the run is still partially successful).
            if any_failed:
                failed_names = [s["step"] for s in steps if not s["ok"]]
                try:
                    from backend.services.alert_service import AlertService

                    AlertService.send_alert_sync(
                        "WARNING",
                        f"run_hourly_jobs completed with failed steps: {failed_names}",
                        metadata={"steps": steps},
                    )
                except Exception:
                    logger.exception(
                        "failed to send aggregate alert for run_hourly_jobs"
                    )

            return result
        except Exception as e:
            # H9: 后台作业失败时发送 CRITICAL 告警，便于运维及时感知。
            # 顶层兜底：捕获 _run_step 未覆盖的异常（如 import 失败、
            # _run_step 闭包自身的错误等）。
            logger.exception("run_hourly_jobs failed")
            try:
                from backend.services.alert_service import AlertService

                AlertService.send_alert_sync(
                    "CRITICAL",
                    f"run_hourly_jobs failed: {e}",
                )
            except Exception:
                logger.exception("failed to send alert for run_hourly_jobs failure")
            raise
