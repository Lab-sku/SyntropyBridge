"""Hardening pass v9 — tests for the ``/v1/conversations/{id}``
pagination guard and the new "missing vs invalid API key" 401
distinction in ``get_current_auth``.

We mount the *real* FastAPI app (via ``backend.main``) on a temp
SQLite file so the proxy and auth resolvers run in their natural
state — no monkey-patching of the auth service.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import unittest
from typing import List

from fastapi.testclient import TestClient


def _build_client(*, db_path: str) -> TestClient:
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["RATE_LIMIT_PER_HOUR"] = "10000"
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MINIMAX_API_KEY"] = "test_upstream_key"
    os.environ.pop("SECRET_KEY", None)
    os.environ.pop("ENCRYPTION_KEY", None)
    os.environ.pop("ALLOW_LEGACY_X_API_KEY", None)
    os.environ.pop("ALLOW_API_KEY_LOGIN", None)

    import backend.config as config
    import backend.main as main
    import backend.routes.admin as admin_routes
    import backend.routes.auth as auth_routes
    import backend.routes.proxy as proxy_routes
    import backend.routes.user as user_routes
    import backend.security as security
    import backend.services.http_client as http_client
    import backend.services.proxy_service as proxy_service
    import backend.services.user_service as user_service

    for mod in (
        config,
        security,
        http_client,
        proxy_service,
        user_service,
        admin_routes,
        proxy_routes,
        auth_routes,
        user_routes,
        main,
    ):
        importlib.reload(mod)

    import backend.database as db

    db.init_db()

    return TestClient(main.app)


def _admin_init_and_login(client: TestClient) -> str:
    strong_pwd = f"T3st!{os.urandom(10).hex()}A#"
    resp = client.post(
        "/api/admin/init",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": strong_pwd},
    )
    if resp.status_code not in (200, 409):
        raise AssertionError(resp.text)
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": strong_pwd},
    )
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    csrf = client.cookies.get("mm_csrf")
    if not csrf:
        raise AssertionError("missing mm_csrf cookie")
    return csrf


def _admin_create_user(client: TestClient, csrf: str, *, username: str) -> str:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        json={"username": username, "quota_5h": 500, "quota_week": 5000},
    )
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    api_key = resp.json().get("api_key")
    if not api_key:
        raise AssertionError("missing api_key in admin response")
    return api_key


# The admin endpoint hands back a `users.api_key` (legacy column).
# ``get_current_auth`` looks at that column only when the request
# comes in with the ``X-API-Key`` header (Bearer tokens are routed
# through the newer `tokens` table). Send both so the test works
# regardless of the caller's auth path.
def _user_headers(api_key: str) -> dict:
    return {
        "X-Forwarded-For": "10.0.0.2",
        "X-API-Key": api_key,
        "Authorization": f"Bearer {api_key}",
    }


class ConversationPaginationTest(unittest.TestCase):
    """`/v1/conversations/{id}` now returns paginated, ordered,
    bounded messages — not an unbounded history dump."""

    def test_default_limit_and_order(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)
        try:
            csrf_admin = _admin_init_and_login(client)
            api_key = _admin_create_user(client, csrf_admin, username="u_conv")

            # Insert 5 user/assistant turns directly.
            db = sqlite3.connect(tmp.name)
            for i in range(5):
                db.execute(
                    "INSERT INTO conversations (user_id, session_id, role, content) VALUES (1, ?, ?, ?)",
                    (f"sess-{i // 2}", "user" if i % 2 == 0 else "assistant", f"m{i}"),
                )
            db.commit()
            db.close()

            # /v1/conversations → list of session_ids
            resp = client.get(
                "/v1/conversations",
                headers=_user_headers(api_key),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            sessions = resp.json()
            self.assertGreaterEqual(len(sessions), 1)

            # /v1/conversations/{sess-0} → messages, oldest first.
            resp = client.get(
                "/v1/conversations/sess-0",
                headers=_user_headers(api_key),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertIn("messages", body)
            self.assertEqual(len(body["messages"]), 2)
            self.assertEqual(body["messages"][0]["role"], "user")
            self.assertEqual(body["messages"][1]["role"], "assistant")
        finally:
            os.unlink(tmp.name)

    def test_limit_caps_response(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)
        try:
            csrf_admin = _admin_init_and_login(client)
            api_key = _admin_create_user(client, csrf_admin, username="u_conv2")
            db = sqlite3.connect(tmp.name)
            for i in range(50):
                db.execute(
                    "INSERT INTO conversations (user_id, session_id, role, content) VALUES (1, 'big', ?, ?)",
                    ("user" if i % 2 == 0 else "assistant", f"m{i}"),
                )
            db.commit()
            db.close()

            resp = client.get(
                "/v1/conversations/big?limit=10",
                headers=_user_headers(api_key),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(len(body["messages"]), 10)
            self.assertTrue(body["has_more"])
            # Chronological order: ids increase, so oldest first.
            contents = [m["content"] for m in body["messages"]]
            self.assertEqual(contents, sorted(contents))
        finally:
            os.unlink(tmp.name)

    def test_before_id_cursor_paginates_back(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)
        try:
            csrf_admin = _admin_init_and_login(client)
            api_key = _admin_create_user(client, csrf_admin, username="u_conv3")
            db = sqlite3.connect(tmp.name)
            ids: List[int] = []
            for i in range(8):
                cur = db.execute(
                    "INSERT INTO conversations (user_id, session_id, role, content) VALUES (1, 'pg', ?, ?)",
                    ("user" if i % 2 == 0 else "assistant", f"m{i}"),
                )
                ids.append(int(cur.lastrowid))
            db.commit()
            db.close()

            # First page: limit=3
            resp = client.get(
                "/v1/conversations/pg?limit=3",
                headers=_user_headers(api_key),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            page1 = resp.json()["messages"]
            self.assertEqual(len(page1), 3)
            # The query is `ORDER BY id DESC LIMIT N` then reversed
            # in Python, so the *first* page shows the *most recent*
            # three messages, in chronological order (m5, m6, m7).
            self.assertEqual([m["content"] for m in page1], ["m5", "m6", "m7"])
            self.assertTrue(resp.json()["has_more"])

            # The cursor `before_id=X+1` should then walk back in
            # time and return the previous three (m2..m5).
            # Inserted row ids are 1..8 for m0..m7; the first page
            # pulls ids 8, 7, 6 (m7, m6, m5). The cursor before_id=7
            # limits to ids < 7, so ids 6, 5, 4 (m5, m4, m3) → in
            # chronological order m3, m4, m5.
            resp = client.get(
                f"/v1/conversations/pg?limit=3&before_id={ids[5] + 1}",
                headers=_user_headers(api_key),
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            page2 = resp.json()["messages"]
            self.assertEqual([m["content"] for m in page2], ["m3", "m4", "m5"])
        finally:
            os.unlink(tmp.name)


class Auth401Test(unittest.TestCase):
    """``get_current_auth`` now distinguishes "no credential" from
    "credential but invalid" with a 401 in both cases."""

    def test_missing_credential_returns_401_with_clear_message(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)
        try:
            resp = client.get(
                "/v1/conversations",
                headers={"X-Forwarded-For": "10.0.0.2"},
            )
            self.assertEqual(resp.status_code, 401, resp.text)
            # The new "missing" message is "未提供凭证".
            detail = resp.json().get("detail", "")
            self.assertIn("凭证", detail)
        finally:
            os.unlink(tmp.name)

    def test_invalid_credential_returns_401_with_invalid_key_message(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)
        try:
            resp = client.get(
                "/v1/conversations",
                headers={
                    "X-Forwarded-For": "10.0.0.2",
                    "X-API-Key": "sk-this-key-does-not-exist",
                },
            )
            self.assertEqual(resp.status_code, 401, resp.text)
            detail = resp.json().get("detail", "")
            # The new "invalid key" message is "无效的 API Key".
            self.assertIn("API Key", detail)
        finally:
            os.unlink(tmp.name)
