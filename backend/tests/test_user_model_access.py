"""Tests for the M9 user-level model access control.

Covers:
  * No user_model_access rows → request allowed (backward-compat default)
  * Exact-model deny row → request blocked
  * Provider-wide deny row (``provider/*``) → request blocked
  * Allow rows are ignored (default is already allow-all)
  * DB read failure → fail open (request allowed)
  * check_key_restrictions integrates the user-level check
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from backend.services.auth_service import check_key_restrictions, check_user_model_access


def _seed_user_and_key(conn):
    """Insert a user + an API key row and return (user_id, key_info_dict)."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (username, email, api_key, is_active)
        VALUES ('access_test_user', 'amt@example.com', 'sk_test_access', 1)
        """,
    )
    user_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO api_keys
            (user_id, name, key_hash, key_prefix, is_active)
        VALUES (?, 'test-key', 'hash_dummy', 'sk_test', 1)
        """,
        (user_id,),
    )
    api_key_id = cur.lastrowid
    conn.commit()

    key_info = {
        "id": api_key_id,
        "user_id": user_id,
        "name": "test-key",
        "key_prefix": "sk_test",
        "key_mask": "sk_***",
        "is_active": True,
        "expires_at": None,
        "allowed_models": [],  # empty = no key-level restriction
        "denied_models": [],
        "allowed_ips": [],
        "monthly_token_limit": None,
        "monthly_credit_limit": None,
        "last_used_at": None,
        "created_at": "2026-01-01 00:00:00",
        "source": "api_keys",
    }
    return user_id, key_info


def _insert_access_row(conn, user_id, model_id, access_type="deny"):
    conn.execute(
        """
        INSERT INTO user_model_access (user_id, model_id, access_type, granted_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (user_id, model_id, access_type),
    )
    conn.commit()


def test_no_access_rows_allows_request(temp_db):
    """No user_model_access rows → no user-level restriction."""
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)

    assert check_user_model_access(user_id, "openai/gpt-4o") is None


def test_exact_model_deny_blocks_request(temp_db):
    """A deny row with the exact model_id blocks the matching model."""
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)
    _insert_access_row(conn, user_id, "openai/gpt-4o", "deny")

    reason = check_user_model_access(user_id, "openai/gpt-4o")
    assert reason is not None
    assert "已被管理员禁用" in reason


def test_exact_model_deny_does_not_block_other_models(temp_db):
    """A deny row for one model doesn't affect a different model."""
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)
    _insert_access_row(conn, user_id, "openai/gpt-4o", "deny")

    # Different model in the same provider — should still be allowed.
    assert check_user_model_access(user_id, "openai/gpt-4o-mini") is None
    # Different provider entirely — should be allowed.
    assert check_user_model_access(user_id, "anthropic/claude-3-opus") is None


def test_provider_wildcard_deny_blocks_all_models_in_provider(temp_db):
    """A deny row with ``provider/*`` blocks any model under that provider."""
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)
    _insert_access_row(conn, user_id, "openai/*", "deny")

    # All openai/ models should be blocked.
    assert check_user_model_access(user_id, "openai/gpt-4o") is not None
    assert check_user_model_access(user_id, "openai/gpt-4o-mini") is not None
    assert check_user_model_access(user_id, "openai/dall-e-3") is not None
    # Other providers should be unaffected.
    assert check_user_model_access(user_id, "anthropic/claude-3-opus") is None


def test_allow_rows_ignored_at_runtime(temp_db):
    """Allow rows are informational only — they don't change the default
    allow-all behaviour and don't override a deny.
    """
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)
    # Insert an allow row for one model and a deny row for another.
    _insert_access_row(conn, user_id, "openai/gpt-4o", "allow")
    _insert_access_row(conn, user_id, "anthropic/claude-3-opus", "deny")

    # Allow row has no effect — model is allowed by default anyway.
    assert check_user_model_access(user_id, "openai/gpt-4o") is None
    # Deny row blocks the other model.
    assert check_user_model_access(user_id, "anthropic/claude-3-opus") is not None


def test_db_read_failure_fails_open(temp_db, monkeypatch):
    """If the SELECT raises, check_user_model_access returns None (allow).

    This mirrors the existing default-allow behaviour — a transient DB
    error shouldn't break a previously-working request.
    """
    conn = temp_db
    user_id, _ = _seed_user_and_key(conn)

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("simulated DB failure")

    # Patch get_db_context inside auth_service to raise.
    import backend.services.auth_service as auth_service

    monkeypatch.setattr(auth_service, "get_db_context", _boom)

    # Must return None (fail open), not raise.
    assert check_user_model_access(user_id, "openai/gpt-4o") is None


def test_check_key_restrictions_integrates_user_level_check(temp_db):
    """check_key_restrictions should call check_user_model_access and
    propagate its rejection.
    """
    conn = temp_db
    user_id, key_info = _seed_user_and_key(conn)
    _insert_access_row(conn, user_id, "openai/gpt-4o", "deny")

    # Key-level restrictions are empty (allowed/denied both []).
    # The user-level deny row should still block.
    reason = check_key_restrictions(key_info, "openai/gpt-4o")
    assert reason is not None
    assert "已被管理员禁用" in reason


def test_check_key_restrictions_allows_when_no_deny(temp_db):
    """When the user has no deny rows, check_key_restrictions passes."""
    conn = temp_db
    _user_id, key_info = _seed_user_and_key(conn)

    reason = check_key_restrictions(key_info, "openai/gpt-4o")
    assert reason is None


# ---------------------------------------------------------------------------
# Fixture: a temp SQLite DB with the minimal schema.
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "model_access.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)

    try:
        from backend.utils import db_pool
        if db_pool._POOL is not None:
            db_pool._POOL.close_all()
            db_pool._POOL = None
    except Exception:
        pass

    from backend.database import get_db_context
    from backend.tests.conftest import _SCHEMA  # type: ignore[attr-defined]

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    with get_db_context() as conn:
        yield conn
