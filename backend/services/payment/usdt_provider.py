"""NOWPayments-backed USDT (and other crypto) payment provider.

NOWPayments is a non-custodial crypto payment gateway. The operator
configures an account, obtains an API key and an IPN (Instant Payment
Notification) secret, and sets them via environment variables:

- ``NOWPAYMENTS_API_KEY`` — required to create payments and query status.
- ``NOWPAYMENTS_IPN_SECRET`` — shared secret used to HMAC-verify IPN
  callbacks posted to ``POST /api/webhooks/usdt``.
- ``NOWPAYMENTS_CNY_USDT_RATE`` — static CNY → USDT rate applied when
  the internal order (priced in CNY) is checked out in USDT. The
  operator absorbs the FX spread.

The provider speaks the :class:`PaymentProvider` interface.
``amount_cents`` is interpreted as the amount in the *smallest* unit of
the checkout currency — for USDT that is hundredths of a USDT
(10.50 USDT → 1050). Webhook and query return the same scale so the
upstream amount-mismatch check (``paid_amount = cents / 100``) stays
correct without provider-specific branches.

Payments are non-custodial: NOWPayments posts the crypto to the
operator's payout address and the gateway never holds the funds.
Refunds are out of band (there is no programmatic refund API
exposed here).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx

from backend.services.payment.base import (
    PaymentProvider,
    PaymentResult,
    PaymentSession,
)

logger = logging.getLogger(__name__)


# NOWPayments status → internal PaymentResult.status
#
# ``partially_paid`` maps to a dedicated ``partial`` status (instead of
# being collapsed into ``pending``) so the webhook handler in
# ``routes/billing.py::_process_usdt_event`` can detect it and route
# the order to ``pending_review`` via ``handle_partial_payment``.
# Mapping it to ``pending`` silently dropped the event — the helper
# existed but was never invoked, leaving under-paid orders stuck.
_STATUS_MAP = {
    "waiting": "pending",
    "confirming": "pending",
    "confirmed": "pending",
    "sending": "pending",
    "partially_paid": "partial",
    "paid": "succeeded",
    "finished": "succeeded",
    "failed": "failed",
    "refunded": "failed",
    "expired": "failed",
}


class UsdtProvider(PaymentProvider):
    """NOWPayments-backed USDT / crypto provider."""

    _API_BASE = "https://api.nowpayments.io/v1"

    def __init__(self) -> None:
        api_key = (os.getenv("NOWPAYMENTS_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("NOWPayments not configured (NOWPAYMENTS_API_KEY missing)")
        self._api_key = api_key
        self._ipn_secret = (os.getenv("NOWPAYMENTS_IPN_SECRET") or "").strip() or None
        # In-process session cache keyed by payment_id (returned by
        # /payment). Lets query_status look up the order_no we attached
        # to the checkout without a round-trip to our DB.
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    def _headers(self, *, with_auth: bool = True) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if with_auth:
            h["x-api-key"] = self._api_key
        return h

    def _checkout_url(self, payment: Dict[str, Any]) -> str:
        """Build the URL the user is redirected to after payment creation.

        NOWPayments exposes two entry points:

        * ``pay_address`` — raw deposit address (works in any wallet).
        * ``invoice_url`` — hosted invoice page (only on /invoice
          creation, not /payment).

        We use /payment (no hosted page) so the frontend renders the
        deposit address + QR itself. The return URL is a deep-link
        back to the Wallet page with the payment_id in the query so
        the frontend can poll query_status.
        """
        pay_address = payment.get("pay_address") or ""
        payment_id = str(payment.get("payment_id") or "")
        # Frontend consumes these query params to render the deposit UI.
        return f"/wallet/crypto?payment_id={payment_id}&address={pay_address}"

    # -----------------------------------------------------------------
    # PaymentProvider interface
    # -----------------------------------------------------------------

    async def create_checkout(
        self,
        *,
        order_no: str,
        amount_cents: int,
        currency: str,
        description: str,
        return_url: str,
    ) -> PaymentSession:
        amount = float(amount_cents) / 100.0
        pay_currency = (currency or "usdt").lower()

        body = {
            "price_amount": amount,
            "price_currency": "usd",  # NOWPayments normalises to USD internally
            "pay_currency": pay_currency,
            "order_id": order_no,
            "order_description": description or f"Order {order_no}",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(
                    f"{self._API_BASE}/payment",
                    headers=self._headers(),
                    json=body,
                )
            except httpx.HTTPError as exc:
                logger.error("NOWPayments create_checkout HTTP error: %s", exc)
                raise RuntimeError(f"NOWPayments unreachable: {exc}") from exc

        if resp.status_code >= 400:
            try:
                err = resp.json()
            except Exception:
                err = {"raw": resp.text}
            logger.error("NOWPayments create_checkout failed: %s", err)
            raise RuntimeError(
                f"NOWPayments rejected the payment: {err.get('message') or resp.status_code}"
            )

        try:
            payment = resp.json()
        except ValueError as exc:
            raise RuntimeError("NOWPayments returned invalid JSON") from exc

        payment_id = payment.get("payment_id")
        if not payment_id:
            raise RuntimeError("NOWPayments response missing payment_id")

        # Prefer NOWPayments' own pay_amount (the exact on-chain figure
        # after fees) and fall back to what we asked for.
        pay_amount = float(payment.get("pay_amount") or amount)
        network = (payment.get("network") or pay_currency).upper()

        self._sessions[str(payment_id)] = {
            "order_no": order_no,
            "amount_cents": amount_cents,
            "currency": pay_currency,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Frontend consumes these query params to render the deposit UI.
        from urllib.parse import urlencode

        qs = urlencode(
            {
                "payment_id": payment_id,
                "address": payment.get("pay_address") or "",
                "pay_amount": f"{pay_amount:.6f}".rstrip("0").rstrip("."),
                "pay_currency": pay_currency.upper(),
                "network": network,
            }
        )
        checkout_url = f"/wallet/crypto?{qs}"

        return PaymentSession(
            session_id=str(payment_id),
            checkout_url=checkout_url,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )

    async def verify_webhook(
        self,
        *,
        payload: bytes,
        signature: str,
    ) -> PaymentResult:
        if not self._ipn_secret:
            raise ValueError("NOWPAYMENTS_IPN_SECRET not configured")

        if not signature:
            raise ValueError("Missing IPN signature")

        expected = hmac.new(
            self._ipn_secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("Invalid IPN signature")

        try:
            import json as _json

            event = _json.loads(payload.decode("utf-8"))
        except ValueError as exc:
            raise ValueError(f"Invalid webhook payload: {exc}") from exc

        status_code = (event.get("payment_status") or "").lower()
        status = _STATUS_MAP.get(status_code, "pending")

        amount_paid = float(event.get("actually_paid") or 0.0)
        amount_cents = int(round(amount_paid * 100))
        currency = str(event.get("pay_currency") or "").lower()

        provider_ref = str(event.get("payment_id") or "")
        # NOWPayments echoes our order_id back verbatim. The webhook
        # route in billing.py uses it to locate the local order.
        order_no = event.get("order_id") or event.get("order_no") or ""

        return PaymentResult(
            status=status,
            amount_cents=amount_cents,
            currency=currency,
            provider_reference=provider_ref,
            raw={
                "event": event,
                "order_no": order_no,
            },
        )

    async def query_status(
        self,
        *,
        session_id: str,
    ) -> PaymentResult:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(
                    f"{self._API_BASE}/payment/{session_id}",
                    headers=self._headers(),
                )
            except httpx.HTTPError as exc:
                logger.warning("NOWPayments query_status HTTP error: %s", exc)
                return PaymentResult(status="pending", provider_reference=session_id)

        if resp.status_code == 404:
            return PaymentResult(status="pending", provider_reference=session_id)
        if resp.status_code >= 400:
            logger.warning("NOWPayments query_status %s: %s", resp.status_code, resp.text)
            return PaymentResult(status="pending", provider_reference=session_id)

        try:
            data = resp.json()
        except ValueError:
            return PaymentResult(status="pending", provider_reference=session_id)

        status_code = (data.get("payment_status") or "").lower()
        status = _STATUS_MAP.get(status_code, "pending")

        amount_paid = float(data.get("actually_paid") or 0.0)
        amount_cents = int(round(amount_paid * 100))
        currency = str(data.get("pay_currency") or "").lower()

        return PaymentResult(
            status=status,
            amount_cents=amount_cents,
            currency=currency,
            provider_reference=str(data.get("payment_id") or session_id),
            raw=data,
        )
