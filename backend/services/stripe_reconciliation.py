"""Daily reconciliation between Stripe Checkout Sessions and local orders.

Webhooks occasionally fail to deliver (network blips, Stripe giving up
after retries). A paid session whose ``checkout.session.completed``
event we missed leaves the local order stuck in ``pending`` while the
user has already been charged. This worker runs once a day, pulls
every paid session from the last ``STRIPE_RECON_LOOKBACK_HOURS``, and
recovers the ones whose local orders are still ``pending``.

Mirrors the existing mismatch / approval logic in
``backend/routes/billing.py::stripe_webhook`` so the behaviour stays
consistent regardless of which path approves the order.

Invocation::

    python -c "from backend.services.stripe_reconciliation import StripeReconciliation; print(StripeReconciliation.run_daily_reconciliation())"

or via :meth:`SubscriptionService.run_daily_jobs`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config knobs (read lazily so tests can monkeypatch env vars after import)
# ---------------------------------------------------------------------------


def _config():
    from backend.config import Config

    return {
        "enabled": bool(getattr(Config, "STRIPE_RECON_ENABLED", True)),
        "lookback_hours": int(getattr(Config, "STRIPE_RECON_LOOKBACK_HOURS", 48) or 48),
        "max_auto_approve": int(getattr(Config, "STRIPE_RECON_MAX_AUTO_APPROVE", 50) or 50),
        "amount_tolerance": float(getattr(Config, "STRIPE_RECON_AMOUNT_TOLERANCE", 0.01) or 0.01),
        "stripe_secret_key": getattr(Config, "STRIPE_SECRET_KEY", None)
        or os.getenv("STRIPE_SECRET_KEY"),
        "usdt_rate": float(getattr(Config, "NOWPAYMENTS_CNY_USDT_RATE", 0.0) or 0.0),
    }


import os  # noqa: E402  (imported after _config to keep the helper compact)


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class StripeReconciliation:
    """Batch-reconcile Stripe Checkout Sessions against local orders."""

    # A synthetic admin_id used on audit rows for system-driven
    # approvals. Keeps the ``approved_by`` column informative (NULL
    # would suggest "approved by nobody").
    _SYSTEM_ADMIN_ID = -1

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @staticmethod
    def run_daily_reconciliation() -> Dict[str, Any]:
        """Scan Stripe sessions from the lookback window and recover any
        local orders that were paid on Stripe but are still ``pending``
        in our DB.

        Returns a counters dict. When the reconciler is disabled or
        Stripe is unconfigured it returns ``{"skipped": True,
        "reason": "..."}`` without making any API call.
        """
        cfg = _config()
        if not cfg["enabled"]:
            return {"skipped": True, "reason": "STRIPE_RECON_ENABLED=false"}
        if not cfg["stripe_secret_key"]:
            return {"skipped": True, "reason": "STRIPE_SECRET_KEY not set"}

        try:
            import stripe
        except ImportError as exc:
            logger.warning("stripe SDK not installed: %s", exc)
            return {"skipped": True, "reason": "stripe SDK missing"}

        stripe.api_key = cfg["stripe_secret_key"]

        cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["lookback_hours"])
        cutoff_ts = int(cutoff.timestamp())

        result = {
            "sessions_scanned": 0,
            "auto_approved": 0,
            "pending_review": 0,
            "orphans": 0,
            "late_payments": 0,
            "no_ops": 0,
            "errors": [],
        }

        try:
            sessions = StripeReconciliation._list_paid_sessions(stripe, cutoff_ts)
        except Exception as exc:
            logger.exception("Stripe session.list failed")
            result["errors"].append(f"session.list: {exc}")
            # P2.4: 顶层 Stripe API 失败意味着当日对账完全跳过，未匹配
            # 的支付只能等下一次 webhook 重试或人工介入。属于 WARNING
            # 级告警 —— 不像资金直接损失那样紧急，但需运维尽快介入。
            try:
                from backend.services.alert_service import AlertService

                AlertService.send_alert_sync(
                    level="WARNING",
                    message="Stripe daily reconciliation failed at session.list",
                    metadata={
                        "lookback_hours": cfg["lookback_hours"],
                        "error": str(exc),
                    },
                )
            except Exception:
                logger.debug("AlertService.send_alert_sync failed", exc_info=True)
            return result

        result["sessions_scanned"] = len(sessions)

        # Deduplicate sessions by order_no — two sessions for the same
        # order (user clicked "Pay" twice) should only approve once.
        seen_order_nos: set = set()
        for session in sessions:
            order_no = (session.get("metadata") or {}).get("order_no") or ""
            if not order_no:
                # No order_no — can't correlate with a local order.
                result["orphans"] += 1
                continue
            if order_no in seen_order_nos:
                # Duplicate paid session for the same order — skip.
                # approve_order's idempotency guard would also catch
                # this, but skipping is cheaper.
                continue
            seen_order_nos.add(order_no)

            # Safety cap on auto-approvals per run. Beyond this point
            # something is catastrophically wrong — surface the rest
            # for human review.
            if result["auto_approved"] >= cfg["max_auto_approve"]:
                result["errors"].append(
                    f"max auto-approve cap ({cfg['max_auto_approve']}) reached; "
                    f"remaining sessions deferred to next run"
                )
                break

            try:
                StripeReconciliation._process_session(session, cfg, result)
            except Exception as exc:
                logger.exception("reconcile failed for session %s", session.get("id"))
                result["errors"].append(f"{session.get('id')}: {exc}")

        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _list_paid_sessions(stripe_module, cutoff_ts: int) -> List[Dict[str, Any]]:
        """Pull every paid Checkout Session created after ``cutoff_ts``.

        Stripe's ``session.list`` endpoint is cursor-paginated with
        ``has_more`` + ``starting_after``. We page until exhaustion —
        on a low-volume platform this is typically 1-2 requests.
        """
        sessions: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {
            "created": {"gte": cutoff_ts},
            "limit": 100,
        }
        while True:
            page = stripe_module.checkout.Session.list(**params)
            data = page.get("data", []) if hasattr(page, "get") else getattr(page, "data", [])
            for s in data:
                # Only interested in paid sessions — unpaid / expired
                # are the webhook's problem, not ours.
                if (s.get("payment_status") or "") == "paid":
                    sessions.append(s)
            has_more = page.get("has_more") if hasattr(page, "get") else getattr(page, "has_more", False)
            if not has_more:
                break
            last = data[-1]
            params["starting_after"] = last.get("id") if hasattr(last, "get") else getattr(last, "id")
        return sessions

    @staticmethod
    def _process_session(
        session: Dict[str, Any],
        cfg: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        from backend.database import get_db_context
        from backend.services import order_service

        order_no = (session.get("metadata") or {}).get("order_no") or ""
        session_id = session.get("id") or ""
        payment_intent = session.get("payment_intent") or ""

        amount_total = int(session.get("amount_total") or 0)
        paid_amount = float(amount_total) / 100.0
        currency = (session.get("currency") or "").lower()

        # Look up the local order by order_no (metadata) first, then
        # fall back to payment_session_id for orders where the metadata
        # wasn't persisted.
        with get_db_context() as conn:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, order_no, user_id, amount, credits, bonus_credits, status,
                       payment_session_id, payment_reference, payment_provider
                FROM orders
                WHERE order_no = ?
                LIMIT 1
                """,
                (order_no,),
            )
            order = cursor.fetchone()
            if not order and session_id:
                cursor.execute(
                    """
                    SELECT id, order_no, user_id, amount, credits, bonus_credits, status,
                           payment_session_id, payment_reference, payment_provider
                    FROM orders
                    WHERE payment_session_id = ?
                    LIMIT 1
                    """,
                    (session_id,),
                )
                order = cursor.fetchone()

        if not order:
            # Paid on Stripe but no matching local order — orphan.
            # Never auto-approve: this needs a human to investigate.
            logger.warning(
                "stripe_recon orphan: session=%s order_no=%s paid=%.2f %s",
                session_id,
                order_no,
                paid_amount,
                currency,
            )
            orphan_details = {
                "session_id": session_id,
                "order_no": order_no,
                "paid_amount": paid_amount,
                "currency": currency,
            }
            StripeReconciliation._audit(
                "stripe_recon.orphan",
                target_type="checkout_session",
                target_id=None,
                details=orphan_details,
            )
            # CRITICAL — could be an attack, a misconfigured webhook,
            # or a Stripe dashboard session created outside our flow.
            StripeReconciliation._alert(
                "CRITICAL",
                f"Stripe reconciliation orphan: session={session_id} order_no={order_no} paid={paid_amount:.2f} {currency}",
                orphan_details,
            )
            result["orphans"] += 1
            return

        order_id = int(order["id"])
        order_status = order.get("status") or ""

        # Already terminal — no work to do, but backfill payment_reference
        # if it's missing (the webhook handler's second UPDATE may have
        # failed on a transient DB lock).
        if order_status in ("paid", "refunded"):
            if not order.get("payment_reference") and payment_intent:
                with get_db_context() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE orders SET payment_reference = ? WHERE id = ?",
                        (payment_intent, order_id),
                    )
                    conn.commit()
            result["no_ops"] += 1
            return

        if order_status == "pending_review":
            # Already flagged for human review — leave it alone.
            result["no_ops"] += 1
            return

        if order_status in ("expired", "cancelled", "failed"):
            # Late payment — Stripe captured the charge AFTER our local
            # expiry window closed. Needs a human to decide whether to
            # refund or reactivate.
            logger.warning(
                "stripe_recon late payment: order=%s status=%s session=%s paid=%.2f",
                order["order_no"],
                order_status,
                session_id,
                paid_amount,
            )
            late_details = {
                "order_no": order["order_no"],
                "status": order_status,
                "session_id": session_id,
                "paid_amount": paid_amount,
            }
            StripeReconciliation._audit(
                "stripe_recon.late_payment",
                target_type="order",
                target_id=order_id,
                details=late_details,
            )
            # WARNING — the user paid but the order expired; needs human
            # decision (refund vs reactivate) but isn't an attack signal.
            StripeReconciliation._alert(
                "WARNING",
                f"Stripe reconciliation late payment: order={order['order_no']} status={order_status} paid={paid_amount:.2f}",
                late_details,
            )
            result["late_payments"] += 1
            return

        if order_status != "pending":
            # Unknown state — don't touch.
            result["no_ops"] += 1
            return

        # Order is still pending — compare amounts and either approve or
        # route to pending_review.
        expected_amount = float(order.get("amount") or 0)
        # USDT orders: Stripe's USDT flow reports amount_total in USDT
        # minor units; convert the CNY-denominated order amount using
        # the operator-configured rate so the comparison is apples-to-
        # apples. Other currencies: compare as-is (operator's risk).
        if (order.get("payment_provider") == "usdt") and cfg["usdt_rate"] > 0:
            expected_amount = expected_amount * cfg["usdt_rate"]

        tolerance = cfg["amount_tolerance"]
        if abs(paid_amount - expected_amount) > tolerance and expected_amount > 0:
            StripeReconciliation._route_to_pending_review(
                order, paid_amount, expected_amount, session_id
            )
            result["pending_review"] += 1
            return

        # Amounts match — auto-approve.
        try:
            ok = order_service.approve_order(
                order_id, admin_id=StripeReconciliation._SYSTEM_ADMIN_ID
            )
        except Exception as exc:
            logger.exception("approve_order failed for order %s", order_id)
            result["errors"].append(f"approve_order({order_id}): {exc}")
            return

        if not ok:
            # approve_order returned False (idempotency guard: order
            # was no longer pending by the time we got there). Treat
            # as a no-op.
            result["no_ops"] += 1
            return

        # Backfill payment_reference so subsequent lookups (refunds,
        # Stripe Dashboard correlation) work.
        if payment_intent:
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE orders SET payment_reference = ? WHERE id = ?",
                    (payment_intent, order_id),
                )
                conn.commit()

        StripeReconciliation._audit(
            "stripe_recon.auto_approve",
            target_type="order",
            target_id=order_id,
            details={
                "order_no": order["order_no"],
                "session_id": session_id,
                "paid_amount": paid_amount,
                "currency": currency,
            },
        )
        result["auto_approved"] += 1

    @staticmethod
    def _route_to_pending_review(
        order: Dict[str, Any],
        paid_amount: float,
        expected_amount: float,
        session_id: str,
    ) -> None:
        """Flag an amount-mismatch order for human review — same shape
        as the webhook mismatch branch in billing.py."""
        from backend.database import get_db_context

        shortfall = expected_amount - paid_amount
        mismatch_note = json.dumps(
            {
                "amount_mismatch": True,
                "expected": expected_amount,
                "paid": paid_amount,
                "shortfall": shortfall,
                "source": "stripe_recon",
                "session_id": session_id,
            }
        )
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE orders SET status = 'pending_review', note = COALESCE(note, '') || ? WHERE id = ?",
                    (mismatch_note, int(order["id"])),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

        logger.critical(
            "stripe_recon AMOUNT MISMATCH: order %s expected %.4f paid %.4f (shortfall=%.4f)",
            order.get("order_no"),
            expected_amount,
            paid_amount,
            shortfall,
        )
        mismatch_details = {
            "order_no": order.get("order_no"),
            "session_id": session_id,
            "expected": expected_amount,
            "paid": paid_amount,
            "shortfall": shortfall,
        }
        StripeReconciliation._audit(
            "stripe_recon.amount_mismatch",
            target_type="order",
            target_id=int(order["id"]),
            details=mismatch_details,
        )
        # WARNING — the customer paid the wrong amount; route to
        # pending_review for a human to decide (refund / partial-credit).
        StripeReconciliation._alert(
            "WARNING",
            f"Stripe reconciliation amount mismatch: order={order.get('order_no')} expected={expected_amount:.4f} paid={paid_amount:.4f} shortfall={shortfall:.4f}",
            mismatch_details,
        )

        # Notify the user so they know their payment is being reviewed.
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                int(order["user_id"]),
                type="order_pending_review",
                title="订单金额待审核",
                body=(
                    f"订单 {order.get('order_no')} 支付金额与订单金额不一致，"
                    f"管理员将尽快审核处理。"
                ),
                metadata={
                    "order_no": order.get("order_no"),
                    "paid": paid_amount,
                    "expected": expected_amount,
                },
            )
        except Exception:
            logger.debug("notification for pending_review order failed", exc_info=True)

    @staticmethod
    def _audit(
        action: str,
        *,
        target_type: Optional[str],
        target_id: Optional[int],
        details: Optional[Dict[str, Any]],
    ) -> None:
        """Best-effort audit write. Never raises — reconciliation must
        keep going even when the audit_logs table is momentarily busy."""
        try:
            from backend.services.audit import log_action

            log_action(
                actor_id=StripeReconciliation._SYSTEM_ADMIN_ID,
                actor_type="system",
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        except Exception:
            logger.debug("audit log failed for %s", action, exc_info=True)

    @staticmethod
    def _alert(level: str, message: str, details: Optional[Dict[str, Any]]) -> None:
        """Best-effort push to AlertService so super-admins see orphan /
        late_payment / amount_mismatch events in real time instead of
        having to scrape audit_logs. Never raises — reconciliation must
        keep going even when the alert channel is down."""
        try:
            from backend.services.alert_service import AlertService

            AlertService.send_alert_sync(level, message, details)
        except Exception:
            logger.debug("alert send failed for %s", message, exc_info=True)
