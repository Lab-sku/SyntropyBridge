"""Abstract payment provider interface.

All payment gateways (Stripe, Alipay, WeChat Pay, ...) must extend
:class:`PaymentProvider` and implement its three abstract methods.

The design follows a *checkout session* pattern:

1. ``create_checkout`` — creates a hosted payment session with the
   provider and returns a URL to redirect the user to.
2. ``verify_webhook`` — validates the provider's server-to-server
   callback and extracts the payment result.
3. ``query_status`` — polls the provider for the current status of a
   previously created session (used after the user returns from the
   provider's checkout page).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class PaymentSession:
    """Returned by :meth:`PaymentProvider.create_checkout`."""

    session_id: str
    """Provider's session / checkout identifier."""

    checkout_url: str
    """URL to redirect the user to for payment."""

    expires_at: Optional[datetime] = None
    """When the checkout session expires (provider-specific)."""


@dataclass
class PaymentResult:
    """Returned by :meth:`PaymentProvider.verify_webhook` and
    :meth:`PaymentProvider.query_status`."""

    status: str
    """One of ``succeeded``, ``failed``, ``pending``."""

    amount_cents: int = 0
    """Amount actually paid, in the smallest currency unit (cents / fen)."""

    currency: str = ""
    """ISO 4217 currency code (``usd``, ``cny``, ...)."""

    provider_reference: str = ""
    """Provider-specific transaction identifier (payment_intent, trade_no, ...)."""

    raw: Dict[str, Any] = field(default_factory=dict)
    """Full provider payload for debugging / audit."""


class PaymentProvider(ABC):
    """Abstract base class for payment gateway integrations."""

    @abstractmethod
    async def create_checkout(
        self,
        *,
        order_no: str,
        amount_cents: int,
        currency: str,
        description: str,
        return_url: str,
    ) -> PaymentSession:
        """Create a hosted checkout session with the payment provider.

        Parameters
        ----------
        order_no:
            Our internal order number — embedded in metadata so the
            webhook can look it up.
        amount_cents:
            Amount to charge in the smallest currency unit.
        currency:
            ISO 4217 currency code.
        description:
            Human-readable line-item description.
        return_url:
            Where the provider should redirect the user after payment.

        Returns
        -------
        PaymentSession with the checkout URL.
        """
        ...

    @abstractmethod
    async def verify_webhook(
        self,
        *,
        payload: bytes,
        signature: str,
    ) -> PaymentResult:
        """Validate and parse a webhook callback from the provider.

        Parameters
        ----------
        payload:
            Raw request body bytes.
        signature:
            Provider-specific signature header value.

        Returns
        -------
        PaymentResult with the parsed payment status.

        Raises
        ------
        ValueError
            When the signature is invalid.
        """
        ...

    @abstractmethod
    async def query_status(
        self,
        *,
        session_id: str,
    ) -> PaymentResult:
        """Poll the provider for the current status of a checkout session.

        Used after the user returns from the provider's checkout page
        (or when the webhook hasn't fired yet).

        Parameters
        ----------
        session_id:
            The ``session_id`` previously returned by ``create_checkout``.

        Returns
        -------
        PaymentResult with the current status.
        """
        ...
