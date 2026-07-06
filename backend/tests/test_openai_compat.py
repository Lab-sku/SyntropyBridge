"""Tests for the OpenAI-compatible gateway.

These tests run against a temporary SQLite file (see ``conftest.py``)
and a minimal FastAPI app that only mounts the new ``openai_compat``
router. We avoid importing ``backend.main`` to keep the suite fast and
to prevent touching the live database / running services.

We do not rely on ``pytest-asyncio``; each async test is wrapped in a
synchronous function that runs the coroutine with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from typing import Iterator, List, Optional, Tuple

import httpx
import pytest

from backend.routes.openai_compat import router as openai_router

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_user_with_key(
    path: str,
    *,
    api_key_value: str = "sk-test-alice",
    user_id: int = 1,
    username: str = "alice",
    balance: float = 100.0,
    is_active: int = 1,
    monthly_token_limit: Optional[int] = None,
    monthly_credit_limit: Optional[float] = None,
    allowed_models: Optional[List[str]] = None,
    denied_models: Optional[List[str]] = None,
    expires_at: Optional[str] = None,
) -> int:
    """Insert a user, a managed api_keys row, and a wallet.

    Returns the api_keys row id.
    """
    conn = _connect(path)
    conn.execute(
        """
        INSERT OR REPLACE INTO users
            (id, username, email, password_hash, api_key, quota_5h,
             quota_week, quota_month, monthly_budget, plan_id, is_active)
        VALUES (?, ?, ?, ?, ?, 1000, 10000, 1000000, 0, NULL, ?)
        """,
        (user_id, username, f"{username}@example.com", "x", api_key_value, is_active),
    )
    conn.execute(
        "INSERT OR REPLACE INTO wallets (user_id, balance, total_recharged, total_consumed) VALUES (?, ?, 0, 0)",
        (user_id, balance),
    )
    # `key_hash` must store SHA-256(secret) to mirror production storage.
    # The auth service now compares against digests, not raw secrets.
    key_digest = hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (user_id, name, key_hash, key_prefix, key_mask,
             monthly_token_limit, monthly_credit_limit,
             allowed_models, denied_models, is_active, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            "test-key",
            key_digest,
            api_key_value[:8],
            api_key_value[:4] + "..." + api_key_value[-4:],
            monthly_token_limit,
            monthly_credit_limit,
            json.dumps(allowed_models) if allowed_models is not None else None,
            json.dumps(denied_models) if denied_models is not None else None,
            1,
            expires_at,
        ),
    )
    api_key_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return api_key_id


def _make_app():
    """Build a minimal FastAPI app that mounts only our router."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(openai_router)
    return app


@pytest.fixture
def app_and_db(temp_db) -> Iterator[Tuple[object, str]]:
    """Yield (app, db_path) with the temp database ready for use."""
    yield _make_app(), temp_db


def _client(app) -> httpx.AsyncClient:
    """Return a configured AsyncClient targeting the in-process ASGI app."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _run(coro):
    """Synchronously run an awaitable inside a fresh event loop."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_models_requires_auth(app_and_db):
    """No auth header → 401 with OpenAI error envelope."""
    app, _db = app_and_db

    async def _do():
        async with _client(app) as client:
            return await client.get("/v1/models")

    resp = _run(_do())
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert "message" in body["error"]


def test_list_models_returns_openai_shape(app_and_db):
    """A valid key returns the OpenAI `{object: list, data: [...]}` envelope."""
    app, db = app_and_db
    _create_user_with_key(db, api_key_value="sk-models-alice")

    # Seed the settings so the `openai` provider looks configured to the
    # model aggregator (otherwise it short-circuits and returns nothing).
    conn = _connect(db)
    conn.execute(
        "INSERT INTO settings (key, value, is_encrypted) VALUES (?, ?, 0)",
        ("openai_api_key", "sk-test-openai-upstream"),
    )
    # Seed a model into the cache.
    conn.execute(
        """
        INSERT INTO models (model_id, display_name, provider, is_active,
                            context_length, last_synced)
        VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
        """,
        ("gpt-4o", "gpt-4o", "openai"),
    )
    conn.commit()
    conn.close()

    async def _do():
        async with _client(app) as client:
            return await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer sk-models-alice"},
            )

    resp = _run(_do())
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert body["data"], "expected at least one model after seeding the cache"
    for item in body["data"]:
        assert set(item.keys()) >= {"id", "object", "created", "owned_by"}
        assert item["object"] == "model"
        assert isinstance(item["id"], str)
        assert isinstance(item["owned_by"], str)


def test_chat_completions_non_stream_unauthenticated(app_and_db):
    """Missing API key on a chat request → 401 with OpenAI error envelope."""
    app, _db = app_and_db

    async def _do():
        async with _client(app) as client:
            return await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )

    resp = _run(_do())
    assert resp.status_code == 401
    body = resp.json()
    assert "error" in body
    assert "message" in body["error"]


def test_chat_completions_insufficient_balance_returns_402(app_and_db, monkeypatch):
    """A user with a zero balance should get HTTP 402 (Payment Required).

    We monkey-patch ``ProxyService.forward_request`` to avoid any real
    upstream call while still allowing the cost / wallet path to run.
    """
    app, db = app_and_db
    _create_user_with_key(db, api_key_value="sk-poor-alice", balance=0.0)

    async def _fake_forward(user_id, payload, provider):
        return {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }, 200

    from backend.routes import openai_compat as oc

    monkeypatch.setattr(oc.ProxyService, "forward_request", _fake_forward)

    async def _do():
        async with _client(app) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-poor-alice"},
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )

    resp = _run(_do())
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["error"]["code"] == "insufficient_quota"
    assert "message" in body["error"]


def test_chat_completions_streaming_returns_sse(app_and_db, monkeypatch):
    """stream=True must return a `text/event-stream` body with at least
    one OpenAI-formatted chunk and a `[DONE]` terminator."""
    app, db = app_and_db
    _create_user_with_key(db, api_key_value="sk-stream-alice", balance=1000.0)

    async def _fake_stream(user_id, payload, provider, *, token_id=None):
        # Yield an OpenAI-style internal delta, then a done event. We
        # use ASCII content to avoid the Python 3 bytes-literal limit.
        yield b'event: delta\ndata: {"content": "hello"}\n\n'
        yield (
            b"event: done\ndata: "
            b'{"content":"hello","model":"gpt-4o",'
            b'"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
        )

    from backend.routes import openai_compat as oc

    monkeypatch.setattr(oc.ProxyService, "stream_chat", _fake_stream)

    async def _do():
        async with _client(app) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-stream-alice"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                chunks: List[str] = []
                async for raw in resp.aiter_lines():
                    if raw:
                        chunks.append(raw)
        return chunks

    chunks = _run(_do())
    # Expect: at least one `data: {...}` line and a `data: [DONE]`.
    data_lines = [c for c in chunks if c.startswith("data:")]
    assert data_lines, chunks
    assert "data: [DONE]" in data_lines, chunks
    first_payload = json.loads(data_lines[0][len("data:") :].strip())
    assert first_payload["object"] == "chat.completion.chunk"
    assert first_payload["model"] == "gpt-4o"
    assert first_payload["choices"][0]["delta"]["content"] == "hello"
