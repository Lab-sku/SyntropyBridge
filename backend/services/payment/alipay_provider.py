"""Alipay payment provider stub.

This module provides a placeholder implementation of the
:class:`PaymentProvider` interface for Alipay (支付宝). It raises
``NotImplementedError`` for all operations, allowing the admin to
enable Alipay in the configuration without breaking the system.

Users will see a clear error and fall back to manual approval.

Implementation requirements (for future reference)
---------------------------------------------------
To implement a real Alipay provider, you need:

1. **Alipay RSA key pair** — generate a 2048-bit RSA key pair and
   upload the public key to the Alipay Open Platform. Store the
   private key securely (HSM or encrypted file).

2. **Alipay App ID** — obtained from the Alipay Open Platform after
   creating an application.

3. **Alipay public key** — the platform's public key for verifying
   signatures on callbacks.

4. **SDK** — ``alipay-sdk-python`` or ``python-alipay-sdk``.

5. **Notify URL** — a publicly accessible endpoint for Alipay to send
   payment notifications to.

Environment variables needed::

    ALIPAY_APP_ID=<your app id>
    ALIPAY_PRIVATE_KEY=<PEM-encoded RSA private key>
    ALIPAY_PUBLIC_KEY=<Alipay's public key>
    ALIPAY_NOTIFY_URL=https://your-domain/api/webhooks/alipay
"""

from __future__ import annotations

from backend.services.payment.base import PaymentProvider, PaymentResult, PaymentSession


class AlipayProvider(PaymentProvider):
    """Alipay stub — raises NotImplementedError for all operations."""

    async def create_checkout(self, **kwargs) -> PaymentSession:
        raise NotImplementedError("Alipay integration pending — use Stripe or manual approval")

    async def verify_webhook(self, **kwargs) -> PaymentResult:
        raise NotImplementedError("Alipay integration pending — use Stripe or manual approval")

    async def query_status(self, **kwargs) -> PaymentResult:
        raise NotImplementedError("Alipay integration pending — use Stripe or manual approval")
