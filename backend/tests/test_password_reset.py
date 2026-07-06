"""Integration tests for the password reset flow (Phase 11).

Covers:
- POST /api/auth/forgot-password
- GET  /api/auth/reset-password/validate
- POST /api/auth/reset-password
- POST /api/admin/users/{id}/send-reset-email
- End-to-end reset scenario
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers — mirror the _build_client pattern from test_regression_flows.py
# ---------------------------------------------------------------------------


def _build_client(
    *, db_path: str, rate_limit_per_minute: str = "100", rate_limit_per_hour: str = "1000"
) -> tuple[TestClient, str]:
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_PER_MINUTE"] = rate_limit_per_minute
    os.environ["RATE_LIMIT_PER_HOUR"] = rate_limit_per_hour
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
    import backend.services.channel_service as channel_service
    import backend.services.http_client as http_client
    import backend.services.proxy_service as proxy_service
    import backend.services.user_service as user_service

    importlib.reload(config)
    importlib.reload(security)
    importlib.reload(http_client)
    importlib.reload(proxy_service)
    importlib.reload(channel_service)
    importlib.reload(user_service)
    importlib.reload(admin_routes)
    importlib.reload(proxy_routes)
    importlib.reload(auth_routes)
    importlib.reload(user_routes)
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


def _strong_password() -> str:
    """Return a unique strong password that passes assert_strong_password."""
    return f"T3st!{os.urandom(8).hex()}Z#"


def _admin_init_and_login(client: TestClient, *, ip: str = "10.0.0.1") -> str:
    strong_pwd = _strong_password()
    client.post(
        "/api/admin/init",
        headers={"X-Forwarded-For": ip},
        json={"username": "admin", "password": strong_pwd},
    )
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": ip},
        json={"username": "admin", "password": strong_pwd},
    )
    assert resp.status_code == 200, resp.text
    return client.cookies.get("mm_csrf")


def _create_user_with_email(
    client: TestClient,
    csrf: str,
    *,
    username: str = "testuser",
    email: str = "test@example.com",
    password: str | None = None,
    ip: str = "10.0.0.1",
) -> dict:
    """Admin creates a user. Returns the full response JSON."""
    payload: dict = {
        "username": username,
        "email": email,
        "quota_5h": 500,
        "quota_week": 5000,
    }
    if password:
        payload["password"] = password
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": ip, "X-CSRF-Token": csrf},
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _user_login(client: TestClient, username: str, password: str, *, ip: str = "10.0.0.2"):
    return client.post(
        "/api/auth/login",
        headers={"X-Forwarded-For": ip},
        json={"username": username, "password": password},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestForgotPassword(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_valid_email_creates_token(self, *_):
        """POST /api/auth/forgot-password with a registered email creates a
        password_reset_tokens row with ~1h expiry."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd = _strong_password()
        _create_user_with_email(client, csrf, username="u1", email="u1@test.com", password=pwd)

        resp = client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.5"},
            json={"email": "u1@test.com"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Verify a token was created
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = ?)",
            ("u1@test.com",),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        # Expiry should be ~1h from now
        expires_at = datetime.fromisoformat(rows[0]["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        diff = expires_at - datetime.now(timezone.utc)
        self.assertGreater(diff.total_seconds(), 3500)  # > 58 minutes

    def test_invalid_email_no_token_no_leak(self):
        """An unregistered email returns 200 (no leak) but creates no token."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        _admin_init_and_login(client)

        resp = client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.6"},
            json={"email": "nobody@example.com"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("如果邮箱已注册", resp.json().get("message", ""))

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM password_reset_tokens").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_empty_body_returns_422(self):
        """Missing body should produce a 422 validation error."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        _admin_init_and_login(client)

        resp = client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.7"},
            json={},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_rate_limiting_returns_429(self):
        """Too many forgot-password requests from same IP triggers 429."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        _admin_init_and_login(client)

        # The rate limit is 5 per 60 seconds per IP for forgot-password
        ip = "10.0.0.99"
        for i in range(6):
            resp = client.post(
                "/api/auth/forgot-password",
                headers={"X-Forwarded-For": ip},
                json={"email": f"any{i}@test.com"},
            )
        self.assertEqual(resp.status_code, 429, resp.text)


class TestValidateResetToken(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def _setup_user_and_token(self, expired: bool = False, used: bool = False):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd = _strong_password()
        _create_user_with_email(
            client, csrf, username="valuser", email="val@test.com", password=pwd
        )

        # Trigger forgot-password to create a token
        resp = client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.10"},
            json={"email": "val@test.com"},
        )
        self.assertEqual(resp.status_code, 200)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT token FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = ?)",
            ("val@test.com",),
        ).fetchone()
        token = row["token"]

        if expired:
            past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            conn.execute(
                "UPDATE password_reset_tokens SET expires_at = ? WHERE token = ?",
                (past, token),
            )
        if used:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?",
                (now, token),
            )
        conn.commit()
        conn.close()
        return client, token, db_path

    def test_valid_unused_unexpired_token(self):
        client, token, _ = self._setup_user_and_token()
        resp = client.get(f"/api/auth/reset-password/validate?token={token}")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["valid"])
        self.assertIsNotNone(body["expires_at"])

    def test_expired_token(self):
        client, token, _ = self._setup_user_and_token(expired=True)
        resp = client.get(f"/api/auth/reset-password/validate?token={token}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["valid"])

    def test_already_used_token(self):
        client, token, _ = self._setup_user_and_token(used=True)
        resp = client.get(f"/api/auth/reset-password/validate?token={token}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["valid"])

    def test_nonexistent_token(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        _admin_init_and_login(client)

        resp = client.get("/api/auth/reset-password/validate?token=doesnotexist")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["valid"])


class TestResetPassword(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def _get_reset_token(self, client, db_path, email):
        client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.20"},
            json={"email": email},
        )
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT token FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = ?) AND used_at IS NULL",
            (email,),
        ).fetchone()
        conn.close()
        return row["token"] if row else None

    def test_valid_token_strong_password(self):
        """Valid token + strong new password resets successfully."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="resetme", email="reset@test.com", password=pwd1
        )

        token = self._get_reset_token(client, db_path, "reset@test.com")
        self.assertIsNotNone(token)

        new_pwd = _strong_password()
        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.21"},
            json={"token": token, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("密码重置成功", resp.json().get("message", ""))

    def test_weak_password_rejected(self):
        """password1 is too weak (fails assert_strong_password)."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="weakuser", email="weak@test.com", password=pwd1
        )

        token = self._get_reset_token(client, db_path, "weak@test.com")

        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.22"},
            json={"token": token, "new_password": "password1"},
        )
        # Either 400 or 422 depending on where validation catches it
        self.assertIn(resp.status_code, (400, 422), resp.text)

    def test_password_containing_username_rejected(self):
        """Phase 10-B: password containing username is rejected at reset-password endpoint."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(client, csrf, username="john", email="john@test.com", password=pwd1)

        token = self._get_reset_token(client, db_path, "john@test.com")

        # The handler re-validates the password with the username after token
        # lookup, so a password containing "john" must be rejected with 400.
        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.23"},
            json={"token": token, "new_password": "john12345Abc!"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("用户名", resp.json().get("detail", ""))

    def test_expired_token_rejected(self):
        """Expired token yields 400."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="expuser", email="exp@test.com", password=pwd1
        )

        token = self._get_reset_token(client, db_path, "exp@test.com")

        # Manually expire the token
        conn = sqlite3.connect(db_path)
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            "UPDATE password_reset_tokens SET expires_at = ? WHERE token = ?",
            (past, token),
        )
        conn.commit()
        conn.close()

        new_pwd = _strong_password()
        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.24"},
            json={"token": token, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("过期", resp.json().get("detail", ""))

    def test_already_used_token_rejected(self):
        """Token that has already been used yields 400."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="useduser", email="used@test.com", password=pwd1
        )

        token = self._get_reset_token(client, db_path, "used@test.com")

        # Use the token once
        new_pwd = _strong_password()
        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.25"},
            json={"token": token, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Try again with the same token
        resp2 = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.25"},
            json={"token": token, "new_password": _strong_password()},
        )
        self.assertEqual(resp2.status_code, 400, resp2.text)

    def test_sessions_invalidated_after_reset(self):
        """After a successful reset, all existing sessions for the user are deleted."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="sessuser", email="sess@test.com", password=pwd1
        )

        # Log in as the user to create a session
        login_resp = _user_login(client, "sessuser", pwd1, ip="10.0.0.30")
        self.assertEqual(login_resp.status_code, 200, login_resp.text)

        # Verify session works
        session_resp = client.get("/api/user/config", headers={"X-Forwarded-For": "10.0.0.30"})
        self.assertEqual(session_resp.status_code, 200, session_resp.text)

        # Reset password
        token = self._get_reset_token(client, db_path, "sess@test.com")
        new_pwd = _strong_password()
        resp = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.31"},
            json={"token": token, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Old session should be invalidated
        old_session_resp = client.get("/api/user/config", headers={"X-Forwarded-For": "10.0.0.30"})
        self.assertEqual(old_session_resp.status_code, 401, old_session_resp.text)


class TestAdminSendResetEmail(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_admin_creates_token_for_user_with_email(self):
        """Admin triggers reset for user with email: token created + audit log."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        pwd = _strong_password()
        user = _create_user_with_email(
            client, csrf, username="emailuser", email="eu@test.com", password=pwd
        )
        user_id = user["id"]

        resp = client.post(
            f"/api/admin/users/{user_id}/send-reset-email",
            headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("email_sent"))

        # Token exists in DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tokens = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE user_id = ?", (user_id,)
        ).fetchall()
        # Audit log entry
        logs = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'ADMIN_SEND_RESET_EMAIL' AND target_id = ?",
            (str(user_id),),
        ).fetchall()
        conn.close()
        self.assertEqual(len(tokens), 1)
        self.assertGreaterEqual(len(logs), 1)

    def test_admin_creates_token_for_user_without_email(self):
        """When user has no email, token is created and reset_url is returned."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        # Create user without email
        resp = client.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
            json={"username": "noemail", "quota_5h": 500, "quota_week": 5000},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        user_id = resp.json()["id"]

        resp2 = client.post(
            f"/api/admin/users/{user_id}/send-reset-email",
            headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        )
        self.assertEqual(resp2.status_code, 200, resp2.text)
        body = resp2.json()
        self.assertFalse(body.get("email_sent"))
        self.assertIn("reset_url", body)

    def test_non_admin_gets_401(self):
        """Unauthenticated caller gets 401."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        _admin_init_and_login(client)

        # Clear cookies to simulate unauthenticated request
        client.cookies.clear()
        resp = client.post(
            "/api/admin/users/1/send-reset-email",
            headers={"X-Forwarded-For": "10.0.0.50"},
        )
        self.assertIn(resp.status_code, (401, 403), resp.text)


class TestEndToEndPasswordReset(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_full_reset_flow(self):
        """User creates account -> requests reset -> resets -> old pw fails, new pw works."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)

        # Admin creates user with password P1
        pwd1 = _strong_password()
        _create_user_with_email(
            client, csrf, username="e2e_user", email="e2e@test.com", password=pwd1
        )

        # User logs in with P1
        resp_login1 = _user_login(client, "e2e_user", pwd1, ip="10.0.0.40")
        self.assertEqual(resp_login1.status_code, 200, resp_login1.text)
        # Clear user session for next steps
        client.cookies.clear()

        # User requests password reset
        resp_forgot = client.post(
            "/api/auth/forgot-password",
            headers={"X-Forwarded-For": "10.0.0.41"},
            json={"email": "e2e@test.com"},
        )
        self.assertEqual(resp_forgot.status_code, 200)

        # Retrieve the token from DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT token FROM password_reset_tokens WHERE user_id = (SELECT id FROM users WHERE email = ?) AND used_at IS NULL",
            ("e2e@test.com",),
        ).fetchone()
        conn.close()
        token = row["token"]

        # Reset to P2
        pwd2 = _strong_password()
        resp_reset = client.post(
            "/api/auth/reset-password",
            headers={"X-Forwarded-For": "10.0.0.42"},
            json={"token": token, "new_password": pwd2},
        )
        self.assertEqual(resp_reset.status_code, 200, resp_reset.text)

        # Login with P1 fails
        resp_old = _user_login(client, "e2e_user", pwd1, ip="10.0.0.43")
        self.assertEqual(resp_old.status_code, 401, resp_old.text)

        # Login with P2 succeeds
        resp_new = _user_login(client, "e2e_user", pwd2, ip="10.0.0.44")
        self.assertEqual(resp_new.status_code, 200, resp_new.text)


if __name__ == "__main__":
    unittest.main()
