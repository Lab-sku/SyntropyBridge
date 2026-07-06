"""Daily reconciliation between NOWPayments (USDT/crypto) and local orders.

NOWPayments IPN webhooks are best-effort: they can be delayed, dropped,
or arrive out of order. A paid payment whose IPN we missed leaves the
local order stuck in ``pending`` while the user has already paid on
chain. This worker runs once a day, queries NOWPayments
``GET /v1/payment/{payment_id}`` for every pending USDT order in the
lookback window, and recovers the ones that have since been confirmed.

Mirrors the logic in :class:`backend.services.stripe_reconciliation.StripeReconciliation`
and the IPN webhook handler in :mod:`backend.routes.billing`.

Invocation::

    python -c "from backend.services.usdt_reconciliation import USDTReconciliation; print(USDTReconciliation.run_daily_reconciliation())"

or via :meth:`SubscriptionService.run_daily_jobs`.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.database import get_db_context
from backend.services.payment.usdt_provider import UsdtProvider, _STATUS_MAP

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config knobs (read lazily so tests can monkeypatch env vars after import)
# ---------------------------------------------------------------------------


def _config() -> Dict[str, Any]:
    from backend.config import Config

    return {
        "enabled": bool(getattr(Config, "USDT_RECON_ENABLED", True)),
        "lookback_hours": int(getattr(Config, "USDT_RECON_LOOKBACK_HOURS", 48) or 48),
        "max_auto_approve": int(getattr(Config, "USDT_RECON_MAX_AUTO_APPROVE", 50) or 50),
        "amount_tolerance": float(
            getattr(Config, "STRIPE_RECON_AMOUNT_TOLERANCE", 0.01) or 0.01
        ),
        "usdt_rate": float(getattr(Config, "NOWPAYMENTS_CNY_USDT_RATE", 0.0) or 0.0),
        "nowpayments_api_key": (os.getenv("NOWPAYMENTS_API_KEY") or "").strip(),
        "nowpayments_api_base": "https://api.nowpayments.io/v1",
    }


class USDTReconciliation:
    """Batch-reconcile NOWPayments crypto payments against local orders.

    The reconciler walks every ``orders`` row that:

    * has ``payment_provider='usdt'``
    * has ``status='pending'``
    * was created within the lookback window (default 48h)

    For each it calls ``GET /v1/payment/{payment_id}`` and applies the
    same decision tree as the IPN webhook:

    * ``succeeded`` + amount match → ``approve_order``
    * ``succeeded`` + mismatch → ``pending_review`` + notify + alert
    * ``partial``      → ``pending_review`` + notify + alert
    * ``failed``       → mark order ``failed`` + notify user
    * ``pending``      → no-op (still waiting on chain confirmation)
    """

    _SYSTEM_ADMIN_ID = -1
    _API_BASE = "https://api.nowpayments.io/v1"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @staticmethod
    def run_daily_reconciliation() -> Dict[str, Any]:
        """Scan pending USDT orders and reconcile them against NOWPayments.

        Returns a counters dict. When the reconciler is disabled or
        NOWPayments is unconfigured it returns ``{"skipped": True,
        "reason": "..."}`` without making any API call.
        """
        cfg = _config()
        if not cfg["enabled"]:
            return {"skipped": True, "reason": "USDT_RECON_ENABLED=false"}
        if not cfg["nowpayments_api_key"]:
            return {"skipped": True, "reason": "NOWPAYMENTS_API_KEY not set"}

        result: Dict[str, Any] = {
            "orders_scanned": 0,
            "auto_approved": 0,
            "pending_review": 0,
            "failed": 0,
            "no_ops": 0,
            "errors": [],
        }

        # Pull all pending USDT orders in the lookback window.
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=cfg["lookback_hours"])
        ).strftime("%Y-%m-%d %H:%M:%S")

        try:
            orders = USDTReconciliation._list_pending_usdt_orders(cutoff)
        except Exception as exc:
            logger.exception("usdt_recon list pending orders failed")
            result["errors"].append(f"list_pending: {exc}")
            USDTReconciliation._alert(
                "WARNING",
                "USDT daily reconciliation failed at pending-orders lookup",
                {"error": str(exc)},
            )
            return result

        result["orders_scanned"] = len(orders)
        if not orders:
            return result

        headers = {
            "Accept": "application/json",
            "x-api-key": cfg["nowpayments_api_key"],
        }

        for order in orders:
            if result["auto_approved"] >= cfg["max_auto_approve"]:
                result["errors"].append(
                    f"max auto-approve cap ({cfg['max_auto_approve']}) reached; "
                    f"remaining orders deferred to next run"
                )
                break

            payment_id = (order.get("payment_session_id") or "").strip()
            if not payment_id:
                # No payment_id recorded — can't query NOWPayments.
                # Surface as a no-op so the operator notices via the
                # audit_logs / admin UI rather than silently skipping.
                USDTReconciliation._audit(
                    "usdt_recon.missing_payment_id",
                    target_type="order",
                    target_id=int(order["id"]),
                    details={"order_no": order.get("order_no")},
                )
                result["no_ops"] += 1
                continue

            try:
                payment = USDTReconciliation._query_nowpayments(
                    payment_id, headers, cfg
                )
            except Exception as exc:
                logger.exception(
                    "usdt_recon query failed for order=%s payment=%s",
                    order.get("order_no"),
                    payment_id,
                )
                result["errors"].append(
                    f"{order.get('order_no')}: query failed: {exc}"
                )
                continue

            if payment is None:
                # 404 / network error — treat as no-op, retry next run.
                result["no_ops"] += 1
                continue

            try:
                USDTReconciliation._process_payment(order, payment, cfg, result)
            except Exception as exc:
                logger.exception(
                    "usdt_recon process failed for order=%s",
                    order.get("order_no"),
                )
                result["errors"].append(
                    f"{order.get('order_no')}: {exc}"
                )

        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _list_pending_usdt_orders(cutoff_str: str) -> List[Dict[str, Any]]:
        """Return all pending USDT orders created after ``cutoff_str``."""
        with get_db_context() as conn:
            conn.row_factory = lambda c, r: dict(
                zip([col[0] for col in c.description], r)
            )
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, order_no, user_id, amount, credits, bonus_credits,
                       status, payment_session_id, payment_reference,
                       payment_provider, note
                FROM orders
                WHERE payment_provider = 'usdt'
                  AND status = 'pending'
                  AND created_at >= ?
                ORDER BY id ASC
                """,
                (cutoff_str,),
            )
            return list(cursor.fetchall())

    @staticmethod
    def _query_nowpayments(
        payment_id: str,
        headers: Dict[str, str],
        cfg: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Query NOWPayments for a single payment. Returns ``None`` on
        404 or transient network error (treated as no-op)."""
        url = f"{cfg['nowpayments_api_base']}/payment/{payment_id}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("NOWPayments query HTTP error for %s: %s", payment_id, exc)
            return None

        if resp.status_code == 404:
            logger.warning(
                "NOWPayments 404 for payment_id=%s — likely a stale session",
                payment_id,
            )
            return None
        if resp.status_code >= 400:
            logger.warning(
                "NOWPayments query %s for payment_id=%s: %s",
                resp.status_code,
                payment_id,
                resp.text,
            )
            return None

        try:
            return resp.json()
        except ValueError:
            logger.warning("NOWPayments returned invalid JSON for %s", payment_id)
            return None

    @staticmethod
    def _process_payment(
        order: Dict[str, Any],
        payment: Dict[str, Any],
        cfg: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Apply the same decision tree as the IPN webhook handler."""
        from backend.services import order_service

        status_code = (payment.get("payment_status") or "").lower()
        internal_status = _STATUS_MAP.get(status_code, "pending")

        order_id = int(order["id"])
        order_no = order.get("order_no") or ""
        user_id = int(order["user_id"])

        # Still waiting on chain confirmation — no-op.
        if internal_status == "pending":
            result["no_ops"] += 1
            return

        # Success path — verify amount, then approve.
        if internal_status == "succeeded":
            paid_amount = float(payment.get("actually_paid") or 0.0)
            # NOWPayments reports actually_paid in the pay_currency
            # (e.g. USDT). Convert to a CNY-equivalent using the
            # operator-configured rate so the comparison is
            # apples-to-apples with the order amount (CNY).
            expected_amount = float(order.get("amount") or 0)
            if cfg["usdt_rate"] > 0:
                paid_amount_cny = paid_amount / cfg["usdt_rate"]
            else:
                # No rate configured — compare in the original currency.
                # Operator absorbs the FX risk.
                paid_amount_cny = paid_amount

            tolerance = cfg["amount_tolerance"]
            if (
                expected_amount > 0
                and abs(paid_amount_cny - expected_amount) > tolerance
            ):
                USDTReconciliation._route_to_pending_review(
                    order, paid_amount_cny, expected_amount, payment, cfg
                )
                result["pending_review"] += 1
                return

            # Amounts match — auto-approve.
            try:
                ok = order_service.approve_order(
                    order_id, admin_id=USDTReconciliation._SYSTEM_ADMIN_ID
                )
            except Exception as exc:
                logger.exception(
                    "usdt_recon approve_order failed for order=%s", order_id
                )
                result["errors"].append(f"approve_order({order_id}): {exc}")
                return

            if not ok:
                result["no_ops"] += 1
                return

            # Backfill payment_reference so admin/refund paths can find it.
            payment_reference = str(payment.get("payment_id") or "")
            if payment_reference:
                with get_db_context() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE orders SET payment_reference = ? WHERE id = ?",
                        (payment_reference, order_id),
                    )
                    conn.commit()

            USDTReconciliation._audit(
                "usdt_recon.auto_approve",
                target_type="order",
                target_id=order_id,
                details={
                    "order_no": order_no,
                    "payment_id": payment_reference,
                    "paid_amount": paid_amount,
                },
            )
            result["auto_approved"] += 1
            return

        # Partial payment — route to pending_review (same as webhook).
        if internal_status == "partial":
            USDTReconciliation._route_to_pending_review(
                order,
                float(payment.get("actually_paid") or 0.0),
                float(order.get("amount") or 0),
                payment,
                cfg,
                partial=True,
            )
            result["pending_review"] += 1
            return

        # Failed / expired / refunded — mark order failed + notify user.
        if internal_status == "failed":
            USDTReconciliation._mark_order_failed(order, payment, result)
            return

        # Unknown status — no-op.
        result["no_ops"] += 1

    # ------------------------------------------------------------------

    @staticmethod
    def _route_to_pending_review(
        order: Dict[str, Any],
        paid_amount: float,
        expected_amount: float,
        payment: Dict[str, Any],
        cfg: Dict[str, Any],
        *,
        partial: bool = False,
    ) -> None:
        """Flag an amount-mismatch / partial order for human review."""
        import json as _json

        shortfall = expected_amount - paid_amount
        payment_id = str(payment.get("payment_id") or "")
        note_payload = _json.dumps(
            {
                "amount_mismatch": True,
                "partial": partial,
                "expected": expected_amount,
                "paid": paid_amount,
                "shortfall": shortfall,
                "source": "usdt_recon",
                "payment_id": payment_id,
            }
        )
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE orders SET status = 'pending_review', note = COALESCE(note, '') || ? WHERE id = ?",
                    (note_payload, int(order["id"])),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

        logger.critical(
            "usdt_recon %s: order=%s expected=%.4f paid=%.4f shortfall=%.4f",
            "PARTIAL" if partial else "AMOUNT MISMATCH",
            order.get("order_no"),
            expected_amount,
            paid_amount,
            shortfall,
        )
        details = {
            "order_no": order.get("order_no"),
            "payment_id": payment_id,
            "expected": expected_amount,
            "paid": paid_amount,
            "shortfall": shortfall,
            "partial": partial,
        }
        USDTReconciliation._audit(
            "usdt_recon.amount_mismatch",
            target_type="order",
            target_id=int(order["id"]),
            details=details,
        )
        USDTReconciliation._alert(
            "WARNING",
            (
                f"USDT reconciliation {'partial' if partial else 'amount mismatch'}: "
                f"order={order.get('order_no')} expected={expected_amount:.4f} "
                f"paid={paid_amount:.4f} shortfall={shortfall:.4f}"
            ),
            details,
        )

        # Notify the user.
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
    def _mark_order_failed(
        order: Dict[str, Any],
        payment: Dict[str, Any],
        result: Dict[str, Any],
    ) -> None:
        """Mark an order failed when NOWPayments reports the payment as
        failed / expired / refunded. Notify the user so they know to
        retry or contact support."""
        payment_id = str(payment.get("payment_id") or "")
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE orders SET status = 'failed', payment_reference = COALESCE(?, payment_reference) WHERE id = ?",
                    (payment_id or None, int(order["id"])),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

        USDTReconciliation._audit(
            "usdt_recon.failed",
            target_type="order",
            target_id=int(order["id"]),
            details={
                "order_no": order.get("order_no"),
                "payment_id": payment_id,
            },
        )
        try:
            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                int(order["user_id"]),
                type="order_failed",
                title="订单支付失败",
                body=(
                    f"订单 {order.get('order_no')} 的加密支付已失败或过期，"
                    f"请重新发起支付或联系客服。"
                ),
                metadata={"order_no": order.get("order_no")},
            )
        except Exception:
            logger.debug("notification for failed order failed", exc_info=True)

        result["failed"] += 1

    @staticmethod
    def _audit(
        action: str,
        *,
        target_type: Optional[str],
        target_id: Optional[int],
        details: Optional[Dict[str, Any]],
    ) -> None:
        try:
            from backend.services.audit import log_action

            log_action(
                actor_id=USDTReconciliation._SYSTEM_ADMIN_ID,
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
        try:
            from backend.services.alert_service import AlertService

            AlertService.send_alert_sync(level, message, details)
        except Exception:
            logger.debug("alert send failed for %s", message, exc_info=True)
