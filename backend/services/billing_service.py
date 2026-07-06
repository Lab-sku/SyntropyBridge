"""Billing service: pricing quotes, post-request charging, and refunds.

All amounts are in **credits**. Conversion rule (defined upstream in
``backend/database.py``) is roughly:
    1 元 ≈ 100 credits   (1 credit = 0.01 元 = 0.1 分)
    1 USD ≈ 700 credits

This module is intentionally side-effect free except for the explicit
``charge_for_usage`` and ``refund`` functions. The proxy / gateway code
is expected to call ``quote_cost`` first (cheap) and ``charge_for_usage``
only after a successful upstream response.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.database import (
    charge_for_usage_atomic,
    get_db,
    get_model_pricing,
    get_user_plan,
    update_wallet,
)
from backend.utils import idempotency

logger = logging.getLogger(__name__)


def _maybe_emit_low_balance(user_id: int) -> None:
    """Emit a low_balance notification if the wallet is below threshold
    and the user hasn't been notified in the last 24 hours.
    """
    LOW_BALANCE_THRESHOLD = 100.0
    try:
        from backend.database import get_db_context

        with get_db_context() as conn:
            cursor = conn.cursor()
            # Read current balance
            cursor.execute(
                "SELECT balance FROM wallets WHERE user_id = ?",
                (int(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                return
            balance = float(row["balance"] or 0)
            if balance >= LOW_BALANCE_THRESHOLD:
                return

            # Dedup: only emit if no low_balance notification in last 24h
            cursor.execute(
                """
                SELECT MAX(created_at) FROM notifications
                WHERE user_id = ? AND type = 'low_balance'
                """,
                (int(user_id),),
            )
            last_row = cursor.fetchone()
            last_notif = last_row[0] if last_row else None
            if last_notif:
                try:
                    from datetime import datetime, timedelta, timezone

                    if isinstance(last_notif, str):
                        last_dt = datetime.strptime(last_notif[:19], "%Y-%m-%d %H:%M:%S")
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elif isinstance(last_notif, datetime):
                        last_dt = (
                            last_notif
                            if last_notif.tzinfo
                            else last_notif.replace(tzinfo=timezone.utc)
                        )
                    else:
                        last_dt = None
                    if last_dt and (datetime.now(timezone.utc) - last_dt) < timedelta(hours=24):
                        return
                except Exception:
                    pass

            from backend.services.notification_service import NotificationService

            NotificationService.notify(
                int(user_id),
                type="low_balance",
                title="余额不足",
                body=f"您的钱包余额仅剩 {balance:.2f} credits, 请及时充值",
                metadata={"balance": balance, "threshold": LOW_BALANCE_THRESHOLD},
            )
    except Exception:
        logger.debug("low_balance notification check failed for user %s", user_id, exc_info=True)


def _maybe_trigger_auto_recharge(user_id: int) -> None:
    """After a wallet debit, check whether auto-recharge should fire.

    If the wallet has ``auto_recharge_enabled = 1`` and the balance has
    dropped below ``auto_recharge_threshold``, create a pending order for
    ``auto_recharge_amount`` credits. The order is always left pending —
    the user must complete payment via the normal checkout flow.

    Historically this function auto-approved the order when a Stripe
    customer id was on file, but that granted credits without actually
    charging the customer (``approve_order`` does not call the Stripe
    API). The auto-approve branch has been removed; the deprecated
    ``stripe_customer_{user_id}`` setting is now only logged for
    visibility.
    """
    try:
        from backend.database import get_db_context

        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT balance, auto_recharge_enabled, auto_recharge_threshold,
                       auto_recharge_amount
                FROM wallets WHERE user_id = ?
                """,
                (int(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                return

            if not row["auto_recharge_enabled"]:
                return

            balance = float(row["balance"] or 0)
            threshold = float(row["auto_recharge_threshold"] or 0)
            amount = float(row["auto_recharge_amount"] or 0)

            if threshold <= 0 or amount <= 0:
                return
            if balance >= threshold:
                return

            # Dedup: max 3 auto-recharge orders per 24h
            from datetime import datetime, timedelta, timezone

            now = datetime.now(timezone.utc)
            day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute(
                """
                SELECT COUNT(*) FROM orders
                WHERE user_id = ? AND payment_method = 'auto_recharge'
                  AND created_at >= ?
                """,
                (int(user_id), day_ago),
            )
            recent_count = int(cursor.fetchone()[0] or 0)
            if recent_count >= 3:
                return

            # Dedup: no pending auto-recharge order
            cursor.execute(
                """
                SELECT id FROM orders
                WHERE user_id = ? AND payment_method = 'auto_recharge'
                  AND status = 'pending'
                LIMIT 1
                """,
                (int(user_id),),
            )
            if cursor.fetchone():
                return

        # Create the pending order outside the read-lock context
        from backend.services import order_service

        order = order_service.create_order(
            user_id=int(user_id),
            amount=amount,
            payment_method="auto_recharge",
        )

        # Historically this code auto-approved the order when a Stripe
        # customer id was on file (``stripe_customer_{user_id}`` setting).
        # That path called ``approve_order`` which credits the wallet
        # WITHOUT actually charging the customer via Stripe — i.e. free
        # credits. We no longer auto-approve. The order stays pending so
        # the user completes payment through the normal checkout flow
        # (the auto_recharge_triggered notification already tells them
        # about it). The deprecated setting is logged for visibility.
        from backend.database import get_setting

        stripe_customer = get_setting(f"stripe_customer_{user_id}")
        if stripe_customer:
            logger.warning(
                "auto_recharge: deprecated stripe_customer_%s setting present "
                "but no longer triggers auto-approve (would grant uncharged "
                "credits). order %s left pending for manual checkout.",
                user_id, order.get("order_no"),
            )

        # Notification
        from backend.services.notification_service import NotificationService

        NotificationService.notify(
            int(user_id),
            type="auto_recharge_triggered",
            title="Auto-recharge triggered",
            body=f"Your wallet balance ({balance:.2f}) dropped below the threshold ({threshold:.2f}). "
            f"An order for {amount:.2f} credits has been created.",
            metadata={
                "balance": balance,
                "threshold": threshold,
                "amount": amount,
                "order_no": order.get("order_no"),
            },
        )
    except Exception:
        logger.debug("auto_recharge check failed for user %s", user_id, exc_info=True)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def quote_cost(
    user_id: int,
    provider: str,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Dict[str, Any]:
    """Quote the credit cost for a hypothetical request.

    The function never raises on missing pricing — unpriced models simply
    return zero cost so the proxy can still serve them.

    Returns
    -------
    dict with keys:
        cost_credits, input_price, output_price, discount_rate,
        plan_code, plan_name
    """
    pricing = get_model_pricing(provider, model_id) or {}
    in_price = float(pricing.get("input_price_per_1k") or 0.0)
    out_price = float(pricing.get("output_price_per_1k") or 0.0)

    plan = get_user_plan(user_id)
    discount = float(plan.get("discount_rate") or 1.0)

    p_tokens = float(prompt_tokens or 0)
    c_tokens = float(completion_tokens or 0)
    cost = (p_tokens / 1000.0) * in_price + (c_tokens / 1000.0) * out_price
    cost = round(cost * discount, 6)

    return {
        "cost_credits": float(cost),
        "input_price": in_price,
        "output_price": out_price,
        "discount_rate": discount,
        "plan_code": plan.get("code") or "free",
        "plan_name": plan.get("name") or "免费版",
    }


# ---------------------------------------------------------------------------
# Charging / Refunds
# ---------------------------------------------------------------------------


def _load_usage_log(usage_log_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db()
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, model, provider, prompt_tokens,
                   completion_tokens, cost_credits, error_message
            FROM usage_logs WHERE id = ?
        """,
            (usage_log_id,),
        )
        return cursor.fetchone()
    finally:
        conn.close()


def _write_back_cost(usage_log_id: int, cost: float, error_message: Optional[str]) -> None:
    """Persist the actual cost (and any failure note) onto the usage row."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        if error_message is not None:
            cursor.execute(
                """
                UPDATE usage_logs
                SET cost_credits = ?, error_message = ?
                WHERE id = ?
            """,
                (cost, error_message, usage_log_id),
            )
        else:
            cursor.execute(
                """
                UPDATE usage_logs
                SET cost_credits = ?
                WHERE id = ?
            """,
                (cost, usage_log_id),
            )
        conn.commit()
    finally:
        conn.close()


def _wallet_tx_already_charged(user_id: int, usage_log_id: int) -> bool:
    """Defense-in-depth: check whether a wallet_transactions consume row
    already exists for this (user_id, usage_log_id) pair.

    This catches edge cases where the idempotency store was wiped
    (retention expired) or the atomic helper's ``cost_credits > 0``
    check was bypassed by a direct SQL update, or two entry points
    (e.g., admin retry + automated cron) raced.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM wallet_transactions
            WHERE user_id = ?
              AND type = 'consume'
              AND related_type = 'usage'
              AND related_id = ?
            LIMIT 1
            """,
            (user_id, usage_log_id),
        )
        return cursor.fetchone() is not None
    except Exception:
        # Non-fatal: if the check fails we proceed to the atomic helper
        # which has its own idempotency guards.
        return False
    finally:
        conn.close()


def charge_for_usage(user_id: int, usage_log_id: int) -> bool:
    """Charge a user's wallet for a single usage_logs row.

    Reads the model's pricing, applies the user's plan discount, and
    atomically debits the wallet. When the wallet has insufficient
    balance, the function **does not raise** — it returns ``False`` and
    annotates the usage row with the failure reason. This way the proxy
    request is not blocked by billing errors.

    Idempotent: if the usage row already has a non-zero ``cost_credits``
    the function returns True without debiting the wallet again.

    Cross-request idempotency is provided by the idempotency store
    (``backend.utils.idempotency``), keyed on ``usage_log_id``. This
    prevents double-debit when the OpenAI SDK retries a network error
    that actually succeeded server-side.

    All write operations (idempotency check under lock, wallet debit,
    wallet_transactions insert, usage_logs write-back) are performed
    inside a single ``BEGIN IMMEDIATE`` transaction via
    :func:`charge_for_usage_atomic`, preventing double-spend and
    inconsistent-state race conditions.
    """
    # --- Cross-request idempotency (covers SDK retries) --------------------
    idem_key = f"charge:{usage_log_id}"
    idem_result = idempotency.check_or_reserve(
        key=idem_key,
        method="CHARGE",
        route="/billing/charge_for_usage",
        body={"user_id": user_id, "usage_log_id": usage_log_id},
    )
    if idem_result.hit:
        # Cached result: True or False stored by a previous invocation.
        return bool(idem_result.response_body)

    # --- Read-only: load usage log on a separate connection ---------------
    log = _load_usage_log(usage_log_id)
    if not log:
        logger.warning("charge_for_usage: usage_log %s not found", usage_log_id)
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=False,
        )
        return False
    if int(log.get("user_id") or 0) != int(user_id):
        logger.warning("charge_for_usage: user mismatch on log %s", usage_log_id)
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=False,
        )
        return False

    # --- Read-only: idempotency fast-path ---------------------------------
    existing_cost = float(log.get("cost_credits") or 0)
    if existing_cost > 0:
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=True,
        )
        return True

    # --- Read-only: compute the quote (may open its own connections) ------
    provider = log.get("provider") or "unknown"
    model = log.get("model") or ""
    quote = quote_cost(
        user_id=user_id,
        provider=provider,
        model_id=model,
        prompt_tokens=int(log.get("prompt_tokens") or 0),
        completion_tokens=int(log.get("completion_tokens") or 0),
    )
    cost = float(quote.get("cost_credits") or 0.0)

    # --- Defense-in-depth: wallet_transactions dedupe ----------------------
    # Catch edge cases where the idempotency store was wiped or the
    # atomic helper's cost_credits check was bypassed by a direct SQL
    # update or two entry points raced.
    if _wallet_tx_already_charged(user_id, usage_log_id):
        logger.info(
            "charge_for_usage: wallet_transactions dedupe hit for user %s, log %s",
            user_id,
            usage_log_id,
        )
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=True,
        )
        return True

    # --- Atomic: charge (or mark zero-cost) on a single connection --------
    result = charge_for_usage_atomic(
        user_id=user_id,
        usage_log_id=usage_log_id,
        cost=cost,
        note=f"{provider}/{model}",
    )

    if result in ("ok", "already_charged"):
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=True,
        )
        if result == "ok":
            _maybe_emit_low_balance(user_id)
            _maybe_trigger_auto_recharge(user_id)
        return True

    if result == "insufficient":
        logger.info(
            "charge_for_usage: insufficient balance for user %s, log %s (need %s)",
            user_id,
            usage_log_id,
            cost,
        )
        idempotency.finalize(
            key=idem_key,
            method="CHARGE",
            route="/billing/charge_for_usage",
            status_code=200,
            response_body=False,
        )
        return False

    # not_found, user_mismatch, or unexpected error
    logger.warning(
        "charge_for_usage: atomic charge returned %r for log %s",
        result,
        usage_log_id,
    )
    # Release the reservation so the client can retry if appropriate.
    idempotency.release(
        key=idem_key,
        method="CHARGE",
        route="/billing/charge_for_usage",
    )
    return False


def estimate_tokens(
    messages: Optional[list] = None,
    accumulated_text: str = "",
) -> tuple:
    prompt_tokens = 0
    completion_tokens = 0
    if messages:
        # Byte-length heuristic (UTF-8) approximate token counts far
        # better than character counts for CJK text: a single Chinese
        # character is ~1 token but 3 UTF-8 bytes, so char-based
        # estimation under-counts Chinese by ~3x. The byte-based
        # ``len(text.encode("utf-8")) // 4`` keeps ASCII roughly
        # accurate (4 bytes ≈ 1 token) and brings CJK within ~33%.
        prompt_tokens = (
            sum(
                len((m.get("content", "") or "").encode("utf-8"))
                for m in messages
            )
            // 4
        )
    if accumulated_text:
        completion_tokens = len(accumulated_text.encode("utf-8")) // 4
    return max(prompt_tokens, 1), max(completion_tokens, 1)


def _settle_over_reserve(
    *,
    user_id: int,
    delta: float,
    provider: str,
    model: str,
    actual_cost: float,
    cost_reserve: float,
) -> float:
    """Settle a streaming over-reserve (or no-reserve direct charge) by
    debiting whatever the wallet can cover and recording an audit trail
    for the uncovered remainder.

    Returns the shortfall (0.0 when the wallet covered the full delta).
    The shortfall is recorded in:

    - the ``note`` of the partial_settle ``wallet_transactions`` row
      (``pending_debit:short=X``), and
    - a row in ``audit_logs`` (action ``stream_pending_debit``) so
      operators can find uncollectable consumption.

    ``wallet_transactions`` enforces ``CHECK (amount != 0)``, so a
    zero-amount audit row is not storable there — the audit_logs table
    is the canonical home for that trail.
    """
    if delta >= 0:
        return 0.0
    needed = -delta  # positive amount we want to debit
    try:
        update_wallet(
            user_id,
            delta,
            "consume",
            related_type="stream_reconcile",
            related_id=None,
            note=f"stream over-reserve {provider}/{model}",
        )
        return 0.0
    except ValueError:
        # Wallet can't cover the full debit — settle what we can.
        from backend.database import get_db_context

        shortfall = 0.0
        post_balance = 0.0
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?",
                    (int(user_id),),
                )
                row = cursor.fetchone()
                current_balance = float(row[0] or 0) if row else 0.0
                debit = min(needed, current_balance)
                if debit > 0:
                    cursor.execute(
                        """
                        UPDATE wallets
                        SET balance = balance - ?,
                            total_consumed = total_consumed + ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ?
                        """,
                        (debit, debit, int(user_id)),
                    )
                post_balance = current_balance - debit
                shortfall = needed - debit
                note = (
                    f"partial_settle {provider}/{model}"
                    if shortfall <= 0
                    else (
                        f"partial_settle {provider}/{model} "
                        f"pending_debit:short={shortfall:.6f} "
                        f"actual={actual_cost:.6f} reserve={cost_reserve:.6f}"
                    )
                )
                if debit > 0:
                    cursor.execute(
                        """
                        INSERT INTO wallet_transactions
                            (user_id, type, amount, balance_after,
                             related_type, related_id, note,
                             expires_at, expiry_debited)
                        VALUES (?, 'consume', ?, ?, 'stream_reconcile', NULL, ?, NULL, 0)
                        """,
                        (
                            int(user_id),
                            -debit,
                            post_balance,
                            note,
                        ),
                    )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise

        # Audit-log the shortfall so operators can find it. Best-effort:
        # a failure here must not mask the successful partial settle.
        if shortfall > 0:
            try:
                from backend.services.audit import log_action

                log_action(
                    actor_id=-1,
                    actor_type="system",
                    action="stream_pending_debit",
                    target_type="user",
                    target_id=int(user_id),
                    details={
                        "user_id": int(user_id),
                        "provider": provider,
                        "model": model,
                        "actual_cost": float(actual_cost),
                        "cost_reserve": float(cost_reserve),
                        "debited": float(needed - shortfall),
                        "shortfall": float(shortfall),
                        "post_balance": float(post_balance),
                    },
                    ip_address=None,
                )
            except Exception:
                logger.debug(
                    "audit log write failed for stream_pending_debit user %s",
                    user_id,
                    exc_info=True,
                )
        return shortfall


def reconcile_stream_reserve(
    user_id: int,
    provider: str,
    model: str,
    cost_reserve: float,
    prompt_tokens: int,
    completion_tokens: int,
    usage_log_id=None,
    messages: Optional[list] = None,
    accumulated_text: str = "",
    stream_completed: bool = True,
) -> float:
    from backend.database import update_usage_log_cost

    if not stream_completed and (prompt_tokens == 0 and completion_tokens == 0):
        if accumulated_text or messages:
            est_prompt, est_completion = estimate_tokens(messages, accumulated_text)
            if prompt_tokens == 0:
                prompt_tokens = est_prompt
            if completion_tokens == 0:
                completion_tokens = est_completion
            logger.warning(
                "stream reconcile: usage chunk missing, estimated tokens "
                "prompt=%d completion=%d for user %s (%s/%s)",
                prompt_tokens, completion_tokens, user_id, provider, model,
            )

    actual_quote = quote_cost(user_id, provider, model, prompt_tokens, completion_tokens)
    actual_cost = float(actual_quote.get("cost_credits") or 0.0)

    if cost_reserve <= 0:
        if actual_cost > 0:
            # No reservation to reconcile against — try a direct debit.
            # If the wallet can't cover it, settle what we can and log
            # the shortfall as a pending_debit audit row instead of
            # silently swallowing the ValueError (which previously left
            # usage_logs.cost_credits recorded while the wallet was
            # untouched — platform ate the upstream cost).
            _shortfall = _settle_over_reserve(
                user_id=user_id,
                delta=-actual_cost,
                provider=provider,
                model=model,
                actual_cost=actual_cost,
                cost_reserve=0.0,
            )
            if _shortfall > 0:
                logger.warning(
                    "stream reconcile: pending_debit short=%.4f for user %s "
                    "direct charge cost %s (%s/%s)",
                    _shortfall, user_id, actual_cost, provider, model,
                )
        if actual_cost > 0 and usage_log_id:
            try:
                update_usage_log_cost(usage_log_id, actual_cost)
            except Exception:
                pass
        return actual_cost

    delta = cost_reserve - actual_cost
    if delta > 0.0001:
        try:
            # expires_at=None prevents expiry laundering: returning an
            # unused reservation must NOT reset the credit-entry TTL,
            # otherwise users could refresh expiring credits by running
            # pre-reserve → refund cycles.
            update_wallet(
                user_id,
                delta,
                "refund",
                related_type="stream_reconcile",
                related_id=None,
                note=f"stream unused reserve {provider}/{model}",
                expires_at=None,
            )
        except Exception:
            logger.warning(
                "stream reconcile: failed to refund unused reserve for user %s",
                user_id,
            )
    elif delta < -0.0001:
        # Over-reserve: actual_cost exceeded the reservation. Try to
        # debit the difference; if the wallet can't cover it, settle
        # what we can and log the shortfall as a pending_debit audit
        # row instead of silently swallowing the ValueError (which
        # previously left usage_logs.cost_credits recorded while the
        # wallet was untouched — platform ate the upstream cost).
        _shortfall = _settle_over_reserve(
            user_id=user_id,
            delta=delta,
            provider=provider,
            model=model,
            actual_cost=actual_cost,
            cost_reserve=cost_reserve,
        )
        if _shortfall > 0:
            logger.warning(
                "stream reconcile: pending_debit short=%.4f for user %s "
                "(actual %s > reserve %s, %s/%s)",
                _shortfall, user_id, actual_cost, cost_reserve, provider, model,
            )

    if actual_cost > 0 and usage_log_id:
        try:
            update_usage_log_cost(usage_log_id, actual_cost)
        except Exception:
            pass

    return actual_cost


def refund(
    user_id: int,
    amount: float,
    reason: str,
    idempotency_key: Optional[str] = None,
) -> bool:
    """Add ``amount`` credits to a user's wallet, logging the reason.

    When *idempotency_key* is provided, the function checks the idempotency
    store first. If a cached result exists for the same key+body, the cached
    result is returned without re-executing the wallet update, preventing
    double-credit on retries.

    Returns True on success, False on validation / persistence failure.
    """
    if amount is None or float(amount) <= 0:
        return False

    # --- Cross-request idempotency ----------------------------------------
    idem_key = idempotency_key or ""
    if idem_key:
        idem_result = idempotency.check_or_reserve(
            key=idem_key,
            method="POST",
            route="/billing/refund",
            body={"user_id": user_id, "amount": float(amount), "reason": reason},
        )
        if idem_result.hit:
            return bool(idem_result.response_body)

    try:
        update_wallet(
            user_id,
            float(amount),
            "refund",
            related_type="admin",
            related_id=None,
            note=reason or "refund",
        )
        if idem_key:
            idempotency.finalize(
                key=idem_key,
                method="POST",
                route="/billing/refund",
                status_code=200,
                response_body=True,
            )
        return True
    except ValueError as exc:
        logger.warning("refund: validation error for user %s: %s", user_id, exc)
        if idem_key:
            idempotency.finalize(
                key=idem_key,
                method="POST",
                route="/billing/refund",
                status_code=400,
                response_body=False,
            )
        return False
    except Exception:
        logger.exception("refund: failed for user %s amount %s", user_id, amount)
        if idem_key:
            idempotency.release(
                key=idem_key,
                method="POST",
                route="/billing/refund",
            )
        return False
