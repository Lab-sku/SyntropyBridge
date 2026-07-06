"""Unit tests for the payment provider package.

Covers:
- Provider registry (get_provider, list_providers)
- Stripe provider (create_checkout, verify_webhook, query_status)
- Alipay and WeChat stubs (NotImplementedError)

The ``stripe`` Python SDK is NOT installed in the test environment, so we
inject a MagicMock into ``sys.modules`` before any StripeProvider import.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from backend.services.payment.base import PaymentResult, PaymentSession

# ---------------------------------------------------------------------------
# Stripe mock helpers
# ---------------------------------------------------------------------------


def _make_mock_stripe():
    """Return a MagicMock that quacks like the ``stripe`` module."""
    mock = MagicMock()
    # Provide a real exception class for SignatureVerificationError
    mock.error = MagicMock()
    mock.error.SignatureVerificationError = type("SignatureVerificationError", (Exception,), {})
    return mock


@pytest.fixture
def mock_stripe_module(monkeypatch):
    """Inject a mock ``stripe`` module into sys.modules so that
    ``import stripe`` inside StripeProvider.__init__ succeeds.

    Yields the mock so tests can configure return values.
    """
    mock = _make_mock_stripe()
    monkeypatch.setitem(sys.modules, "stripe", mock)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_secret")
    yield mock


def _fresh_stripe_provider(mock_stripe):
    """Create a StripeProvider instance with the mock stripe attached.

    We import StripeProvider AFTER the mock is in sys.modules (guaranteed
    by the mock_stripe_module fixture).
    """
    from backend.services.payment import reset_providers
    from backend.services.payment.stripe_provider import StripeProvider

    reset_providers()
    return StripeProvider()


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_get_stripe(self, mock_stripe_module):
        from backend.services.payment import get_provider, reset_providers
        from backend.services.payment.stripe_provider import StripeProvider

        reset_providers()
        provider = get_provider("stripe")
        assert isinstance(provider, StripeProvider)

    def test_get_alipay(self):
        from backend.services.payment import get_provider, reset_providers
        from backend.services.payment.alipay_provider import AlipayProvider

        reset_providers()
        provider = get_provider("alipay")
        assert isinstance(provider, AlipayProvider)

    def test_get_wechat(self):
        from backend.services.payment import get_provider, reset_providers
        from backend.services.payment.wechat_provider import WechatProvider

        reset_providers()
        provider = get_provider("wechat")
        assert isinstance(provider, WechatProvider)

    def test_unknown_provider_raises(self):
        from backend.services.payment import get_provider, reset_providers

        reset_providers()
        with pytest.raises(KeyError, match="Unknown payment provider"):
            get_provider("unknown")

    def test_lazy_import_no_error_at_module_load(self):
        """Importing the payment package must not trigger an ImportError
        even when the stripe SDK is absent."""
        import backend.services.payment  # noqa: F401

        # The module loaded successfully — stripe is only imported when
        # StripeProvider is instantiated.


# ---------------------------------------------------------------------------
# Stripe provider — create_checkout
# ---------------------------------------------------------------------------


class TestStripeCreateCheckout:
    def test_returns_payment_session(self, mock_stripe_module):
        mock_stripe_module.checkout.Session.create.return_value = {
            "id": "cs_test_123",
            "url": "https://checkout.stripe.com/pay/cs_test_123",
            "expires_at": 1999999999,
        }

        provider = _fresh_stripe_provider(mock_stripe_module)
        session = asyncio.run(
            provider.create_checkout(
                order_no="ORD-001",
                amount_cents=5000,
                currency="usd",
                description="Test order",
                return_url="https://example.com/return",
            )
        )

        assert isinstance(session, PaymentSession)
        assert session.session_id == "cs_test_123"
        assert "checkout.stripe.com" in session.checkout_url

        # Verify correct params passed to Stripe
        call_kwargs = mock_stripe_module.checkout.Session.create.call_args
        assert call_kwargs.kwargs["mode"] == "payment"
        assert call_kwargs.kwargs["metadata"] == {"order_no": "ORD-001"}
        line_items = call_kwargs.kwargs["line_items"]
        assert line_items[0]["price_data"]["unit_amount"] == 5000
        assert line_items[0]["price_data"]["currency"] == "usd"

    def test_raises_when_key_missing(self, monkeypatch):
        """StripeProvider.__init__ raises RuntimeError when STRIPE_SECRET_KEY
        is not set."""
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        # Ensure the mock is in sys.modules so import doesn't fail
        monkeypatch.setitem(sys.modules, "stripe", _make_mock_stripe())

        from backend.services.payment.stripe_provider import StripeProvider

        with pytest.raises(RuntimeError, match="Stripe not configured"):
            StripeProvider()


# ---------------------------------------------------------------------------
# Stripe provider — verify_webhook
# ---------------------------------------------------------------------------


class TestStripeVerifyWebhook:
    def test_valid_signature_returns_succeeded(self, mock_stripe_module):
        mock_stripe_module.Webhook.construct_event.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "payment_status": "paid",
                    "amount_total": 5000,
                    "currency": "usd",
                    "payment_intent": "pi_test_456",
                    "id": "cs_test_123",
                }
            },
        }

        provider = _fresh_stripe_provider(mock_stripe_module)
        result = asyncio.run(
            provider.verify_webhook(
                payload=b'{"type":"checkout.session.completed"}',
                signature="valid_sig",
            )
        )

        assert isinstance(result, PaymentResult)
        assert result.status == "succeeded"
        assert result.amount_cents == 5000
        assert result.currency == "usd"
        assert result.provider_reference == "pi_test_456"

    def test_invalid_signature_raises(self, mock_stripe_module):
        sig_error_cls = mock_stripe_module.error.SignatureVerificationError
        mock_stripe_module.Webhook.construct_event.side_effect = sig_error_cls("bad sig")

        provider = _fresh_stripe_provider(mock_stripe_module)
        with pytest.raises(ValueError, match="Invalid webhook signature"):
            asyncio.run(
                provider.verify_webhook(
                    payload=b"bad_payload",
                    signature="bad_sig",
                )
            )


# ---------------------------------------------------------------------------
# Stripe provider — query_status
# ---------------------------------------------------------------------------


class TestStripeQueryStatus:
    def test_returns_payment_status(self, mock_stripe_module):
        mock_stripe_module.checkout.Session.retrieve.return_value = {
            "payment_status": "paid",
            "amount_total": 3000,
            "currency": "eur",
            "payment_intent": "pi_test_789",
            "id": "cs_test_999",
        }

        provider = _fresh_stripe_provider(mock_stripe_module)
        result = asyncio.run(provider.query_status(session_id="cs_test_999"))

        assert isinstance(result, PaymentResult)
        assert result.status == "succeeded"
        assert result.amount_cents == 3000
        assert result.currency == "eur"

        mock_stripe_module.checkout.Session.retrieve.assert_called_once_with("cs_test_999")


# ---------------------------------------------------------------------------
# Alipay stub
# ---------------------------------------------------------------------------


class TestAlipayStub:
    def _provider(self):
        from backend.services.payment.alipay_provider import AlipayProvider

        return AlipayProvider()

    def test_create_checkout_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().create_checkout())

    def test_verify_webhook_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().verify_webhook())

    def test_query_status_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().query_status())


# ---------------------------------------------------------------------------
# WeChat stub
# ---------------------------------------------------------------------------


class TestWechatStub:
    def _provider(self):
        from backend.services.payment.wechat_provider import WechatProvider

        return WechatProvider()

    def test_create_checkout_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().create_checkout())

    def test_verify_webhook_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().verify_webhook())

    def test_query_status_raises(self):
        with pytest.raises(NotImplementedError):
            asyncio.run(self._provider().query_status())
