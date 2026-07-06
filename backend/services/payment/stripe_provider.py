"""Stripe payment provider implementation.

Uses the official ``stripe`` Python SDK (``stripe>=10.0.0``) to create
Checkout Sessions and verify webhook events.

Configuration
-------------
Two environment variables are required:

- ``STRIPE_SECRET_KEY`` — Stripe API secret key (``sk_test_...`` or
  ``sk_live_...``).
- ``STRIPE_WEBHOOK_SECRET`` — the signing secret for the webhook
  endpoint (``whsec_...``). Set in the Stripe Dashboard under
  Developers > Webhooks.

When ``STRIPE_SECRET_KEY`` is not set, :meth:`__init__` raises
``RuntimeError`` so the registry can report the provider as
unavailable and the route layer returns HTTP 503.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from backend.services.payment.base import PaymentProvider, PaymentResult, PaymentSession

logger = logging.getLogger(__name__)


class StripeProvider(PaymentProvider):
    """Stripe Checkout payment provider."""

    def __init__(self) -> None:
        secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        if not secret_key:
            raise RuntimeError("Stripe not configured")

        import stripe

        stripe.api_key = secret_key
        self._stripe = stripe
        self._webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

    async def create_checkout(
        self,
        *,
        order_no: str,
        amount_cents: int,
        currency: str,
        description: str,
        return_url: str,
    ) -> PaymentSession:
        stripe = self._stripe
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": currency.lower(),
                        "unit_amount": int(amount_cents),
                        "product_data": {
                            "name": description or f"Order {order_no}",
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{return_url}?status=success&order={order_no}",
            cancel_url=f"{return_url}?status=cancelled&order={order_no}",
            metadata={"order_no": order_no},
            expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()),
        )
        expires_at = None
        if session.get("expires_at"):
            expires_at = datetime.fromtimestamp(int(session["expires_at"]), tz=timezone.utc)
        return PaymentSession(
            session_id=session["id"],
            checkout_url=session["url"],
            expires_at=expires_at,
        )

    async def verify_webhook(
        self,
        *,
        payload: bytes,
        signature: str,
    ) -> PaymentResult:
        stripe = self._stripe
        if not self._webhook_secret:
            raise ValueError("STRIPE_WEBHOOK_SECRET not configured")

        try:
            event = stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise ValueError(f"Invalid webhook signature: {exc}") from exc

        event_type = event.get("type", "")
        data = event.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            payment_status = data.get("payment_status", "")
            status = "succeeded" if payment_status == "paid" else "pending"
            return PaymentResult(
                status=status,
                amount_cents=int(data.get("amount_total") or 0),
                currency=str(data.get("currency") or ""),
                provider_reference=str(data.get("payment_intent") or data.get("id") or ""),
                raw={"event_type": event_type, "data": data},
            )

        # Non-checkout events: return pending so the caller can no-op.
        return PaymentResult(
            status="pending",
            raw={"event_type": event_type, "data": data},
        )

    async def query_status(
        self,
        *,
        session_id: str,
    ) -> PaymentResult:
        stripe = self._stripe
        session = stripe.checkout.Session.retrieve(session_id)
        payment_status = session.get("payment_status", "")
        if payment_status == "paid":
            status = "succeeded"
        elif payment_status == "unpaid":
            status = "pending"
        else:
            status = payment_status or "pending"

        return PaymentResult(
            status=status,
            amount_cents=int(session.get("amount_total") or 0),
            currency=str(session.get("currency") or ""),
            provider_reference=str(session.get("payment_intent") or session.get("id") or ""),
            raw=dict(session) if hasattr(session, "keys") else {},
        )
