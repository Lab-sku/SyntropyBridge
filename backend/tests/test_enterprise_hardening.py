"""Tests for the new enterprise-grade building blocks added in this milestone:

* :mod:`backend.utils.circuit_breaker`
* :mod:`backend.utils.idempotency`
* :mod:`backend.utils.log_safety`
* :mod:`backend.utils.db_pool`
* :mod:`backend.services.api_key_service`
* :mod:`backend.services.lockout`
* :mod:`backend.database.list_effective_pricing` / `get_pricing_for_model_list`
* :mod:`backend.routes.openai_compat._model_to_openai` with ``pricing=``

These are pure unit tests — they don't hit the network, they exercise
the failure paths in isolation, and they don't need a running server.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch):
    """Redirect database access to a throwaway file so the tests
    don't pollute the real DB.

    The fixture also tears down the singleton :class:`DatabasePool` and
    the in-process usage-window cache so cached connections from an
    earlier test (still pointing at a different temp file) can't
    silently race with inserts into *this* test's file.
    """
    import backend.database as db

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE wallets (
            user_id INTEGER PRIMARY KEY,
            balance NUMERIC DEFAULT 0,
            frozen NUMERIC DEFAULT 0,
            total_recharged NUMERIC DEFAULT 0,
            total_consumed NUMERIC DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type VARCHAR(20) NOT NULL,
            amount NUMERIC NOT NULL,
            balance_after NUMERIC NOT NULL,
            related_type VARCHAR(20),
            related_id INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name VARCHAR(100) NOT NULL,
            key_hash VARCHAR(128) UNIQUE NOT NULL,
            key_prefix VARCHAR(20) NOT NULL,
            key_mask VARCHAR(30),
            monthly_token_limit INTEGER,
            monthly_credit_limit NUMERIC,
            allowed_models TEXT,
            denied_models TEXT,
            is_active INTEGER DEFAULT 1,
            last_used_at TIMESTAMP,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE model_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider VARCHAR(50) NOT NULL,
            model_id VARCHAR(200) NOT NULL,
            input_price_per_1k NUMERIC DEFAULT 0,
            output_price_per_1k NUMERIC DEFAULT 0,
            tier VARCHAR(20) DEFAULT 'standard',
            is_active INTEGER DEFAULT 1,
            is_custom INTEGER DEFAULT 0,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER,
            UNIQUE(provider, model_id, tier)
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DATABASE_PATH", path)

    # Reset the singleton pool so cached connections from a previous
    # test are dropped before this test writes anything.
    try:
        from backend.utils.db_pool import get_pool

        get_pool().close_all()
    except Exception:
        pass
    try:
        db._usage_windows_cache.clear()
    except Exception:
        pass

    yield path

    try:
        from backend.utils.db_pool import get_pool

        get_pool().close_all()
    except Exception:
        pass
    try:
        db._usage_windows_cache.clear()
    except Exception:
        pass

    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# circuit_breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_threshold():
    from backend.utils.circuit_breaker import CircuitBreaker, CircuitOpenError

    br = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=0.1)

    async def bad():
        raise RuntimeError("boom")

    async def good():
        return 42

    async def runner():
        # 3 failures in a row should open the breaker
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await br.call(bad)
        # The 4th call should short-circuit
        with pytest.raises(CircuitOpenError):
            await br.call(bad)
        # After cooldown the breaker half-opens; a successful call closes it
        await asyncio.sleep(0.15)
        assert await br.call(good) == 42
        status = br.status()
        assert status["failures"] == 0
        assert status["open"] is False

    asyncio.run(runner())


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotency_reserve_and_hit(temp_db):
    from backend.utils import idempotency

    res = idempotency.check_or_reserve(
        key="abc-123",
        method="POST",
        route="/api/test",
        body={"foo": "bar"},
    )
    assert res.hit is False
    assert res.reserved is True

    # finalize and re-check
    idempotency.finalize(
        key="abc-123",
        method="POST",
        route="/api/test",
        status_code=200,
        response_body={"ok": True},
    )
    res2 = idempotency.check_or_reserve(
        key="abc-123",
        method="POST",
        route="/api/test",
        body={"foo": "bar"},
    )
    assert res2.hit is True
    assert res2.status_code == 200
    assert res2.response_body == {"ok": True}


def test_idempotency_rejects_different_body(temp_db):
    from backend.utils import idempotency

    idempotency.check_or_reserve(
        key="k1",
        method="POST",
        route="/x",
        body={"a": 1},
    )
    idempotency.finalize(
        key="k1",
        method="POST",
        route="/x",
        status_code=200,
        response_body={"ok": True},
    )
    res = idempotency.check_or_reserve(
        key="k1",
        method="POST",
        route="/x",
        body={"a": 2},
    )
    assert res.hit is True
    assert res.status_code == 422


def test_idempotency_release(temp_db):
    from backend.utils import idempotency

    idempotency.check_or_reserve(key="release-me", method="POST", route="/x", body=None)
    idempotency.release(key="release-me", method="POST", route="/x")
    res = idempotency.check_or_reserve(key="release-me", method="POST", route="/x", body=None)
    assert res.hit is False
    assert res.reserved is True


# ---------------------------------------------------------------------------
# log_safety
# ---------------------------------------------------------------------------


def test_redact_payload_masks_secrets():
    from backend.utils.log_safety import redact_payload

    payload = {
        "username": "alice",
        "password": "super-secret",
        "api_key": "sk-abcdefghij12345",
        "nested": {"token": "eyJxxxxxx.yyyyyy.zzzzzz", "user": "bob"},
    }
    redacted = redact_payload(payload)
    assert redacted["username"] == "alice"
    assert "***" in redacted["password"]
    assert "***" in redacted["api_key"]
    assert "***" in redacted["nested"]["token"]
    assert redacted["nested"]["user"] == "bob"


def test_scrub_text_in_place():
    from backend.utils.log_safety import _scrub_text

    out = _scrub_text("Authorization: Bearer sk-1234567890abcdefghij")
    assert "sk-***" not in out
    assert "***" in out
    # Email — keep the domain
    out = _scrub_text("contact alice@example.com for details")
    assert "***@example.com" in out
    # JWT-style token in the body
    out = _scrub_text("token: eyJabc12345.def67890.ghi09876")
    assert "jwt-***" in out or "***" in out


# ---------------------------------------------------------------------------
# db_pool
# ---------------------------------------------------------------------------


def test_db_pool_acquire_release():
    from backend.utils.db_pool import get_pool

    pool = get_pool()
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
    stats = pool.stats()
    assert "max_size" in stats
    assert "in_use" in stats


# ---------------------------------------------------------------------------
# api_key_service
# ---------------------------------------------------------------------------


def test_api_key_issue_and_lookup(temp_db):
    from backend.services.api_key_service import hash_key, issue_key, lookup_by_hash

    issued = issue_key(
        user_id=42,
        name="primary",
        monthly_token_limit=1000,
        monthly_credit_limit=10.0,
        allowed_models=["gpt-4o"],
    )
    assert issued.secret.startswith("sk-")
    assert len(issued.secret) > 30

    row = lookup_by_hash(hash_key(issued.secret))
    assert row is not None
    assert row["user_id"] == 42
    assert row["name"] == "primary"
    assert row["monthly_token_limit"] == 1000
    assert "gpt-4o" in row["allowed_models"]


def test_api_key_rotate_invalidates_old_secret(temp_db):
    from backend.services.api_key_service import hash_key, issue_key, lookup_by_hash, rotate_key

    issued = issue_key(user_id=1, name="k")
    old_secret = issued.secret
    rotated = rotate_key(issued.id, user_id=1)
    assert rotated.secret != old_secret
    assert lookup_by_hash(hash_key(old_secret)) is None
    assert lookup_by_hash(hash_key(rotated.secret)) is not None


def test_api_key_revoke(temp_db):
    from backend.services.api_key_service import hash_key, issue_key, lookup_by_hash, revoke_key

    issued = issue_key(user_id=1, name="k")
    assert revoke_key(issued.id, user_id=1) is True
    assert lookup_by_hash(hash_key(issued.secret)) is None


# ---------------------------------------------------------------------------
# lockout
# ---------------------------------------------------------------------------


def test_lockout_three_strikes(temp_db):
    from backend.services.lockout import check_allowed, record_failure, record_success

    identifier = "alice-lockout"
    # 1st and 2nd attempt should remain allowed
    assert record_failure(identifier, max_failures=3, window_seconds=300).allowed is True
    # 3rd attempt (== max_failures) is still allowed (we only block on the next one)
    assert record_failure(identifier, max_failures=3, window_seconds=300).allowed is True
    # 4th attempt must be blocked
    decision = record_failure(identifier, max_failures=3, window_seconds=300)
    assert decision.allowed is False
    # After a success the counter is reset
    record_success(identifier)
    assert check_allowed(identifier, max_failures=3, window_seconds=300).allowed is True


# ---------------------------------------------------------------------------
# pricing helpers
# ---------------------------------------------------------------------------


def test_list_effective_pricing_prefers_custom(temp_db):
    """Admin custom row should win over the official default.

    To avoid the ``UNIQUE(provider, model_id, tier)`` constraint, the
    custom row uses a different tier (``premium``) — the custom flag
    is what makes it win in the resolver, not the tier label.
    """
    from backend.database import get_db, list_effective_pricing

    # Sanity check: the temp file is unique to this test.
    conn = sqlite3.connect(temp_db)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM model_pricing")
    assert cur.fetchone()[0] == 0, "temp DB should be empty"
    conn.close()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO model_pricing (provider, model_id, input_price_per_1k,
                                   output_price_per_1k, tier, is_active, is_custom)
        VALUES (?, ?, ?, ?, ?, 1, 0)
    """,
        ("openai", "gpt-4o", 1.0, 2.0, "standard"),
    )
    cur.execute(
        """
        INSERT INTO model_pricing (provider, model_id, input_price_per_1k,
                                   output_price_per_1k, tier, is_active, is_custom)
        VALUES (?, ?, ?, ?, ?, 1, 1)
    """,
        ("openai", "gpt-4o", 5.0, 10.0, "premium"),
    )
    conn.commit()
    conn.close()
    rows = list_effective_pricing()
    assert len(rows) == 1
    assert float(rows[0]["input_price_per_1k"]) == 5.0
    assert rows[0]["is_custom"] == 1


def test_get_pricing_for_model_list_bulk(temp_db):
    from backend.database import get_db, get_pricing_for_model_list

    conn = get_db()
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO model_pricing (provider, model_id, input_price_per_1k,
                                   output_price_per_1k, tier, is_active, is_custom)
        VALUES (?, ?, ?, ?, ?, 1, 0)
    """,
        [
            ("openai", "gpt-4o", 1.0, 2.0, "standard"),
            ("anthropic", "claude-3-5-sonnet-20241022", 3.0, 15.0, "standard"),
        ],
    )
    conn.commit()
    conn.close()

    out = get_pricing_for_model_list(
        ["openai/gpt-4o", "anthropic/claude-3-5-sonnet-20241022", "openai/unknown-model"]
    )
    assert "openai/gpt-4o" in out
    assert "anthropic/claude-3-5-sonnet-20241022" in out
    assert "openai/unknown-model" not in out
    assert out["openai/gpt-4o"]["is_custom"] is False
    assert out["openai/gpt-4o"]["input_price_per_1k"] == 1.0


# ---------------------------------------------------------------------------
# openai_compat model shape with pricing
# ---------------------------------------------------------------------------


def test_model_to_openai_includes_pricing_when_provided():
    """The OpenAI model envelope should expose a `pricing` block when known."""
    from backend.routes.openai_compat import _model_to_openai

    out = _model_to_openai(
        "openai",
        "gpt-4o",
        1,
        pricing={
            "input_price_per_1k": 1.75,
            "output_price_per_1k": 7.0,
            "tier": "standard",
            "is_custom": False,
        },
    )
    assert out["id"] == "gpt-4o"
    assert out["owned_by"] == "openai"
    assert out["pricing"]["input_per_1k"] == 1.75
    assert out["pricing"]["output_per_1k"] == 7.0
    assert out["pricing"]["currency"] == "credits"
    assert out["pricing"]["is_custom"] is False


def test_model_to_openai_omits_pricing_when_none():
    from backend.routes.openai_compat import _model_to_openai

    out = _model_to_openai("openai", "gpt-4o", 1)
    assert "pricing" not in out
