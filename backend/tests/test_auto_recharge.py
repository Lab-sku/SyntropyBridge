"""Integration tests for Phase 11-D auto-recharge trigger.

Covers:
  - Trigger when balance drops below threshold
  - No trigger when balance stays above threshold
  - No trigger when auto_recharge is disabled
  - Stripe customer on file → auto-approved (status=paid)
  - No Stripe customer → pending order
  - Dedup: only one order per hour
  - Dedup: new order after 1 hour
  - End-to-end flow
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.database import get_wallet, set_setting
from backend.services import billing_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_user(
    path: str,
    *,
    user_id: int = 1,
    username: str = "alice",
    api_key: str = "ak_test_alice",
    balance: float = 0.0,
    auto_recharge_enabled: int = 1,
    auto_recharge_threshold: float = 100.0,
    auto_recharge_amount: float = 500.0,
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO users (id, username, api_key, quota_5h, quota_week,
                           quota_month, monthly_budget, is_active)
        VALUES (?, ?, ?, 1000, 10000, 100000, 0, 1)
        """,
        (user_id, username, api_key),
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO wallets
            (user_id, balance, total_recharged, total_consumed, frozen,
             auto_recharge_enabled, auto_recharge_threshold, auto_recharge_amount)
        VALUES (?, ?, 0, 0, 0, ?, ?, ?)
        """,
        (user_id, balance, auto_recharge_enabled, auto_recharge_threshold, auto_recharge_amount),
    )
    conn.commit()
    conn.close()


def _insert_usage_log(
    path: str,
    user_id: int,
    cost: float = 0.5,
    model: str = "gpt-4o",
    provider: str = "openai",
) -> int:
    conn = _connect(path)
    cur = conn.execute(
        """
        INSERT INTO usage_logs
            (user_id, endpoint, model, provider, prompt_tokens,
             completion_tokens, total_tokens, cost_credits,
             response_time_ms, status_code)
        VALUES (?, '/v1/chat', ?, ?, 100, 50, 150, ?, 100, 200)
        """,
        (user_id, model, provider, cost),
    )
    log_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return log_id


def _count_orders(path: str, user_id: int) -> int:
    conn = _connect(path)
    row = conn.execute("SELECT COUNT(*) FROM orders WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row[0]


def _get_orders(path: str, user_id: int) -> list[dict]:
    conn = _connect(path)
    rows = conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _count_notifications(path: str, user_id: int, type_: str) -> int:
    conn = _connect(path)
    row = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND type = ?",
        (user_id, type_),
    ).fetchone()
    conn.close()
    return row[0]


# =========================================================================
# Trigger conditions
# =========================================================================


class TestAutoRechargeTrigger:
    def test_trigger_when_balance_below_threshold(self, temp_db):
        """Balance drops below threshold → pending order created + notification."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=60.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        billing_service._maybe_trigger_auto_recharge(1)
        orders = _get_orders(temp_db, 1)
        assert len(orders) == 1
        assert orders[0]["payment_method"] == "auto_recharge"
        # Amount is in yuan, credits = amount * 100
        assert abs(float(orders[0]["credits"]) - 50000.0) < 1e-6
        # Notification emitted
        assert _count_notifications(temp_db, 1, "auto_recharge_triggered") == 1

    def test_no_trigger_when_balance_above_threshold(self, temp_db):
        """Balance above threshold → no order created."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=150.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 0

    def test_no_trigger_when_disabled(self, temp_db):
        """auto_recharge_enabled=0 + balance below threshold → no order."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=50.0,
            auto_recharge_enabled=0,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 0

    def test_stripe_customer_no_longer_auto_approves(self, temp_db):
        """Stripe customer ID on file → order stays pending.

        Previously the auto-recharge path called ``approve_order`` when a
        Stripe customer id was stored in settings, which credited the
        wallet WITHOUT actually charging the customer via Stripe —
        effectively granting free credits. The fix removes the
        auto-approve branch entirely; the order is left pending so the
        user completes payment via the normal checkout flow.
        """
        _insert_user(
            temp_db,
            user_id=1,
            balance=50.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        set_setting("stripe_customer_1", "cus_test_123")
        billing_service._maybe_trigger_auto_recharge(1)
        orders = _get_orders(temp_db, 1)
        assert len(orders) == 1
        # Order must NOT be auto-approved — that would grant uncharged credits.
        assert orders[0]["status"] == "pending"
        # Wallet should still be at the original balance.
        wallet = get_wallet(1)
        assert abs(wallet["balance"] - 50.0) < 1e-6

    def test_no_stripe_customer_stays_pending(self, temp_db):
        """No Stripe customer ID → order stays pending."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=50.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        # No stripe_customer_1 setting
        billing_service._maybe_trigger_auto_recharge(1)
        orders = _get_orders(temp_db, 1)
        assert len(orders) == 1
        assert orders[0]["status"] == "pending"


# =========================================================================
# Deduplication
# =========================================================================


class TestAutoRechargeDedup:
    def test_dedup_same_hour(self, temp_db):
        """Two calls within 1 hour → only one order."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=50.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        billing_service._maybe_trigger_auto_recharge(1)
        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 1
        assert _count_notifications(temp_db, 1, "auto_recharge_triggered") == 1

    def test_new_order_after_one_hour(self, temp_db):
        """After previous order is resolved, another trigger creates a new order."""
        _insert_user(
            temp_db,
            user_id=1,
            balance=50.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )
        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 1

        # Mark the first order as paid (resolved) so dedup allows a new one
        conn = _connect(temp_db)
        conn.execute(
            "UPDATE orders SET status = 'paid' WHERE user_id = 1 AND payment_method = 'auto_recharge'"
        )
        conn.commit()
        conn.close()

        # Re-set balance below threshold
        conn = _connect(temp_db)
        conn.execute("UPDATE wallets SET balance = 50.0 WHERE user_id = 1")
        conn.commit()
        conn.close()

        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 2


# =========================================================================
# End-to-end test
# =========================================================================


class TestAutoRechargeE2E:
    def test_full_flow(self, temp_db):
        """
        1. Create user, set auto-recharge (threshold=100, amount=500)
        2. Set initial balance = 120
        3. Simulate API call that costs 50 → new balance 70 (below threshold)
        4. Assert: order with credits for 500 yuan exists, status=pending
        5. Assert: notification of type auto_recharge_triggered exists
        6. Trigger again → still only 1 order (deduped)
        """
        _insert_user(
            temp_db,
            user_id=1,
            balance=120.0,
            auto_recharge_enabled=1,
            auto_recharge_threshold=100.0,
            auto_recharge_amount=500.0,
        )

        # Simulate a charge that brings balance to 70
        conn = _connect(temp_db)
        conn.execute("UPDATE wallets SET balance = 70.0 WHERE user_id = 1")
        conn.commit()
        conn.close()

        # Trigger auto-recharge
        billing_service._maybe_trigger_auto_recharge(1)

        # Assert: order created
        orders = _get_orders(temp_db, 1)
        assert len(orders) == 1
        assert orders[0]["status"] == "pending"
        assert orders[0]["payment_method"] == "auto_recharge"
        # 500 yuan * 100 credits/yuan = 50000 credits
        assert abs(float(orders[0]["credits"]) - 50000.0) < 1e-6

        # Assert: notification
        assert _count_notifications(temp_db, 1, "auto_recharge_triggered") == 1

        # Trigger again (deduped)
        billing_service._maybe_trigger_auto_recharge(1)
        assert _count_orders(temp_db, 1) == 1
