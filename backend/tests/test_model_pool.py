"""Tests for the user-defined model pool feature.

Covers:

* Pool CRUD (create / list / update / delete / reorder) with the
  encryption-at-rest invariant.
* ``sk-ump_`` key generation, resolution, and listing.
* Dispatch selection: priority order, cooldown skip, max_tokens skip,
  and the "all exhausted → random active" fallback.
* ``record_usage`` token increment + cooldown arm/clear.
* SSRF guard on ``api_base``.
* End-to-end ``sk-ump_`` auth flow through the OpenAI-compatible
  gateway (dispatch + failover on 429).

Tests run against a temp SQLite DB (see ``conftest.py``); we do not
import ``backend.main`` so the suite stays fast.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from backend.services.model_pool_service import ModelPoolService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_user(path: str, *, user_id: int = 1, username: str = "alice") -> int:
    """Insert a bare user row (no api_key / wallet needed for pool tests)."""
    conn = _connect(path)
    conn.execute(
        """
        INSERT OR REPLACE INTO users
            (id, username, email, password_hash, api_key, quota_5h,
             quota_week, quota_month, monthly_budget, plan_id, is_active)
        VALUES (?, ?, ?, ?, ?, 1000, 10000, 1000000, 0, NULL, 1)
        """,
        (user_id, username, f"{username}@example.com", "x", ""),
    )
    conn.commit()
    conn.close()
    return user_id


def _mock_public_dns(hostname: str):
    """Patch ``socket.getaddrinfo`` so *hostname* resolves to a public IP.

    Avoids real DNS lookups in tests.  Returns the patcher; caller calls
    ``.start()`` / ``.stop()`` (or use ``with``).
    """

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        # Always resolve to a public, non-RFC1918 address.
        return [
            (
                __import__("socket").AF_INET,
                __import__("socket").SOCK_STREAM,
                __import__("socket").IPPROTO_TCP,
                "",
                ("203.0.113.1", port or 443),
            )
        ]

    return patch("backend.services.model_pool_service.socket.getaddrinfo", _fake_getaddrinfo)


@pytest.fixture
def db_with_user(temp_db) -> Tuple[str, int]:
    """Yield (db_path, user_id) with a single seeded user."""
    _seed_user(temp_db, user_id=1, username="alice")
    return temp_db, 1


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pool CRUD
# ---------------------------------------------------------------------------


def test_create_pool_persists_and_returns_id(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name="openai-primary",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-upstream-secret",
            model_name="gpt-4o",
            priority=0,
            max_tokens=10000,
        )
    assert isinstance(pool_id, int) and pool_id > 0


def test_create_pool_rejects_missing_name(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        with pytest.raises(ValueError, match="名称不能为空"):
            ModelPoolService.create_pool(
                user_id=user_id,
                name="",
                provider_type="openai",
                api_base="https://api.openai.com/v1",
                api_key="sk-x",
                model_name="gpt-4o",
            )


def test_create_pool_rejects_localhost(db_with_user):
    """SSRF guard: localhost must be rejected even though it resolves."""
    db, user_id = db_with_user
    with pytest.raises(ValueError, match="不允许使用内部地址"):
        ModelPoolService.create_pool(
            user_id=user_id,
            name="local",
            provider_type="openai",
            api_base="http://localhost:8080",
            api_key="sk-x",
            model_name="gpt-4o",
        )


def test_create_pool_rejects_private_ip(db_with_user):
    """SSRF guard: 10.x must be rejected after DNS resolution."""
    db, user_id = db_with_user

    def _fake_private(host, port, *args, **kwargs):
        return [(__import__("socket").AF_INET, __import__("socket").SOCK_STREAM, __import__("socket").IPPROTO_TCP, "", ("10.0.0.1", port or 443))]

    with patch("backend.services.model_pool_service.socket.getaddrinfo", _fake_private):
        with pytest.raises(ValueError, match="不允许使用内部/私有地址"):
            ModelPoolService.create_pool(
                user_id=user_id,
                name="private",
                provider_type="openai",
                api_base="https://internal.example.com",
                api_key="sk-x",
                model_name="gpt-4o",
            )


def test_list_pools_returns_decrypted_for_owner(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        ModelPoolService.create_pool(
            user_id=user_id,
            name="p1",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-secret-1",
            model_name="gpt-4o",
            priority=0,
        )
        ModelPoolService.create_pool(
            user_id=user_id,
            name="p2",
            provider_type="anthropic",
            api_base="https://api.anthropic.com",
            api_key="sk-secret-2",
            model_name="claude-3-5-sonnet",
            priority=1,
        )
    pools = ModelPoolService.list_pools(user_id)
    assert len(pools) == 2
    # Owner sees decrypted values.
    assert pools[0]["api_key"] == "sk-secret-1"
    assert pools[0]["api_base"] == "https://api.openai.com/v1"
    assert pools[1]["api_key"] == "sk-secret-2"
    # Priority ordering: lower first.
    assert pools[0]["name"] == "p1"
    assert pools[1]["name"] == "p2"


def test_list_pools_returns_empty_for_user_without_pools(db_with_user):
    _db, user_id = db_with_user
    assert ModelPoolService.list_pools(user_id) == []


def test_get_pool_returns_none_for_other_user(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name="p1",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
            model_name="gpt-4o",
        )
    # Other user cannot see it.
    assert ModelPoolService.get_pool(pool_id, user_id=999) is None
    # Owner can.
    own = ModelPoolService.get_pool(pool_id, user_id=user_id)
    assert own is not None
    assert own["name"] == "p1"


def test_update_pool_re_encrypts_api_key(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name="p1",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-old",
            model_name="gpt-4o",
        )
        ok = ModelPoolService.update_pool(
            pool_id, user_id, api_key="sk-new", name="renamed"
        )
    assert ok is True
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["api_key"] == "sk-new"
    assert pool["name"] == "renamed"


def test_update_pool_returns_false_when_no_fields(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name="p1",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
            model_name="gpt-4o",
        )
    # No allowed fields → False.
    assert ModelPoolService.update_pool(pool_id, user_id, unknown_field="x") is False
    # All None values → False.
    assert ModelPoolService.update_pool(pool_id, user_id, name=None) is False


def test_delete_pool_respects_ownership(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name="p1",
            provider_type="openai",
            api_base="https://api.openai.com/v1",
            api_key="sk-x",
            model_name="gpt-4o",
        )
    # Other user cannot delete.
    assert ModelPoolService.delete_pool(pool_id, user_id=999) is False
    # Owner can.
    assert ModelPoolService.delete_pool(pool_id, user_id) is True
    assert ModelPoolService.get_pool(pool_id, user_id) is None


def test_reorder_pools_reassigns_priority(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1,
        )
        id_c = ModelPoolService.create_pool(
            user_id=user_id, name="c", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-c", model_name="gpt-4o",
            priority=2,
        )
    # Reverse the order.
    updated = ModelPoolService.reorder_pools(user_id, [id_c, id_b, id_a])
    assert updated == 3
    pools = ModelPoolService.list_pools(user_id)
    assert [p["id"] for p in pools] == [id_c, id_b, id_a]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_generate_key_returns_sk_ump_prefix(db_with_user):
    db, user_id = db_with_user
    key = ModelPoolService.generate_key(user_id, name="test-key")
    assert key.startswith("sk-ump_")
    assert len(key) > len("sk-ump_")
    # Stored keys list should show the prefix but not the full key.
    keys = ModelPoolService.list_keys(user_id)
    assert len(keys) == 1
    assert keys[0]["key_prefix"].startswith("sk-ump_")
    assert "key_hash" not in keys[0]  # full hash is intentionally omitted


def test_resolve_key_returns_user_id(db_with_user):
    db, user_id = db_with_user
    key = ModelPoolService.generate_key(user_id, name="test-key")
    assert ModelPoolService.resolve_key(key) == user_id


def test_resolve_key_returns_none_for_unknown(db_with_user):
    _db, user_id = db_with_user
    assert ModelPoolService.resolve_key("sk-ump_nonexistent_token_value_xyz") is None


def test_resolve_key_returns_none_for_wrong_prefix(db_with_user):
    _db, user_id = db_with_user
    assert ModelPoolService.resolve_key("sk-not-ump-key") is None
    assert ModelPoolService.resolve_key("") is None


def test_resolve_key_returns_none_for_inactive(db_with_user):
    db, user_id = db_with_user
    key = ModelPoolService.generate_key(user_id, name="test-key")
    # Manually deactivate the key.
    conn = _connect(db)
    conn.execute("UPDATE user_model_pool_keys SET is_active = 0")
    conn.commit()
    conn.close()
    assert ModelPoolService.resolve_key(key) is None


def test_delete_key_respects_ownership(db_with_user):
    db, user_id = db_with_user
    ModelPoolService.generate_key(user_id, name="k1")
    keys = ModelPoolService.list_keys(user_id)
    key_id = keys[0]["id"]
    # Other user cannot delete.
    assert ModelPoolService.delete_key(key_id, user_id=999) is False
    # Owner can.
    assert ModelPoolService.delete_key(key_id, user_id) is True
    assert ModelPoolService.list_keys(user_id) == []


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_get_next_model_picks_lowest_priority(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=5,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1,
        )
    pool = ModelPoolService.get_next_model_for_user(user_id)
    assert pool is not None
    assert pool["id"] == id_b  # priority=1 wins over priority=5


def test_get_next_model_returns_none_when_no_pools(db_with_user):
    _db, user_id = db_with_user
    assert ModelPoolService.get_next_model_for_user(user_id) is None


def test_get_next_model_skips_cooldown(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1,
        )
    # Arm cooldown on pool A (the higher-priority one).
    ModelPoolService.record_usage(id_a, 0, success=False, error_msg="429")
    pool = ModelPoolService.get_next_model_for_user(user_id)
    assert pool is not None
    assert pool["id"] == id_b  # A is cooling down, B is picked


def test_get_next_model_skips_when_max_tokens_exhausted(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0, max_tokens=100,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1,
        )
    # Burn through A's quota.
    ModelPoolService.record_usage(id_a, 150, success=True)
    pool = ModelPoolService.get_next_model_for_user(user_id)
    assert pool is not None
    assert pool["id"] == id_b  # A exhausted, B is picked


def test_get_next_model_falls_back_to_random_when_all_exhausted(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0, max_tokens=100,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1, max_tokens=100,
        )
    # Burn through both quotas.
    ModelPoolService.record_usage(id_a, 150, success=True)
    ModelPoolService.record_usage(id_b, 150, success=True)
    pool = ModelPoolService.get_next_model_for_user(user_id)
    # Fallback should still return one of the active pools (not None).
    assert pool is not None
    assert pool["id"] in (id_a, id_b)


def test_select_fallback_excludes_pool(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        id_a = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
        id_b = ModelPoolService.create_pool(
            user_id=user_id, name="b", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-b", model_name="gpt-4o",
            priority=1,
        )
    # Exclude A — should get B.
    pool = ModelPoolService.select_fallback_model(user_id, exclude_pool_id=id_a)
    assert pool is not None
    assert pool["id"] == id_b
    # Exclude B — should get A.
    pool = ModelPoolService.select_fallback_model(user_id, exclude_pool_id=id_b)
    assert pool is not None
    assert pool["id"] == id_a


def test_select_fallback_returns_none_when_no_pools(db_with_user):
    _db, user_id = db_with_user
    assert ModelPoolService.select_fallback_model(user_id) is None


# ---------------------------------------------------------------------------
# record_usage
# ---------------------------------------------------------------------------


def test_record_usage_increments_tokens(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0, max_tokens=10000,
        )
    ModelPoolService.record_usage(pool_id, 500, success=True)
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["used_tokens"] == 500


def test_record_usage_failure_arms_cooldown(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
    ModelPoolService.record_usage(pool_id, 0, success=False, error_msg="HTTP 429")
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["cooldown_until"] is not None
    assert "429" in (pool["last_error"] or "")


def test_record_usage_success_clears_cooldown(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
    # Arm cooldown first.
    ModelPoolService.record_usage(pool_id, 0, success=False, error_msg="429")
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["cooldown_until"] is not None
    # Success should clear it.
    ModelPoolService.record_usage(pool_id, 100, success=True)
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["cooldown_until"] is None
    assert pool["last_error"] is None
    assert pool["used_tokens"] == 100


def test_reset_cooldown_clears_cooldown(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
    ModelPoolService.record_usage(pool_id, 0, success=False, error_msg="429")
    assert ModelPoolService.reset_cooldown(pool_id, user_id) is True
    pool = ModelPoolService.get_pool(pool_id, user_id)
    assert pool["cooldown_until"] is None
    assert pool["last_error"] is None


def test_reset_cooldown_respects_ownership(db_with_user):
    db, user_id = db_with_user
    with _mock_public_dns("api.openai.com"):
        pool_id = ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-a", model_name="gpt-4o",
            priority=0,
        )
    ModelPoolService.record_usage(pool_id, 0, success=False, error_msg="429")
    # Other user cannot reset.
    assert ModelPoolService.reset_cooldown(pool_id, user_id=999) is False


# ---------------------------------------------------------------------------
# Encryption-at-rest invariant
# ---------------------------------------------------------------------------


def test_api_base_and_key_are_encrypted_at_rest(db_with_user):
    """The cleartext must not appear in the DB even if the file leaks."""
    db, user_id = db_with_user
    secret_key = "sk-super-secret-upstream-key-12345"
    secret_url = "https://api.openai.com/v1/special"
    with _mock_public_dns("api.openai.com"):
        ModelPoolService.create_pool(
            user_id=user_id, name="a", provider_type="openai",
            api_base=secret_url, api_key=secret_key, model_name="gpt-4o",
            priority=0,
        )
    # Read the raw DB rows — cleartext must not appear.
    conn = _connect(db)
    cursor = conn.cursor()
    cursor.execute("SELECT api_base, api_key_encrypted FROM user_model_pools WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    assert row is not None
    raw_base = row["api_base"]
    raw_key = row["api_key_encrypted"]
    assert secret_key not in raw_key
    assert secret_url not in raw_base
    assert secret_key not in raw_base
    # But decryption recovers the cleartext.
    from backend.security import Security
    assert Security.decrypt(raw_key) == secret_key
    assert Security.decrypt(raw_base) == secret_url.rstrip("/")


# ---------------------------------------------------------------------------
# sk-ump_ auth flow through the OpenAI-compatible gateway
# ---------------------------------------------------------------------------


def _make_pool_app():
    """Build a minimal FastAPI app that mounts the openai_compat router."""
    from fastapi import FastAPI
    from backend.routes.openai_compat import router as openai_router

    app = FastAPI()
    app.include_router(openai_router)
    return app


@pytest.fixture
def pool_app_and_db(temp_db) -> Iterator[Tuple[object, str, int]]:
    """Yield (app, db_path, user_id) with a user + pool + sk-ump_ key."""
    _seed_user(temp_db, user_id=1, username="alice")
    with _mock_public_dns("api.openai.com"):
        ModelPoolService.create_pool(
            user_id=1, name="primary", provider_type="openai",
            api_base="https://api.openai.com/v1", api_key="sk-upstream-real",
            model_name="gpt-4o", priority=0,
        )
    display_key = ModelPoolService.generate_key(1, name="test-key")
    yield _make_pool_app(), temp_db, display_key


def test_chat_completions_sk_ump_unknown_key_returns_401(pool_app_and_db):
    app, _db, _key = pool_app_and_db

    async def _do():
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-ump_unknown_key_xyz"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )

    resp = _run(_do())
    assert resp.status_code == 401


def test_chat_completions_sk_ump_dispatches_to_pool(pool_app_and_db, monkeypatch):
    """A valid sk-ump_ key should bypass platform quota/billing and forward
    to the user's upstream, returning the upstream’s response verbatim."""
    app, _db, display_key = pool_app_and_db

    # Mock the upstream HTTP call so no real network traffic happens.
    captured = {}

    class _FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

        @property
        def text(self):
            return ""

    async def _fake_post(url, json=None, headers=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _FakeResponse({
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello from upstream"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }, 200)

    # Patch the httpx client used by _forward_pool_request.
    import backend.services.http_client as http_client_mod
    original_client = http_client_mod.get_async_client()

    class _FakeClient:
        async def post(self, url, **kwargs):
            return await _fake_post(url, **kwargs)

        def build_request(self, *args, **kwargs):
            return None

        async def send(self, *args, **kwargs):
            return _FakeResponse({}, 200)

        async def aclose(self):
            pass

    monkeypatch.setattr(http_client_mod, "get_async_client", lambda: _FakeClient())

    async def _do():
        import httpx
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {display_key}"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )

    resp = _run(_do())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "hello from upstream"
    # The pool's model_name should override the client's model field.
    assert captured["body"]["model"] == "gpt-4o"
    # Authorization header carries the upstream key, not the sk-ump_ key.
    assert captured["headers"]["Authorization"] == "Bearer sk-upstream-real"
