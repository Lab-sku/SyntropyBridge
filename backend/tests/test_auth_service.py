"""Tests for the API-key authentication service.

These tests focus on ``resolve_api_key`` because it is the single
chokepoint every OpenAI-compatible request flows through. A bug here
blackholes the entire platform, so the regression net has to be
relatively tight.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional

from backend.services.auth_service import (
    _hash_secret,
    check_key_restrictions,
    resolve_api_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_key(
    path: str,
    *,
    raw_key: str,
    user_id: int = 1,
    is_active: int = 1,
    allowed_models=None,
    denied_models=None,
    monthly_token_limit: Optional[int] = None,
    monthly_credit_limit: Optional[float] = None,
    expires_at: Optional[str] = None,
    raw_hash: bool = False,
) -> int:
    """Insert a managed api_keys row.

    ``raw_hash=True`` stores the secret verbatim in ``key_hash``; that
    is *not* how production works, but lets us reproduce legacy /
    mis-imported data to verify the lookup logic stays correct.
    """
    digest = raw_key if raw_hash else hashlib.sha256(raw_key.encode()).hexdigest()
    conn = _connect(path)
    # NOTE: legacy `users.api_key` is set to a *different* value so the
    # legacy-fallback path does not shadow the managed-keys lookup. In
    # production the two columns are alternate storages, not mirrors.
    conn.execute(
        "INSERT OR REPLACE INTO users (id, username, email, password_hash, api_key, is_active)"
        " VALUES (?, 'u', 'u@example.com', 'x', 'sk-legacy-shim', 1)",
        (user_id,),
    )
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (user_id, name, key_hash, key_prefix, key_mask,
             monthly_token_limit, monthly_credit_limit,
             allowed_models, denied_models, is_active, expires_at)
        VALUES (?, 'k', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            digest,
            raw_key[:8],
            raw_key[:4] + "..." + raw_key[-4:],
            monthly_token_limit,
            monthly_credit_limit,
            __import__("json").dumps(allowed_models) if allowed_models is not None else None,
            __import__("json").dumps(denied_models) if denied_models is not None else None,
            is_active,
            expires_at,
        ),
    )
    key_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return key_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveApiKey:
    """The original implementation compared the raw secret to the
    ``key_hash`` digest column, which can never match. These tests pin
    the correct behaviour: digest the inbound secret *first*, then look
    it up. They also exercise the prefix-collision defence.
    """

    def test_hashes_inbound_secret(self, temp_db):
        _seed_key(temp_db, raw_key="sk-prod-alice")
        info = resolve_api_key("sk-prod-alice")
        assert info is not None
        assert info["source"] == "api_keys"
        assert info["key_prefix"] == "sk-prod-"

    def test_handles_bearer_prefix(self, temp_db):
        _seed_key(temp_db, raw_key="sk-prod-alice")
        info = resolve_api_key("Bearer sk-prod-alice")
        assert info is not None
        assert info["user_id"] == 1

    def test_unknown_key_returns_none(self, temp_db):
        _seed_key(temp_db, raw_key="sk-prod-alice")
        assert resolve_api_key("sk-doesnotexist") is None

    def test_too_short_returns_none(self, temp_db):
        assert resolve_api_key("") is None
        assert resolve_api_key("sk-") is None

    def test_inactive_key_returns_none(self, temp_db):
        _seed_key(temp_db, raw_key="sk-disabled", is_active=0)
        assert resolve_api_key("sk-disabled") is None

    def test_prefix_collision_with_wrong_hash_rejected(self, temp_db):
        """Two keys sharing the same 8-char prefix must NOT be
        cross-resolved. The digest comparison at the prefix-match
        branch is what guarantees that.
        """
        # Same prefix "sk-coll-" (8 chars)
        _seed_key(temp_db, raw_key="sk-coll-AAAA-1111", user_id=1)
        _seed_key(temp_db, raw_key="sk-coll-BBBB-2222", user_id=2)
        info = resolve_api_key("sk-coll-AAAA-1111")
        assert info is not None
        assert info["user_id"] == 1
        info2 = resolve_api_key("sk-coll-BBBB-2222")
        assert info2 is not None
        assert info2["user_id"] == 2

    def test_legacy_users_api_key_fallback(self, temp_db):
        """A key that lives on the legacy ``users.api_key_hash`` column is
        still resolvable (compatibility shim for old accounts).

        P1.4 removed the plaintext ``users.api_key = ?`` fallback, so
        the legacy path now requires the sha256 digest to be stored in
        ``api_key_hash`` (which Migration 25 already does for every
        pre-existing plaintext key).
        """
        legacy_key = "sk-legacy-key"
        legacy_digest = hashlib.sha256(legacy_key.encode()).hexdigest()
        conn = _connect(temp_db)
        conn.execute(
            "INSERT OR REPLACE INTO users (id, username, email, password_hash, api_key, api_key_hash, is_active)"
            " VALUES (42, 'legacy', 'l@e.com', 'x', ?, ?, 1)",
            (legacy_key, legacy_digest),
        )
        conn.commit()
        conn.close()
        info = resolve_api_key(legacy_key)
        assert info is not None
        assert info["source"] == "users.api_key"
        assert info["user_id"] == 42

    def test_hash_helper_matches_storage(self):
        """The internal helper must match ``api_key_service.hash_key``."""
        from backend.services.api_key_service import hash_key

        for secret in ["sk-abc", "sk-1234567890-abcdef", "Bearer sk-xyz"]:
            assert _hash_secret(secret) == hash_key(secret)


class TestCheckKeyRestrictions:
    """The enforcement layer applied to every chat-completion call."""

    def test_none_key_info_rejected(self):
        assert check_key_restrictions({}, "gpt-4o") == "无效的 API Key"
        assert check_key_restrictions(None, "gpt-4o") == "无效的 API Key"

    def test_inactive_key_blocked(self):
        info = {"is_active": False, "user_id": 1}
        assert check_key_restrictions(info, "gpt-4o") == "API Key 已被禁用"

    def test_expired_key_blocked(self):
        info = {"is_active": True, "user_id": 1, "expires_at": "2000-01-01T00:00:00"}
        assert check_key_restrictions(info, "gpt-4o") == "API Key 已过期"

    def test_model_not_in_allow_list_blocked(self):
        info = {"is_active": True, "user_id": 1, "allowed_models": ["gpt-4o"]}
        assert "不在允许列表中" in check_key_restrictions(info, "gpt-3.5")

    def test_model_in_deny_list_blocked(self):
        info = {"is_active": True, "user_id": 1, "denied_models": ["gpt-3.5"]}
        assert "已被禁用" in check_key_restrictions(info, "gpt-3.5")

    def test_clean_key_allowed(self):
        info = {"is_active": True, "user_id": 1, "allowed_models": [], "denied_models": []}
        assert check_key_restrictions(info, "gpt-4o") is None
