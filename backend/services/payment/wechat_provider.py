"""WeChat Pay payment provider stub.

This module provides a placeholder implementation of the
:class:`PaymentProvider` interface for WeChat Pay (微信支付). It
raises ``NotImplementedError`` for all operations, allowing the admin
to enable WeChat Pay in the configuration without breaking the system.

Users will see a clear error and fall back to manual approval.

Implementation requirements (for future reference)
---------------------------------------------------
To implement a real WeChat Pay provider, you need:

1. **WeChat Pay Merchant ID** (``mch_id``) — obtained after
   registering as a merchant on the WeChat Pay platform.

2. **API v3 Key** — a 32-character key for encrypting/decrypting
   sensitive fields in API v3 requests.

3. **Merchant API Certificate** — ``apiclient_cert.pem`` and
   ``apiclient_key.pem`` downloaded from the merchant dashboard.

4. **Merchant Serial Number** — the serial number of the API
   certificate, used for request signing.

5. **SDK** — ``wechatpayv3`` or a custom implementation of the
   WeChat Pay API v3 protocol.

6. **Notify URL** — a publicly accessible HTTPS endpoint for WeChat
   to send payment result notifications to.

Environment variables needed::

    WECHAT_PAY_MCH_ID=<merchant id>
    WECHAT_PAY_APP_ID=<app id>
    WECHAT_PAY_API_V3_KEY=<32-char key>
    WECHAT_PAY_CERT_PATH=/path/to/apiclient_cert.pem
    WECHAT_PAY_KEY_PATH=/path/to/apiclient_key.pem
    WECHAT_PAY_SERIAL_NO=<certificate serial number>
    WECHAT_PAY_NOTIFY_URL=https://your-domain/api/webhooks/wechat
"""

from __future__ import annotations

from backend.services.payment.base import PaymentProvider, PaymentResult, PaymentSession


class WechatProvider(PaymentProvider):
    """WeChat Pay stub — raises NotImplementedError for all operations."""

    async def create_checkout(self, **kwargs) -> PaymentSession:
        raise NotImplementedError("WeChat Pay integration pending — use Stripe or manual approval")

    async def verify_webhook(self, **kwargs) -> PaymentResult:
        raise NotImplementedError("WeChat Pay integration pending — use Stripe or manual approval")

    async def query_status(self, **kwargs) -> PaymentResult:
        raise NotImplementedError("WeChat Pay integration pending — use Stripe or manual approval")
