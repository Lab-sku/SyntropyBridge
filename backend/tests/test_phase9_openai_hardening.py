"""Regression tests for the hardening pass on the OpenAI-compatible
gateway (v9).

These tests cover the following issues we fixed:

  1. ``/v1/usage`` returned a *naive* UTC timestamp via the
     deprecated ``datetime.utcnow()``; now it uses
     ``datetime.now(timezone.utc)`` and the ``period`` key matches
     the SQLite-side calendar month.
  2. ``/v1/models`` and ``/v1/models/{id}`` used to skip the
     ``update_last_used`` bump, leaving stale ``last_used_at`` for
     users who only listed models.
  3. The streaming chat emitter used to send
     ``error → done → [DONE]`` when the wallet ran dry mid-stream.
     Most OpenAI SDKs treat the trailing ``[DONE]`` as a success
     marker and silently drop the error chunk. We now stop after
     the first error chunk so the wire format stays unambiguous.
  4. ``/v1/chat/completions`` (and the legacy ``/v1/completions``)
     used to forget to pass ``token_id`` through to
     ``ProxyService``, so per-key channel routing never kicked in
     for the OpenAI-compatible endpoint.

The tests don't try to make real HTTP calls to upstream
providers — the proxy service is monkey-patched with a fake
async stream that yields deterministic chunks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterator, List, Tuple

import httpx
import pytest

from backend.routes.openai_compat import router as openai_router

# ---------------------------------------------------------------------------
# Fixtures (mirrors the helpers in test_openai_compat.py)
# ---------------------------------------------------------------------------


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_user_with_key(
    path: str,
    *,
    api_key_value: str = "sk-phase9-alice",
    user_id: int = 1,
    username: str = "alice",
    balance: float = 100.0,
    is_active: int = 1,
) -> int:
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
    key_digest = hashlib.sha256(api_key_value.encode("utf-8")).hexdigest()
    cur = conn.execute(
        """
        INSERT INTO api_keys
            (user_id, name, key_hash, key_prefix, key_mask,
             is_active, allowed_models, denied_models)
        VALUES (?, ?, ?, ?, ?, 1, NULL, NULL)
        """,
        (
            user_id,
            "phase9",
            key_digest,
            api_key_value[:8],
            api_key_value[:4] + "..." + api_key_value[-4:],
        ),
    )
    api_key_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return api_key_id


def _make_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(openai_router)
    return app


@pytest.fixture
def app_and_db(temp_db) -> Iterator[Tuple[object, str]]:
    yield _make_app(), temp_db


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# /v1/usage: timezone-aware period
# ---------------------------------------------------------------------------


def test_usage_period_is_utc_and_current_month(app_and_db):
    """`period` must come from a tz-aware UTC clock and match the
    calendar month the SQLite-side summary is using."""
    app, db = app_and_db
    _create_user_with_key(db)

    async def _do():
        async with _client(app) as client:
            return await client.get(
                "/v1/usage",
                headers={"Authorization": "Bearer sk-phase9-alice"},
            )

    resp = _run(_do())
    assert resp.status_code == 200
    body = resp.json()
    assert "period" in body
    expected = datetime.now(timezone.utc).strftime("%Y-%m")
    assert body["period"] == expected
    # The format must be exactly YYYY-MM (4-2 with hyphen).
    assert len(body["period"]) == 7
    assert body["period"][4] == "-"


# ---------------------------------------------------------------------------
# /v1/models bumps last_used_at
# ---------------------------------------------------------------------------


def test_list_models_updates_last_used_at(app_and_db):
    """`/v1/models` must bump the api_key row's last_used_at so the
    admin dashboard reflects "last seen" accurately."""
    app, db = app_and_db
    api_key_id = _create_user_with_key(db)

    # Seed the model aggregator so the response is non-empty.
    conn = _connect(db)
    conn.execute(
        "INSERT INTO settings (key, value, is_encrypted) VALUES (?, ?, 0)",
        ("openai_api_key", "sk-test-upstream"),
    )
    conn.execute(
        """INSERT INTO models (model_id, display_name, provider, is_active,
                                 context_length, last_synced)
           VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)""",
        ("gpt-4o", "gpt-4o", "openai"),
    )
    conn.commit()
    conn.close()

    # Confirm baseline: last_used_at is null.
    pre = (
        _connect(db)
        .execute("SELECT last_used_at FROM api_keys WHERE id = ?", (api_key_id,))
        .fetchone()
    )
    assert pre["last_used_at"] is None

    async def _do():
        async with _client(app) as client:
            return await client.get(
                "/v1/models",
                headers={"Authorization": "Bearer sk-phase9-alice"},
            )

    resp = _run(_do())
    assert resp.status_code == 200

    post = (
        _connect(db)
        .execute("SELECT last_used_at FROM api_keys WHERE id = ?", (api_key_id,))
        .fetchone()
    )
    assert post["last_used_at"] is not None, "last_used_at should be set after /v1/models call"


def test_retrieve_model_updates_last_used_at(app_and_db):
    """`/v1/models/{model_id}` must also bump last_used_at."""
    app, db = app_and_db
    api_key_id = _create_user_with_key(db, api_key_value="sk-phase9-bob")

    conn = _connect(db)
    conn.execute(
        "INSERT INTO settings (key, value, is_encrypted) VALUES (?, ?, 0)",
        ("openai_api_key", "sk-test-upstream"),
    )
    conn.execute(
        """INSERT INTO models (model_id, display_name, provider, is_active,
                                 context_length, last_synced)
           VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)""",
        ("gpt-4o", "gpt-4o", "openai"),
    )
    conn.commit()
    conn.close()

    async def _do():
        async with _client(app) as client:
            return await client.get(
                "/v1/models/openai/gpt-4o",
                headers={"Authorization": "Bearer sk-phase9-bob"},
            )

    resp = _run(_do())
    assert resp.status_code == 200

    post = (
        _connect(db)
        .execute("SELECT last_used_at FROM api_keys WHERE id = ?", (api_key_id,))
        .fetchone()
    )
    assert post["last_used_at"] is not None


# ---------------------------------------------------------------------------
# Streaming: error → [DONE] is forbidden
# ---------------------------------------------------------------------------


def test_stream_error_does_not_emit_done_sentinel(app_and_db, monkeypatch):
    """When the upstream sends an `event: error` chunk we must NOT
    follow it up with `data: [DONE]` — that would mislead the SDK
    into treating the request as a clean success."""
    app, db = app_and_db
    _create_user_with_key(db, balance=1000.0)

    async def _fake_stream(user_id, payload, provider, *, token_id=None):
        yield b'event: delta\ndata: {"content": "partial"}\n\n'
        yield (b'event: error\ndata: {"error":"upstream blew up","code":502}\n\n')

    from backend.routes import openai_compat as oc

    monkeypatch.setattr(oc.ProxyService, "stream_chat", _fake_stream)

    async def _do():
        async with _client(app) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-phase9-alice"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                chunks: List[str] = []
                async for raw in resp.aiter_lines():
                    if raw:
                        chunks.append(raw)
        return chunks

    chunks = _run(_do())
    # The wire format must end with the error chunk, NOT [DONE].
    assert "data: [DONE]" not in chunks, chunks
    error_payload = [c for c in chunks if c.startswith("data:") and "error" in c]
    assert error_payload, chunks
    body = json.loads(error_payload[0][len("data:") :].strip())
    assert body["error"]["message"] == "upstream blew up"


def test_stream_token_id_is_forwarded_to_proxy(app_and_db, monkeypatch):
    """The `token_id` returned by the auth resolver must be threaded
    into the streaming call so channel routing can work for
    managed keys (api_keys.id, not the legacy users.api_key)."""
    app, db = app_and_db
    _create_user_with_key(db, balance=1000.0)

    captured: dict = {}

    async def _fake_stream(user_id, payload, provider, *, token_id=None):
        captured["token_id"] = token_id
        yield (
            b"event: done\ndata: "
            b'{"content":"x","model":"openai/gpt-4o",'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        )

    from backend.routes import openai_compat as oc

    monkeypatch.setattr(oc.ProxyService, "stream_chat", _fake_stream)

    async def _do():
        async with _client(app) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-phase9-alice"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as resp:
                async for _ in resp.aiter_lines():
                    pass
        return resp

    _run(_do())
    # The fake stream captured the token_id argument. The test key
    # we created has an api_keys row with an id; the auth resolver
    # surfaces that as ``info["id"]`` and the route passes it
    # through to ProxyService.stream_chat.
    assert captured.get("token_id") is not None
    assert int(captured["token_id"]) > 0


def test_non_stream_token_id_is_forwarded_to_proxy(app_and_db, monkeypatch):
    """Same thread-through guarantee for the non-streaming path."""
    app, db = app_and_db
    _create_user_with_key(db, balance=1000.0)

    captured: dict = {}

    async def _fake_forward(user_id, payload, provider, *, token_id=None):
        captured["token_id"] = token_id
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hi back"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }, 200

    from backend.routes import openai_compat as oc

    monkeypatch.setattr(oc.ProxyService, "forward_request", _fake_forward)

    async def _do():
        async with _client(app) as client:
            return await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer sk-phase9-alice"},
                json={
                    "model": "openai/gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    resp = _run(_do())
    assert resp.status_code == 200
    assert captured.get("token_id") is not None
    assert int(captured["token_id"]) > 0
