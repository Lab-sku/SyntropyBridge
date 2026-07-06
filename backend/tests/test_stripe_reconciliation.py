"""Unit tests for :mod:`backend.services.stripe_reconciliation`.

Covers the 11 reconciliation scenarios:

1. auto-approve (paid + pending + amount match)
2. amount mismatch → pending_review
3. orphan (no local order)
4. late_payment (paid but local order already expired)
5. dedup (two paid sessions for same order)
6. disabled (STRIPE_RECON_ENABLED=false)
7. API failure (session.list raises)
8. amount tolerance (small diff < tolerance → approve)
9. USDT rate applied (CNY order, USDT provider, rate configured)
10. max-auto-approve cap
11. already-terminal order (paid/refunded) — no-op + backfill reference
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from backend.services import stripe_reconciliation as recon_mod
from backend.services.stripe_reconciliation import StripeReconciliation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_user(path: str, user_id: int = 1, balance: float = 0.0) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO users (id, username, api_key, quota_5h, quota_week,
                           quota_month, monthly_budget, is_active)
        VALUES (?, ?, ?, 1000, 10000, 100000, 0, 1)
        """,
        (user_id, f"user{user_id}", f"ak_test_{user_id}"),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO wallets
            (user_id, balance, total_recharged, total_consumed, frozen,
             auto_recharge_enabled)
        VALUES (?, ?, 0, 0, 0, 0)
        """,
        (user_id, balance),
    )
    conn.commit()
    conn.close()


def _insert_order(
    path: str,
    *,
    order_no: str = "ORD_TEST_001",
    user_id: int = 1,
    amount: float = 10.0,
    credits: float = 1000.0,
    status: str = "pending",
    payment_session_id: str = "cs_test_001",
    payment_provider: str = "stripe",
    payment_reference: str = None,
    created_at: str = None,
) -> int:
    conn = _connect(path)
    cur = conn.execute(
        """
        INSERT INTO orders
            (order_no, user_id, amount, credits, payment_method, status,
             payment_session_id, payment_provider, payment_reference, created_at)
        VALUES (?, ?, ?, ?, 'stripe', ?, ?, ?, ?, ?)
        """,
        (
            order_no,
            user_id,
            amount,
            credits,
            status,
            payment_session_id,
            payment_provider,
            payment_reference,
            created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    order_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return order_id


def _make_session(
    *,
    session_id: str = "cs_test_001",
    order_no: str = "ORD_TEST_001",
    amount_total_cents: int = 1000,
    currency: str = "cny",
    payment_intent: str = "pi_test_001",
    payment_status: str = "paid",
) -> dict:
    """Construct a Stripe Checkout Session dict the way the reconciler sees it."""
    return {
        "id": session_id,
        "payment_intent": payment_intent,
        "payment_status": payment_status,
        "amount_total": amount_total_cents,
        "currency": currency,
        "metadata": {"order_no": order_no},
    }


def _fake_stripe_module(sessions):
    """Build a fake ``stripe`` module whose ``checkout.Session.list``
    returns the supplied sessions (already filtered to paid ones)."""

    class _Page:
        def __init__(self, data):
            self.data = data
            self.has_more = False

        def get(self, key, default=None):
            return getattr(self, key, default)

    class _SessionAPI:
        @staticmethod
        def list(**params):
            return _Page(sessions)

    fake = types.SimpleNamespace()
    fake.api_key = None
    fake.checkout = types.SimpleNamespace(Session=_SessionAPI)
    return fake


@pytest.fixture
def cfg_enabled():
    """Force-enable reconciliation with sane test knobs.

    Also injects a fake ``stripe`` module into ``sys.modules`` so the
    ``import stripe`` inside ``run_daily_reconciliation`` succeeds
    even when the real SDK isn't installed.
    """
    fake_stripe = _fake_stripe_module([])
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_dummy"}):
        with patch.dict(sys.modules, {"stripe": fake_stripe}):
            with patch.object(recon_mod, "_config") as mock_cfg:
                mock_cfg.return_value = {
                    "enabled": True,
                    "lookback_hours": 48,
                    "max_auto_approve": 50,
                    "amount_tolerance": 0.01,
                    "stripe_secret_key": "sk_test_dummy",
                    "usdt_rate": 0.0,
                }
                yield mock_cfg


# ---------------------------------------------------------------------------
# 1. Auto-approve
# ---------------------------------------------------------------------------


class TestAutoApprove:
    def test_paid_pending_amount_match_approves(self, temp_db, cfg_enabled):
        _insert_user(temp_db, user_id=1, balance=0.0)
        order_id = _insert_order(
            temp_db,
            order_no="ORD_TEST_001",
            amount=10.0,
            credits=1000.0,
            status="pending",
        )
        sessions = [_make_session(order_no="ORD_TEST_001", amount_total_cents=1000)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["sessions_scanned"] == 1
        assert result["auto_approved"] == 1
        assert result["errors"] == []

        # Verify the order is now paid.
        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT status, payment_reference FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "paid"
        assert row["payment_reference"] == "pi_test_001"


# ---------------------------------------------------------------------------
# 2. Amount mismatch → pending_review
# ---------------------------------------------------------------------------


class TestAmountMismatch:
    def test_paid_wrong_amount_routes_to_pending_review(self, temp_db, cfg_enabled):
        _insert_user(temp_db, user_id=1)
        order_id = _insert_order(
            temp_db,
            order_no="ORD_TEST_002",
            amount=10.0,
            credits=1000.0,
            status="pending",
        )
        # User paid 5.00 instead of 10.00
        sessions = [_make_session(order_no="ORD_TEST_002", amount_total_cents=500)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["pending_review"] == 1
        assert result["auto_approved"] == 0

        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT status, note FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()
        assert row["status"] == "pending_review"
        assert "amount_mismatch" in (row["note"] or "")


# ---------------------------------------------------------------------------
# 3. Orphan (no local order)
# ---------------------------------------------------------------------------


class TestOrphan:
    def test_paid_no_local_order_counts_orphan(self, temp_db, cfg_enabled):
        # No local order for this session
        sessions = [_make_session(order_no="ORD_NONEXISTENT", amount_total_cents=1000)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["orphans"] == 1
        assert result["auto_approved"] == 0


# ---------------------------------------------------------------------------
# 4. Late payment (local order already expired)
# ---------------------------------------------------------------------------


class TestLatePayment:
    def test_paid_but_local_order_expired(self, temp_db, cfg_enabled):
        _insert_user(temp_db, user_id=1)
        order_id = _insert_order(
            temp_db,
            order_no="ORD_LATE_001",
            amount=10.0,
            credits=1000.0,
            status="expired",
        )
        sessions = [_make_session(order_no="ORD_LATE_001", amount_total_cents=1000)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["late_payments"] == 1
        assert result["auto_approved"] == 0

        conn = _connect(temp_db)
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
        # Status unchanged — late payments need human decision
        assert row["status"] == "expired"


# ---------------------------------------------------------------------------
# 5. Dedup (two paid sessions for the same order)
# ---------------------------------------------------------------------------


class TestDedup:
    def test_duplicate_sessions_for_same_order_approve_once(self, temp_db, cfg_enabled):
        _insert_user(temp_db, user_id=1)
        order_id = _insert_order(
            temp_db,
            order_no="ORD_DEDUP_001",
            amount=10.0,
            credits=1000.0,
            status="pending",
        )
        sessions = [
            _make_session(
                session_id="cs_dedup_1",
                order_no="ORD_DEDUP_001",
                amount_total_cents=1000,
            ),
            _make_session(
                session_id="cs_dedup_2",
                order_no="ORD_DEDUP_001",
                amount_total_cents=1000,
            ),
        ]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["sessions_scanned"] == 2
        assert result["auto_approved"] == 1

        conn = _connect(temp_db)
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        conn.close()
        assert row["status"] == "paid"


# ---------------------------------------------------------------------------
# 6. Disabled
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_disabled_returns_skipped(self, temp_db, cfg_enabled):
        cfg_enabled.return_value = {
            "enabled": False,
            "lookback_hours": 48,
            "max_auto_approve": 50,
            "amount_tolerance": 0.01,
            "stripe_secret_key": "sk_test_dummy",
            "usdt_rate": 0.0,
        }
        result = StripeReconciliation.run_daily_reconciliation()
        assert result["skipped"] is True
        assert "STRIPE_RECON_ENABLED" in result["reason"]


# ---------------------------------------------------------------------------
# 7. API failure (session.list raises)
# ---------------------------------------------------------------------------


class TestAPIFailure:
    def test_session_list_failure_records_error(self, temp_db, cfg_enabled):
        with patch.object(
            StripeReconciliation,
            "_list_paid_sessions",
            side_effect=Exception("stripe API down"),
        ):
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["sessions_scanned"] == 0
        assert any("session.list" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# 8. Amount tolerance
# ---------------------------------------------------------------------------


class TestAmountTolerance:
    def test_small_diff_within_tolerance_approves(self, temp_db, cfg_enabled):
        cfg_enabled.return_value = {
            "enabled": True,
            "lookback_hours": 48,
            "max_auto_approve": 50,
            "amount_tolerance": 0.50,  # 50 cents tolerance
            "stripe_secret_key": "sk_test_dummy",
            "usdt_rate": 0.0,
        }
        _insert_user(temp_db, user_id=1)
        _insert_order(
            temp_db,
            order_no="ORD_TOL_001",
            amount=10.00,
            credits=1000.0,
            status="pending",
        )
        # Paid 10.30 vs expected 10.00 — within 0.50 tolerance
        sessions = [_make_session(order_no="ORD_TOL_001", amount_total_cents=1030)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["auto_approved"] == 1
        assert result["pending_review"] == 0


# ---------------------------------------------------------------------------
# 9. USDT rate applied
# ---------------------------------------------------------------------------


class TestUSDTRateApplied:
    def test_usdt_order_uses_configured_rate_for_comparison(self, temp_db, cfg_enabled):
        # CNY order at 10.00, USDT rate 0.10 (10 CNY = 1 USDT)
        cfg_enabled.return_value = {
            "enabled": True,
            "lookback_hours": 48,
            "max_auto_approve": 50,
            "amount_tolerance": 0.01,
            "stripe_secret_key": "sk_test_dummy",
            "usdt_rate": 0.10,
        }
        _insert_user(temp_db, user_id=1)
        _insert_order(
            temp_db,
            order_no="ORD_USDT_001",
            amount=10.0,
            credits=1000.0,
            status="pending",
            payment_provider="usdt",
        )
        # Stripe reports amount_total in USDT minor units (1 USDT = 100)
        # 10 CNY * 0.10 rate = 1.00 USDT = 100 cents
        sessions = [_make_session(order_no="ORD_USDT_001", amount_total_cents=100)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["auto_approved"] == 1


# ---------------------------------------------------------------------------
# 10. max-auto-approve cap
# ---------------------------------------------------------------------------


class TestMaxAutoApproveCap:
    def test_cap_stops_approving_after_limit(self, temp_db, cfg_enabled):
        cfg_enabled.return_value = {
            "enabled": True,
            "lookback_hours": 48,
            "max_auto_approve": 2,
            "amount_tolerance": 0.01,
            "stripe_secret_key": "sk_test_dummy",
            "usdt_rate": 0.0,
        }
        _insert_user(temp_db, user_id=1)
        # Three pending orders, all paid correctly.
        for i in range(3):
            _insert_order(
                temp_db,
                order_no=f"ORD_CAP_{i:03d}",
                amount=10.0,
                credits=1000.0,
                status="pending",
                payment_session_id=f"cs_cap_{i}",
            )
        sessions = [
            _make_session(
                session_id=f"cs_cap_{i}",
                order_no=f"ORD_CAP_{i:03d}",
                amount_total_cents=1000,
            )
            for i in range(3)
        ]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["auto_approved"] == 2
        assert any("max auto-approve cap" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# 11. Already-terminal order — no-op + reference backfill
# ---------------------------------------------------------------------------


class TestTerminalNoOp:
    def test_paid_order_with_no_reference_backfills_only(self, temp_db, cfg_enabled):
        _insert_user(temp_db, user_id=1)
        _insert_order(
            temp_db,
            order_no="ORD_PAID_001",
            amount=10.0,
            credits=1000.0,
            status="paid",
            payment_reference=None,
        )
        sessions = [_make_session(order_no="ORD_PAID_001", amount_total_cents=1000)]

        with patch.object(StripeReconciliation, "_list_paid_sessions") as m:
            m.return_value = sessions
            result = StripeReconciliation.run_daily_reconciliation()

        assert result["no_ops"] == 1
        assert result["auto_approved"] == 0

        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT status, payment_reference FROM orders WHERE order_no = ?",
            ("ORD_PAID_001",),
        ).fetchone()
        conn.close()
        assert row["status"] == "paid"
        assert row["payment_reference"] == "pi_test_001"
