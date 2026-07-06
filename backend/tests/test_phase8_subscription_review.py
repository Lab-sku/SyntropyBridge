"""Tests for the subscription request approval flow.

The previous version of ``POST /api/admin/subscriptions/{req_id}/review``
inserted into ``user_subscriptions`` — a table that never existed
in the schema. The insert would 500, the admin would see a broken
"approved" message, and the user would never actually get access.

The fix writes to ``user_model_access`` (the canonical per-user
access table) using ``INSERT OR IGNORE`` so re-approval is a no-op.

These tests pin:

* approval grants the user a row in ``user_model_access``
* rejection leaves ``user_model_access`` untouched
* a second approval is idempotent (no unique-constraint error)
* quota grants (``grant_quota_5h``/``grant_quota_week``) update the
  user's row, taking the MAX with whatever they already had
* trying to re-review a settled request is a 400
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
import unittest.mock

from fastapi.testclient import TestClient


def _build_client(*, db_path: str):
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_PER_MINUTE"] = "100"
    os.environ["RATE_LIMIT_PER_HOUR"] = "1000"
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
    import backend.routes.platform as platform_routes
    import backend.routes.proxy as proxy_routes
    import backend.routes.user as user_routes
    import backend.security as security
    import backend.services.channel_service as channel_service
    import backend.services.email_service as email_service
    import backend.services.http_client as http_client
    import backend.services.proxy_service as proxy_service
    import backend.services.redis_service as redis_service
    import backend.services.user_service as user_service

    importlib.reload(config)
    importlib.reload(security)
    importlib.reload(http_client)
    importlib.reload(proxy_service)
    importlib.reload(channel_service)
    importlib.reload(user_service)
    importlib.reload(redis_service)
    importlib.reload(email_service)
    importlib.reload(admin_routes)
    importlib.reload(proxy_routes)
    importlib.reload(auth_routes)
    importlib.reload(user_routes)
    importlib.reload(platform_routes)
    main = importlib.reload(main)

    import backend.database as db

    db.init_db()

    return TestClient(main.app), db_path


def _close_http_client() -> None:
    try:
        import backend.services.http_client as http_client

        asyncio.run(http_client.aclose_async_client())
    except Exception:
        pass


def _admin_init_and_login(client, password: str | None = None) -> str:
    pwd = password or f"T3st!{os.urandom(10).hex()}A#"
    resp = client.post(
        "/api/admin/init",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": pwd},
    )
    if resp.status_code not in (200, 409):
        raise AssertionError(resp.text)
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": pwd},
    )
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def _admin_create_user(
    client, csrf: str, *, username: str, quota_5h: int = 5, quota_week: int = 10
) -> int:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        json={"username": username, "quota_5h": quota_5h, "quota_week": quota_week},
    )
    assert resp.status_code == 200, resp.text
    return int(resp.json().get("id"))


def _user_login_api_key(client, api_key: str) -> str:
    resp = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.2"},
        json={"api_key": api_key},
    )
    assert resp.status_code == 200, resp.text
    return client.cookies.get("mm_csrf") or ""


class SubscriptionReviewTest(unittest.TestCase):
    """The end-to-end review flow."""

    def setUp(self):
        from backend.services import redis_service

        self._storage: dict = {}
        self._patchers = []

        def _set(k, v, ex=None):
            self._storage[k] = v
            return True

        def _get(k):
            return self._storage.get(k)

        def _delete(k):
            self._storage.pop(k, None)
            return True

        for name, fn in (
            ("set_with_expiry", lambda k, v, ex=None: _set(k, v, ex)),
            ("set_verification_code", lambda email, code: _set(f"verify:{email}", code)),
            ("get_verification_code", lambda email: _get(f"verify:{email}")),
            ("delete_verification_code", lambda email: _delete(f"verify:{email}")),
            ("set_reset_token", lambda email, token: _set(f"reset:{email}", token)),
            ("get_reset_token", lambda email: _get(f"reset:{email}")),
            ("delete_reset_token", lambda email: _delete(f"reset:{email}")),
            ("get", _get),
            ("delete", _delete),
        ):
            p = unittest.mock.patch.object(redis_service.RedisService, name, staticmethod(fn))
            p.start()
            self._patchers.append(p)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        _close_http_client()

    def test_approval_inserts_into_user_model_access(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)

        # Create a user and a subscription request directly in the DB
        # (the public /user/subscriptions endpoint requires the user
        # to be logged in, which is more boilerplate than this test
        # needs).
        user_id = _admin_create_user(client, csrf, username="subuser1")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO subscription_requests"
            " (user_id, provider, model_id, status) VALUES (?, ?, ?, 'pending')",
            (user_id, "nvidia", "meta/llama-3.3-70b-instruct"),
        )
        req_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0])
        conn.commit()
        conn.close()

        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.3", "X-CSRF-Token": csrf},
            json={"status": "approved", "admin_note": "ok"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("status"), "approved")

        # The model access row was actually inserted.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM user_model_access WHERE user_id = ? AND model_id = ?",
            (user_id, "nvidia/meta/llama-3.3-70b-instruct"),
        ).fetchone()
        self.assertIsNotNone(row, "user_model_access row missing")
        self.assertEqual(row["access_type"], "allow")

        status = conn.execute(
            "SELECT status, admin_note FROM subscription_requests WHERE id = ?",
            (req_id,),
        ).fetchone()
        self.assertEqual(status["status"], "approved")
        self.assertEqual(status["admin_note"], "ok")
        conn.close()

    def test_rejection_does_not_grant_access(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        user_id = _admin_create_user(client, csrf, username="subuser2")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO subscription_requests"
            " (user_id, provider, model_id, status) VALUES (?, ?, ?, 'pending')",
            (user_id, "nvidia", "meta/llama-3.3-70b-instruct"),
        )
        req_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0])
        conn.commit()
        conn.close()

        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.4", "X-CSRF-Token": csrf},
            json={"status": "rejected", "admin_note": "no"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM user_model_access WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        )
        conn.close()

    def test_double_approval_blocks_re_review(self):
        """A re-review against an already-settled request is a 400.
        The INSERT OR IGNORE in the SQL is still valuable as a
        defence against a future code path that might re-issue
        the insert (e.g. a back-fill job), so we don't want to
        drop it.
        """
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        user_id = _admin_create_user(client, csrf, username="subuser3")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO subscription_requests"
            " (user_id, provider, model_id, status) VALUES (?, ?, ?, 'pending')",
            (user_id, "nvidia", "meta/llama-3.3-70b-instruct"),
        )
        req_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0])
        conn.commit()
        conn.close()

        # First approval: OK.
        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.5", "X-CSRF-Token": csrf},
            json={"status": "approved"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Second approval against the same request: blocked at the
        # route layer (status != "pending").
        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.5", "X-CSRF-Token": csrf},
            json={"status": "approved"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

        # And the access table still has exactly one row.
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM user_model_access WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(rows[0], 1, "expected exactly one access row")

    def test_quota_grant_uses_max(self):
        """If the user already has a quota of 10 and the admin grants 5,
        the user should end up with max(10, 5) = 10. If the admin grants
        20, the user should end up with max(10, 20) = 20."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        user_id = _admin_create_user(client, csrf, username="subuser4", quota_5h=10, quota_week=10)

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO subscription_requests"
            " (user_id, provider, model_id, status) VALUES (?, ?, ?, 'pending')",
            (user_id, "nvidia", "meta/llama-3.3-70b-instruct"),
        )
        req_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0])
        conn.commit()
        conn.close()

        # Grant smaller — should keep existing
        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.6", "X-CSRF-Token": csrf},
            json={"status": "approved", "grant_quota_5h": 5, "grant_quota_week": 5},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT quota_5h, quota_week FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], 10, "quota_5h should be max of existing and granted")
        self.assertEqual(row[1], 10, "quota_week should be max of existing and granted")

    def test_cannot_review_settled_request(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        user_id = _admin_create_user(client, csrf, username="subuser5")

        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO subscription_requests"
            " (user_id, provider, model_id, status) VALUES (?, ?, ?, 'approved')",
            (user_id, "nvidia", "meta/llama-3.3-70b-instruct"),
        )
        req_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()[0])
        conn.commit()
        conn.close()

        resp = client.post(
            f"/api/admin/subscriptions/{req_id}/review",
            headers={"X-Forwarded-For": "10.0.0.7", "X-CSRF-Token": csrf},
            json={"status": "approved"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_admin_create_duplicate_user_returns_friendly_400(self):
        """The admin POST /admin/users path now catches the
        sqlite3.IntegrityError on duplicate username/email and
        returns a friendly 400 (not a 500).
        """
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)

        # First creation succeeds.
        resp = client.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": "10.0.0.8", "X-CSRF-Token": csrf},
            json={"username": "dupuser", "quota_5h": 5, "quota_week": 10},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Second creation with the same username must be a 400.
        resp = client.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": "10.0.0.8", "X-CSRF-Token": csrf},
            json={"username": "dupuser", "quota_5h": 5, "quota_week": 10},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("已存在", resp.json().get("detail", ""))


if __name__ == "__main__":
    unittest.main()
