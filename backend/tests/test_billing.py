"""Unit tests for the billing system: quote, charge, order, redeem, promo.

These tests run against a temporary SQLite file. The conftest fixture
already provisions the basic schema; here we only depend on those tables
plus a tiny amount of seed data (model_pricing, plans, users) inserted
per-test.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend.database import get_wallet
from backend.services import billing_service, order_service
from backend.services.audit import get_logs, log_action

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
    is_active: int = 1,
    plan_id: int = None,
    balance: float = 0.0,
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO users (id, username, api_key, quota_5h, quota_week,
                           quota_month, monthly_budget, plan_id, is_active)
        VALUES (?, ?, ?, 1000, 10000, 100000, 0, ?, ?)
        """,
        (user_id, username, api_key, plan_id, is_active),
    )
    # Ensure the wallet row exists with all columns the service uses.
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


def _insert_pricing(
    path: str,
    provider: str,
    model_id: str,
    in_price: float,
    out_price: float,
    tier: str = "standard",
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT OR REPLACE INTO model_pricing
            (provider, model_id, input_price_per_1k, output_price_per_1k,
             tier, is_active, is_custom)
        VALUES (?, ?, ?, ?, ?, 1, 0)
        """,
        (provider, model_id, in_price, out_price, tier),
    )
    conn.commit()
    conn.close()


def _insert_plan(
    path: str, *, plan_id: int = 1, code: str = "pro", discount_rate: float = 0.8
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT OR REPLACE INTO plans
            (id, name, code, monthly_price, monthly_credits, discount_rate,
             max_api_keys, max_concurrent, rate_limit_rpm, rate_limit_tpm,
             features, sort_order, is_active)
        VALUES (?, ?, ?, 0, 0, ?, 1, 5, 60, 100000, '[]', 0, 1)
        """,
        (plan_id, code, code, discount_rate),
    )
    conn.commit()
    conn.close()


def _insert_usage_log(
    path: str,
    user_id: int,
    prompt: int,
    completion: int,
    model: str = "gpt-4o",
    provider: str = "openai",
) -> int:
    conn = _connect(path)
    cur = conn.execute(
        """
        INSERT INTO usage_logs
            (user_id, endpoint, model, provider, prompt_tokens,
             completion_tokens, total_tokens, response_time_ms, status_code)
        VALUES (?, '/v1/chat', ?, ?, ?, ?, ?, 100, 200)
        """,
        (user_id, model, provider, prompt, completion, prompt + completion),
    )
    log_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return log_id


# ---------------------------------------------------------------------------
# quote_cost
# ---------------------------------------------------------------------------


class TestQuoteCost:
    def _setup(self, temp_db, *, discount=0.8):
        _insert_plan(temp_db, plan_id=1, code="pro", discount_rate=discount)
        _insert_user(temp_db, user_id=1, plan_id=1)
        _insert_pricing(temp_db, "openai", "gpt-4o", 1.75, 7.0)
        return temp_db

    def test_wallet_quote(self, temp_db):
        self._setup(temp_db, discount=0.8)
        # 1K input @ 1.75 + 1K output @ 7.0 = 8.75 credits, *0.8 = 7.0
        quote = billing_service.quote_cost(
            user_id=1,
            provider="openai",
            model_id="gpt-4o",
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert abs(quote["cost_credits"] - 7.0) < 1e-6
        assert abs(quote["input_price"] - 1.75) < 1e-9
        assert abs(quote["output_price"] - 7.0) < 1e-9
        assert abs(quote["discount_rate"] - 0.8) < 1e-9
        assert quote["plan_code"] == "pro"

    def test_no_pricing_returns_zero(self, temp_db):
        self._setup(temp_db)
        quote = billing_service.quote_cost(
            user_id=1,
            provider="unknown",
            model_id="missing",
            prompt_tokens=1000,
            completion_tokens=1000,
        )
        assert quote["cost_credits"] == 0.0


# ---------------------------------------------------------------------------
# charge_for_usage
# ---------------------------------------------------------------------------


class TestChargeForUsage:
    def _setup(self, temp_db, *, balance: float = 100.0):
        _insert_plan(temp_db, plan_id=1, code="basic", discount_rate=0.9)
        _insert_user(temp_db, user_id=1, plan_id=1, balance=balance)
        _insert_pricing(temp_db, "openai", "gpt-4o-mini", 0.105, 0.42)
        return temp_db

    def test_charge_for_usage(self, temp_db):
        self._setup(temp_db, balance=100.0)
        log_id = _insert_usage_log(
            temp_db, user_id=1, prompt=1000, completion=1000, model="gpt-4o-mini", provider="openai"
        )
        ok = billing_service.charge_for_usage(user_id=1, usage_log_id=log_id)
        assert ok is True
        # (1000/1000 * 0.105 + 1000/1000 * 0.42) * 0.9 = 0.4725
        wallet = get_wallet(1)
        assert abs(wallet["balance"] - (100.0 - 0.4725)) < 1e-6
        # usage_logs.cost_credits was written back
        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT cost_credits, error_message FROM usage_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        conn.close()
        assert abs(row["cost_credits"] - 0.4725) < 1e-6
        assert row["error_message"] is None

    def test_charge_insufficient_balance(self, temp_db):
        self._setup(temp_db, balance=0.0)
        log_id = _insert_usage_log(
            temp_db, user_id=1, prompt=1000, completion=1000, model="gpt-4o-mini", provider="openai"
        )
        ok = billing_service.charge_for_usage(user_id=1, usage_log_id=log_id)
        assert ok is False
        # The cost field should be 0 with an error annotation.
        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT cost_credits, error_message FROM usage_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
        conn.close()
        assert row["cost_credits"] == 0
        assert row["error_message"] and "insufficient" in row["error_message"]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class TestOrders:
    def _setup(self, temp_db, *, balance: float = 0.0):
        _insert_user(temp_db, user_id=1, balance=balance)
        return temp_db

    def test_order_create_and_approve(self, temp_db):
        self._setup(temp_db, balance=0.0)
        order = order_service.create_order(
            user_id=1,
            amount=50.0,
            payment_method="alipay",
        )
        assert order["status"] == "pending"
        assert order["credits"] == 5000.0  # 1 元 = 100 credits
        assert order["order_no"].startswith("ORD")

        ok = order_service.approve_order(int(order["id"]), admin_id=99)
        assert ok is True
        wallet = get_wallet(1)
        assert abs(wallet["balance"] - 5000.0) < 1e-6
        assert abs(wallet["total_recharged"] - 5000.0) < 1e-6

        # Approve again is a no-op
        ok2 = order_service.approve_order(int(order["id"]), admin_id=99)
        assert ok2 is False

    def test_reject_order(self, temp_db):
        self._setup(temp_db)
        order = order_service.create_order(user_id=1, amount=10.0)
        ok = order_service.reject_order(int(order["id"]), admin_id=1, reason="bad")
        assert ok is True
        refetched = order_service.get_order(int(order["id"]))
        assert refetched["status"] == "failed"


# ---------------------------------------------------------------------------
# Redeem codes
# ---------------------------------------------------------------------------


class TestRedeem:
    def _setup(self, temp_db, *, balance: float = 0.0):
        _insert_plan(temp_db, plan_id=10, code="pro")
        _insert_user(temp_db, user_id=1, balance=balance)
        return temp_db

    def _make_code(
        self,
        temp_db,
        code: str = "REDEEM123",
        type_: str = "credits",
        value: float = 1000.0,
        expires_in_days: int = 30,
        plan_id: int = None,
    ) -> str:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        conn = _connect(temp_db)
        conn.execute(
            """
            INSERT INTO redeem_codes
                (code, type, value, plan_id, max_uses, expires_at, is_active)
            VALUES (?, ?, ?, ?, 1, ?, 1)
            """,
            (code, type_, value, plan_id, expires_str),
        )
        conn.commit()
        conn.close()
        return code

    def test_redeem_code_credits(self, temp_db):
        self._setup(temp_db, balance=0.0)
        self._make_code(temp_db, code="CREDITS100", value=500.0)
        result = order_service.redeem_code("CREDITS100", user_id=1)
        assert result["type"] == "credits"
        assert result["credits_granted"] == 500.0
        wallet = get_wallet(1)
        assert abs(wallet["balance"] - 500.0) < 1e-6

    def test_redeem_code_expired(self, temp_db):
        self._setup(temp_db)
        self._make_code(temp_db, code="EXPIRED1", expires_in_days=-1)
        with pytest.raises(ValueError):
            order_service.redeem_code("EXPIRED1", user_id=1)

    def test_redeem_code_plan_days(self, temp_db):
        self._setup(temp_db)
        self._make_code(temp_db, code="PLANDAYS", type_="plan_days", value=30, plan_id=10)
        result = order_service.redeem_code("PLANDAYS", user_id=1)
        assert result["type"] == "plan_days"
        assert result["plan_id"] == 10
        # A subscription row was created
        conn = _connect(temp_db)
        sub = conn.execute(
            "SELECT plan_id, status FROM subscriptions WHERE user_id = ?",
            (1,),
        ).fetchone()
        conn.close()
        assert sub is not None
        assert sub["plan_id"] == 10
        assert sub["status"] == "active"

    def test_redeem_single_use(self, temp_db):
        self._setup(temp_db)
        self._make_code(temp_db, code="ONESHOT")
        order_service.redeem_code("ONESHOT", user_id=1)
        with pytest.raises(ValueError):
            order_service.redeem_code("ONESHOT", user_id=1)


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------


class TestPromo:
    def _setup(self, temp_db):
        _insert_user(temp_db, user_id=1)
        return temp_db

    def _make_promo(self, temp_db, code: str, type_: str, value: float) -> None:
        conn = _connect(temp_db)
        conn.execute(
            """
            INSERT INTO promo_codes
                (code, type, value, max_uses, per_user_limit, is_active)
            VALUES (?, ?, ?, 100, 1, 1)
            """,
            (code, type_, value),
        )
        conn.commit()
        conn.close()

    def test_promo_code_apply_discount_percent(self, temp_db):
        self._setup(temp_db)
        self._make_promo(temp_db, "WELCOME10", "discount_percent", 10)
        order = order_service.create_order(
            user_id=1,
            amount=100.0,
            promo_code="WELCOME10",
        )
        # 100 元 → 10000 credits base, 10% off = 1000 off → 9000 credits
        assert abs(order["credits"] - 9000.0) < 1e-6

    def test_promo_code_bonus_credits(self, temp_db):
        self._setup(temp_db)
        self._make_promo(temp_db, "EXTRA500", "bonus_credits", 500)
        order = order_service.create_order(
            user_id=1,
            amount=20.0,
            promo_code="EXTRA500",
        )
        # 20 元 → 2000 credits + 500 bonus = 2500
        assert abs(order["credits"] - 2000.0) < 1e-6
        assert abs(order["bonus_credits"] - 500.0) < 1e-6

    def test_invalid_promo(self, temp_db):
        self._setup(temp_db)
        with pytest.raises(ValueError):
            order_service.create_order(
                user_id=1,
                amount=10.0,
                promo_code="NOPE",
            )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAudit:
    def test_log_and_query(self, temp_db):
        log_action(
            actor_id=42,
            actor_type="user",
            action="order.create",
            target_type="order",
            target_id=7,
            details={"foo": "bar"},
            ip_address="127.0.0.1",
        )
        rows = get_logs(actor_id=42, limit=10)
        assert len(rows) == 1
        assert rows[0]["action"] == "order.create"
        assert rows[0]["actor_id"] == 42
        assert rows[0]["details"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Admin API keys (platform-wide management)
# ---------------------------------------------------------------------------


class TestAdminApiKeys:
    """Direct DB-level tests for the admin /api-keys listing endpoint.

    We don't spin up the FastAPI app here; the endpoint logic is a
    thin SQL wrapper and verifying it at the SQL level is sufficient
    to lock in the join + filter behavior.
    """

    def _seed(self, temp_db):
        _insert_user(temp_db, user_id=1, username="alice", api_key="ak_alice")
        _insert_user(temp_db, user_id=2, username="bob", api_key="ak_bob")
        conn = _connect(temp_db)
        conn.executescript(
            """
            INSERT INTO api_keys
                (user_id, name, key_hash, key_prefix, key_mask,
                 monthly_token_limit, monthly_credit_limit, is_active, allowed_models)
            VALUES
                (1, 'alice-prod',  'h1', 'sk-aaaaaaaa', 'sk-aa...aaaa', 1000, NULL, 1, '["m1"]'),
                (1, 'alice-staging','h2', 'sk-bbbbbbbb', 'sk-bb...bbbb', NULL,  50,   1, NULL),
                (2, 'bob-mobile',  'h3', 'sk-cccccccc', 'sk-cc...cccc', NULL,  NULL, 0, '["m2","m3"]');
            """
        )
        conn.commit()
        conn.close()

    def test_list_all_keys(self, temp_db):
        self._seed(temp_db)
        from backend.routes.admin_billing import list_api_keys

        rows = list_api_keys(limit=100, offset=0)
        # Endpoint is async; sync shim for unit test
        import asyncio

        result = asyncio.run(rows)
        assert len(result) == 3
        # Newest first by id DESC
        assert result[0]["username"] == "bob"
        assert result[0]["name"] == "bob-mobile"
        # Allowed models was stored as JSON string and should be parsed.
        assert result[0]["allowed_models"] == ["m2", "m3"]
        # Internal hash must never leak.
        for r in result:
            assert "key_hash" not in r
            assert "key_mask" in r  # masked form is OK

    def test_list_keys_filtered_by_user(self, temp_db):
        self._seed(temp_db)
        import asyncio

        from backend.routes.admin_billing import list_api_keys

        result = asyncio.run(list_api_keys(user_id=1, limit=100, offset=0))
        assert len(result) == 2
        assert all(r["user_id"] == 1 for r in result)

    def test_revoke_unknown_returns_404(self, temp_db):
        self._seed(temp_db)
        import asyncio

        from fastapi import HTTPException

        from backend.routes.admin_billing import revoke_api_key

        try:
            asyncio.run(revoke_api_key(key_id=999, request=None))
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            pytest.fail("Expected HTTPException for missing key")

    def test_revoke_flips_is_active(self, temp_db):
        self._seed(temp_db)
        import asyncio

        from backend.routes.admin_billing import revoke_api_key

        asyncio.run(revoke_api_key(key_id=1, request=None))
        conn = _connect(temp_db)
        row = conn.execute("SELECT is_active FROM api_keys WHERE id = 1").fetchone()
        conn.close()
        assert int(row[0]) == 0


# ---------------------------------------------------------------------------
# auto_activate_free_plan — expires_at stamping (Problem 1)
# ---------------------------------------------------------------------------


class TestAutoActivateFreePlanExpiresAt:
    """Regression: the free-plan initial credits grant must stamp
    ``expires_at`` on the ``wallet_transactions`` row, same as the
    renewal / upgrade paths do via ``grant_credits``.

    Before the fix, ``auto_activate_free_plan`` used a raw INSERT
    without an ``expires_at`` column, so the first-time grant never
    expired even when ``CREDITS_EXPIRE_DAYS`` was configured —
    inconsistent with renewals of the same plan.
    """

    def _seed_free_plan(self, path: str, *, monthly_credits: float = 500.0) -> None:
        conn = _connect(path)
        conn.execute(
            """
            INSERT OR REPLACE INTO plans
                (id, name, code, monthly_price, monthly_credits,
                 discount_rate, max_api_keys, max_concurrent,
                 rate_limit_rpm, rate_limit_tpm, features, sort_order,
                 is_active)
            VALUES (1, 'Free', 'free', 0, ?, 1.0, 1, 5, 60, 100000,
                    '[]', 0, 1)
            """,
            (monthly_credits,),
        )
        conn.commit()
        conn.close()

    def test_free_plan_bonus_stamps_expires_at(self, temp_db, monkeypatch):
        """When CREDITS_EXPIRE_DAYS > 0, the bonus row from
        auto_activate_free_plan must carry a non-NULL expires_at."""
        import backend.config as config_mod

        monkeypatch.setattr(config_mod.Config, "CREDITS_EXPIRE_DAYS", 30)

        _insert_user(temp_db, user_id=1)
        self._seed_free_plan(temp_db, monthly_credits=500.0)

        from backend.services.user_service import UserService

        UserService.auto_activate_free_plan(1)

        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT type, amount, note, expires_at, expiry_debited"
            " FROM wallet_transactions WHERE user_id = 1"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["type"] == "bonus"
        assert abs(float(row["amount"]) - 500.0) < 1e-6
        assert row["expires_at"] is not None, (
            "free-plan bonus must stamp expires_at when "
            "CREDITS_EXPIRE_DAYS > 0"
        )
        assert int(row["expiry_debited"]) == 0

    def test_free_plan_bonus_expires_at_none_when_disabled(self, temp_db, monkeypatch):
        """When CREDITS_EXPIRE_DAYS == 0 (default), expires_at is NULL
        — preserving the legacy 'credits never expire' behaviour."""
        import backend.config as config_mod

        monkeypatch.setattr(config_mod.Config, "CREDITS_EXPIRE_DAYS", 0)

        _insert_user(temp_db, user_id=1)
        self._seed_free_plan(temp_db, monthly_credits=500.0)

        from backend.services.user_service import UserService

        UserService.auto_activate_free_plan(1)

        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT expires_at FROM wallet_transactions WHERE user_id = 1"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["expires_at"] is None

    def test_idempotent_no_duplicate_grant(self, temp_db, monkeypatch):
        """Calling auto_activate_free_plan twice must not grant the
        bonus credits a second time."""
        import backend.config as config_mod

        monkeypatch.setattr(config_mod.Config, "CREDITS_EXPIRE_DAYS", 30)

        _insert_user(temp_db, user_id=1)
        self._seed_free_plan(temp_db, monthly_credits=500.0)

        from backend.services.user_service import UserService

        UserService.auto_activate_free_plan(1)
        UserService.auto_activate_free_plan(1)

        conn = _connect(temp_db)
        rows = conn.execute(
            "SELECT amount FROM wallet_transactions WHERE user_id = 1"
            " AND type = 'bonus'"
        ).fetchall()
        conn.close()
        assert len(rows) == 1, "second call must not duplicate the bonus"
        assert abs(float(rows[0]["amount"]) - 500.0) < 1e-6

    def test_atomic_with_external_conn_rolls_back_on_failure(
        self, temp_db, monkeypatch
    ):
        """When called with an external conn, a failure must propagate
        so the caller's transaction rolls back — no half-written
        subscription row should survive."""
        import backend.config as config_mod

        monkeypatch.setattr(config_mod.Config, "CREDITS_EXPIRE_DAYS", 30)

        _insert_user(temp_db, user_id=1)
        self._seed_free_plan(temp_db, monthly_credits=500.0)

        from backend.database import get_db_context
        from backend.services.user_service import UserService

        # Simulate a failure inside the transaction by dropping the
        # wallets table mid-flight (auto_activate_free_plan's
        # grant_credits call will then fail).
        with pytest.raises(Exception):
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("DROP TABLE wallets")
                UserService.auto_activate_free_plan(1, conn=conn)

        # No subscription should exist because the transaction rolled
        # back.
        conn = _connect(temp_db)
        sub_count = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE user_id = 1"
        ).fetchone()
        conn.close()
        assert int(sub_count["c"]) == 0


# ---------------------------------------------------------------------------
# _handle_subscription_activation — extension vs reset (Problem 3)
# ---------------------------------------------------------------------------


class TestSubscriptionActivationExtension:
    """Regression: paying again for an already-active, unexpired
    subscription must *extend* the expiry — not reset it to now+30d.

    Before the fix, the existing-branch of
    ``_handle_subscription_activation`` unconditionally did
    ``UPDATE subscriptions SET started_at=now, expires_at=now+30d,
    credits_used_this_period=0``. That silently discarded the
    remaining paid days (user paid twice for ~30 days instead of
    stacking to ~60) while still granting a second monthly_credits.
    """

    def _seed_plan_and_user(
        self,
        path: str,
        *,
        plan_id: int = 5,
        monthly_credits: float = 100.0,
        balance: float = 0.0,
    ) -> None:
        conn = _connect(path)
        conn.execute(
            """
            INSERT OR REPLACE INTO plans
                (id, name, code, monthly_price, monthly_credits,
                 discount_rate, max_api_keys, max_concurrent,
                 rate_limit_rpm, rate_limit_tpm, features, sort_order,
                 is_active)
            VALUES (?, 'Pro', 'pro', 30, ?, 1.0, 1, 5, 60, 100000,
                    '[]', 0, 1)
            """,
            (plan_id, monthly_credits),
        )
        conn.commit()
        conn.close()
        _insert_user(path, user_id=1, plan_id=plan_id, balance=balance)

    def _make_sub_order(
        self,
        path: str,
        *,
        user_id: int,
        plan_id: int,
        auto_renew: bool = True,
        amount: float = 30.0,
    ) -> int:
        """Create a pending order whose note carries the subscription
        metadata that ``_handle_subscription_activation`` parses."""
        import json as _json

        note = _json.dumps({"plan_id": plan_id, "auto_renew": auto_renew})
        conn = _connect(path)
        cur = conn.execute(
            """
            INSERT INTO orders (order_no, user_id, amount, credits,
                                bonus_credits, payment_method, status,
                                note)
            VALUES (?, ?, ?, 0, 0, 'alipay', 'pending', ?)
            """,
            (f"ORD-test-{user_id}-{plan_id}", user_id, amount, note),
        )
        order_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return order_id

    def test_extends_active_unexpired_subscription(self, temp_db):
        """Active sub with 25 days left → after re-activation the
        expiry is ~55 days from now (25 remaining + 30 added), not
        ~30 days (which would be a reset)."""
        self._seed_plan_and_user(temp_db, plan_id=5, monthly_credits=100.0)
        now = datetime.now(timezone.utc)

        # Pre-existing active sub expiring in 25 days.
        _insert_sub_existing(
            temp_db,
            user_id=1,
            plan_id=5,
            status="active",
            started_at=(now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(now + timedelta(days=25)).strftime("%Y-%m-%d %H:%M:%S"),
        )
        original_expires = now + timedelta(days=25)

        order_id = self._make_sub_order(temp_db, user_id=1, plan_id=5)
        ok = order_service.approve_order(order_id, admin_id=99)
        assert ok is True

        conn = _connect(temp_db)
        sub = conn.execute(
            "SELECT status, expires_at, started_at, credits_used_this_period"
            " FROM subscriptions WHERE user_id = 1 AND plan_id = 5"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        wt_rows = conn.execute(
            "SELECT amount, type FROM wallet_transactions"
            " WHERE user_id = 1 AND type = 'renew'"
        ).fetchall()
        order_note = conn.execute(
            "SELECT note FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        conn.close()

        assert sub is not None
        assert sub["status"] == "active"
        # Extension: new expiry ≈ original_expiry + 30 days (allow
        # a few seconds of clock skew).
        new_expires = datetime.strptime(sub["expires_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        expected = original_expires + timedelta(days=30)
        assert abs((new_expires - expected).total_seconds()) < 60, (
            f"expected ~{expected}, got {new_expires}"
        )
        # monthly_credits still granted (user paid).
        assert len(wt_rows) == 1
        assert abs(float(wt_rows[0]["amount"]) - 100.0) < 1e-6
        # orders.note carries the extension record.
        assert "subscription_extension" in (order_note["note"] or "")

    def test_resets_expired_subscription(self, temp_db):
        """Expired sub → re-activation resets started_at and
        expires_at to a fresh 30-day window (no extension)."""
        self._seed_plan_and_user(temp_db, plan_id=5, monthly_credits=100.0)
        now = datetime.now(timezone.utc)

        _insert_sub_existing(
            temp_db,
            user_id=1,
            plan_id=5,
            status="expired",
            started_at=(now - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S"),
        )

        order_id = self._make_sub_order(temp_db, user_id=1, plan_id=5)
        ok = order_service.approve_order(order_id, admin_id=99)
        assert ok is True

        conn = _connect(temp_db)
        sub = conn.execute(
            "SELECT status, expires_at FROM subscriptions WHERE user_id = 1"
            " AND plan_id = 5 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert sub is not None
        assert sub["status"] == "active"
        new_expires = datetime.strptime(sub["expires_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        # Reset: new expiry ≈ now + 30 days, not old_expiry + 30.
        assert abs((new_expires - (now + timedelta(days=30))).total_seconds()) < 60

    def test_extends_does_not_reset_credits_used(self, temp_db):
        """Extending must preserve ``credits_used_this_period`` —
        the user's progress into their quota should not be wiped."""
        self._seed_plan_and_user(temp_db, plan_id=5, monthly_credits=100.0)
        now = datetime.now(timezone.utc)

        _insert_sub_existing(
            temp_db,
            user_id=1,
            plan_id=5,
            status="active",
            started_at=(now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            expires_at=(now + timedelta(days=25)).strftime("%Y-%m-%d %H:%M:%S"),
            credits_used_this_period=42,
        )

        order_id = self._make_sub_order(temp_db, user_id=1, plan_id=5)
        order_service.approve_order(order_id, admin_id=99)

        conn = _connect(temp_db)
        sub = conn.execute(
            "SELECT credits_used_this_period FROM subscriptions"
            " WHERE user_id = 1 AND plan_id = 5 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert int(sub["credits_used_this_period"]) == 42, (
            "extension must not reset credits_used_this_period"
        )


def _insert_sub_existing(
    path: str,
    *,
    user_id: int,
    plan_id: int,
    status: str = "active",
    started_at: str,
    expires_at: str,
    auto_renew: int = 1,
    credits_used_this_period: int = 0,
) -> int:
    conn = _connect(path)
    cur = conn.execute(
        """INSERT INTO subscriptions
           (user_id, plan_id, status, started_at, expires_at,
            credits_used_this_period, auto_renew)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, plan_id, status, started_at, expires_at,
         credits_used_this_period, auto_renew),
    )
    sub_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return sub_id
