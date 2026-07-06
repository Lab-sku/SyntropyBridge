"""Unit tests for EmailService (dev-mode and template methods).

All tests run in *dev mode* — no SMTP server is needed.  The ``send()``
static method logs emails to ``audit_logs`` when SMTP is unconfigured.

The conftest schema for ``audit_logs`` is missing ``actor_username`` and
``user_agent`` columns that the production ``add_audit_log`` helper
expects.  The ``_fix_audit_schema`` fixture patches those in.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.email_service import EmailService, _base_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def _fix_audit_schema(temp_db):
    """Add columns that production ``add_audit_log`` expects but the
    conftest schema omits."""
    c = sqlite3.connect(temp_db)
    for col in ("actor_username", "user_agent"):
        try:
            c.execute(f"ALTER TABLE audit_logs ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    c.commit()
    c.close()
    yield temp_db


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch):
    """Ensure SMTP is never configured during email tests so every call
    goes through the dev-mode path."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)


# ---------------------------------------------------------------------------
# Dev-mode send
# ---------------------------------------------------------------------------


class TestSendDevMode:
    def test_returns_true_without_smtp(self, temp_db):
        result = asyncio.run(
            EmailService.send(
                to="user@example.com",
                subject="Hello",
                html_body="<p>World</p>",
            )
        )
        assert result is True

    def test_audit_row_created(self, _fix_audit_schema):
        asyncio.run(
            EmailService.send(
                to="user@example.com",
                subject="Test Subject",
                html_body="<p>Body here</p>",
            )
        )

        c = _conn(_fix_audit_schema)
        rows = c.execute("SELECT * FROM audit_logs WHERE action = 'EMAIL_SENT_DEV'").fetchall()
        c.close()
        assert len(rows) >= 1

    def test_audit_row_contains_recipient_subject_body(self, _fix_audit_schema):
        asyncio.run(
            EmailService.send(
                to="alice@example.com",
                subject="Important Notice",
                html_body="<p>Please read this carefully</p>",
            )
        )

        c = _conn(_fix_audit_schema)
        row = c.execute(
            "SELECT * FROM audit_logs WHERE action = 'EMAIL_SENT_DEV' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        c.close()

        assert row is not None
        meta = json.loads(row["metadata"])
        assert "alice@example.com" in meta.get("to", "")
        assert "Important Notice" in meta.get("subject", "")
        assert "Please read this carefully" in meta.get("body_preview", "")


# ---------------------------------------------------------------------------
# Template methods — each calls send() internally
# ---------------------------------------------------------------------------


class TestSendPasswordReset:
    def test_generates_expected_html(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_password_reset(
                    email="bob@example.com",
                    reset_url="https://example.com/reset?token=abc",
                    username="bob",
                )
            )

        assert result is True
        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "Reset Password" in html or "重置密码" in html
        assert "https://example.com/reset?token=abc" in html
        assert "bob" in html


class TestSendOrderApproved:
    def test_includes_order_no_and_credits(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_order_approved(
                    email="user@example.com",
                    username="alice",
                    order_no="ORD-2024-001",
                    amount=100.0,
                    credits=100.0,
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "ORD-2024-001" in html
        assert "100" in html


class TestSendOrderRejected:
    def test_includes_reason_when_provided(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_order_rejected(
                    email="user@example.com",
                    username="alice",
                    order_no="ORD-2024-002",
                    reason="Payment verification failed",
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "ORD-2024-002" in html
        assert "Payment verification failed" in html

    def test_without_reason(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_order_rejected(
                    email="user@example.com",
                    username="alice",
                    order_no="ORD-2024-003",
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "ORD-2024-003" in html


class TestSendSubscriptionRenewed:
    def test_includes_plan_name_and_expires(self):
        mock_send = AsyncMock(return_value=True)
        expires = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_subscription_renewed(
                    email="user@example.com",
                    username="alice",
                    plan_name="Pro Plan",
                    expires_at=expires,
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "Pro Plan" in html
        assert "2026-06-15" in html


class TestSendSubscriptionExpired:
    def test_includes_plan_name(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_subscription_expired(
                    email="user@example.com",
                    username="alice",
                    plan_name="Basic Plan",
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "Basic Plan" in html
        assert "expired" in html.lower() or "到期" in html


class TestSendAutoRechargeTriggered:
    def test_includes_amount(self):
        mock_send = AsyncMock(return_value=True)
        with patch.object(EmailService, "send", mock_send):
            result = asyncio.run(
                EmailService.send_auto_recharge_triggered(
                    email="user@example.com",
                    username="alice",
                    amount=50.0,
                    order_no="ORD-AUTO-001",
                )
            )

        assert result is True
        kwargs = mock_send.call_args.kwargs
        html = kwargs["html_body"]
        assert "50" in html
        assert "ORD-AUTO-001" in html


# ---------------------------------------------------------------------------
# APP_BASE_URL
# ---------------------------------------------------------------------------


class TestAppBaseUrl:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        assert _base_url() == "https://your-domain.com"

    def test_reads_from_env(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://my-api.example.com")
        assert _base_url() == "https://my-api.example.com"
