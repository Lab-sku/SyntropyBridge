"""Tests for self-service password rotation and profile updates.

Covers the two new self-service flows we landed this milestone:

* ``POST /api/user/password`` — user rotates their own password.
* ``PATCH /api/user/profile`` — user updates their email.
* ``POST /api/admin/password`` — admin rotates their own password.

Each flow is tested for the success path *and* the most common
failure modes (wrong old password, weak new password, missing
CSRF, …) so a future refactor that breaks any of them gets a
loud, fast signal.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from typing import Tuple


def _build_client(*, db_path: str) -> Tuple:
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

    from fastapi.testclient import TestClient

    return TestClient(main.app), db_path


def _close_http_client() -> None:
    try:
        import backend.services.http_client as http_client

        asyncio.run(http_client.aclose_async_client())
    except Exception:
        pass


def _admin_init_and_login(client, password: str | None = None) -> str:
    """Initialise the first admin and log in. Returns the CSRF token."""
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
    if not csrf:
        raise AssertionError("missing mm_csrf cookie")
    return csrf


def _admin_create_user_with_password(
    client,
    csrf: str,
    *,
    username: str = "puser1",
    password: str = "OriginalP@ss1234",
) -> str:
    """Provision a user with a known password and return their API key."""
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        json={"username": username, "password": password, "quota_5h": 5, "quota_week": 10},
    )
    assert resp.status_code == 200, resp.text
    api_key = resp.json().get("api_key")
    assert api_key
    return api_key


def _user_login_api_key(client, api_key: str) -> str:
    """Log the user in via API key and return the user-side CSRF token."""
    resp = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.2"},
        json={"api_key": api_key},
    )
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf, "user mm_csrf cookie missing"
    return csrf


class UserPasswordChangeTest(unittest.TestCase):
    """Self-service password rotation by an authenticated user."""

    def tearDown(self):
        _close_http_client()

    def test_user_can_change_own_password(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        old_password = "OriginalP@ss1234"
        new_password = "NewSecret!1aaA"

        resp = client.post(
            "/api/user/password",
            headers={"X-Forwarded-For": "10.0.0.3", "X-CSRF-Token": user_csrf},
            json={"old_password": old_password, "new_password": new_password},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("message"), "密码已更新")

        # Old password is now wrong
        resp = client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": "10.0.0.4"},
            json={"username": "puser1", "password": old_password},
        )
        self.assertEqual(resp.status_code, 401, resp.text)

        # New password works
        resp = client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": "10.0.0.5"},
            json={"username": "puser1", "password": new_password},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_wrong_old_password_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.post(
            "/api/user/password",
            headers={"X-Forwarded-For": "10.0.0.6", "X-CSRF-Token": user_csrf},
            json={"old_password": "WrongOldP@ss1", "new_password": "NewSecret!1aaA"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("原密码", resp.json().get("detail", ""))

    def test_weak_new_password_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        # Too short — the change_password() helper delegates to
        # Security.assert_strong_password, which requires 12+ chars and
        # 3-of-4 character classes.
        resp = client.post(
            "/api/user/password",
            headers={"X-Forwarded-For": "10.0.0.7", "X-CSRF-Token": user_csrf},
            json={"old_password": "OriginalP@ss1234", "new_password": "short"},
        )
        self.assertEqual(resp.status_code == 400, True, resp.text)

    def test_missing_csrf_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        _user_login_api_key(client, api_key)

        resp = client.post(
            "/api/user/password",
            headers={"X-Forwarded-For": "10.0.0.8"},
            json={"old_password": "OriginalP@ss1234", "new_password": "NewSecret!1aaA"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_password_change_writes_audit_log(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.post(
            "/api/user/password",
            headers={"X-Forwarded-For": "10.0.0.30", "X-CSRF-Token": user_csrf},
            json={"old_password": "OriginalP@ss1234", "new_password": "NewSecret!1aaA"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT action, ip_address, metadata FROM audit_logs "
            "WHERE action = 'USER_CHANGE_PASSWORD' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "expected USER_CHANGE_PASSWORD audit log")
        self.assertEqual(row["ip_address"], "10.0.0.30")
        # We deliberately do NOT log the new password (or hash).
        # metadata should be NULL or empty.
        self.assertFalse(row["metadata"], "must not log password material")


class UserProfileUpdateTest(unittest.TestCase):
    """Self-service email update via PATCH /api/user/profile."""

    def tearDown(self):
        _close_http_client()

    def test_user_can_update_email(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.9", "X-CSRF-Token": user_csrf},
            json={
                "email": "newemail@example.com",
                "current_password": "OriginalP@ss1234",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("message"), "已更新")

    def test_user_can_clear_email(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.10", "X-CSRF-Token": user_csrf},
            json={"email": ""},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_invalid_email_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.11", "X-CSRF-Token": user_csrf},
            json={"email": "not-an-email"},
        )
        # ProfileUpdateRequest declares email as plain str (not
        # EmailStr), so the lightweight check inside
        # UserService.update_user is the one that fires — it
        # returns 400 with a friendly Chinese message.
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("邮箱", resp.json().get("detail", ""))

    def test_user_can_update_username(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.20", "X-CSRF-Token": user_csrf},
            json={"username": "newname_123"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("message"), "已更新")

        # New username is reflected in the DB. Query by *new* username
        # (api_key returned by create_user is masked, so the original
        # plaintext is not available to the test).
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT username FROM users WHERE username = ?", ("newname_123",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "new username not found in DB")
        self.assertEqual(row[0], "newname_123")

    def test_username_uniqueness_enforced(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        # Create two users
        _admin_create_user_with_password(
            client, csrf, username="alpha", password="OriginalP@ss1234"
        )
        api_key = _admin_create_user_with_password(
            client, csrf, username="bravo", password="OriginalP@ss1234"
        )
        user_csrf = _user_login_api_key(client, api_key)

        # bravo tries to take "alpha"'s username
        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.21", "X-CSRF-Token": user_csrf},
            json={"username": "alpha"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("用户名", resp.json().get("detail", ""))

    def test_username_too_short_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.22", "X-CSRF-Token": user_csrf},
            json={"username": "a"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("2-50", resp.json().get("detail", ""))

    def test_username_too_long_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.23", "X-CSRF-Token": user_csrf},
            json={"username": "x" * 51},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("2-50", resp.json().get("detail", ""))

    def test_username_change_writes_audit_log(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(
            client, csrf, username="audit_before", password="OriginalP@ss1234"
        )
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.24", "X-CSRF-Token": user_csrf},
            json={"username": "audit_after"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT action, metadata, ip_address FROM audit_logs "
            "WHERE action = 'USER_CHANGE_USERNAME' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "expected USER_CHANGE_USERNAME audit log")
        self.assertEqual(row["ip_address"], "10.0.0.24")
        import json as _json
        details = _json.loads(row["metadata"])
        self.assertEqual(details.get("old"), "audit_before")
        self.assertEqual(details.get("new"), "audit_after")

    def test_username_no_op_writes_no_audit(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(
            client, csrf, username="same_name", password="OriginalP@ss1234"
        )
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.25", "X-CSRF-Token": user_csrf},
            json={"username": "same_name"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'USER_CHANGE_USERNAME'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "no-op username change must not log")

    def test_username_change_requires_csrf(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(client, csrf)
        _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.26"},
            json={"username": "without_csrf"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_email_change_writes_audit_log(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        csrf = _admin_init_and_login(client)
        api_key = _admin_create_user_with_password(
            client, csrf, username="email_auditor", password="OriginalP@ss1234"
        )
        user_csrf = _user_login_api_key(client, api_key)

        resp = client.patch(
            "/api/user/profile",
            headers={"X-Forwarded-For": "10.0.0.27", "X-CSRF-Token": user_csrf},
            json={
                "email": "audit@example.com",
                "current_password": "OriginalP@ss1234",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT action, metadata FROM audit_logs "
            "WHERE action = 'USER_CHANGE_EMAIL' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "expected USER_CHANGE_EMAIL audit log")
        import json as _json
        details = _json.loads(row["metadata"])
        self.assertEqual(details.get("new"), "audit@example.com")


class AdminPasswordChangeTest(unittest.TestCase):
    """Self-service password rotation for an authenticated admin."""

    def tearDown(self):
        _close_http_client()

    def test_admin_can_change_own_password(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        old_pwd = f"T3st!{os.urandom(10).hex()}A#"
        csrf = _admin_init_and_login(client, password=old_pwd)

        new_pwd = f"N3w!{os.urandom(10).hex()}A#"

        resp = client.post(
            "/api/admin/password",
            headers={"X-Forwarded-For": "10.0.0.12", "X-CSRF-Token": csrf},
            json={"old_password": old_pwd, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json().get("message"), "管理员密码已更新")

        # The new password now works on a fresh login. We log out
        # first because the old session is still valid (we don't
        # invalidate other admin sessions on a password change —
        # only the rotating admin would normally have multiple
        # sessions).
        client.cookies.clear()
        resp = client.post(
            "/api/admin/login",
            headers={"X-Forwarded-For": "10.0.0.13"},
            json={"username": "admin", "password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # And the old password is rejected.
        client.cookies.clear()
        resp = client.post(
            "/api/admin/login",
            headers={"X-Forwarded-For": "10.0.0.14"},
            json={"username": "admin", "password": old_pwd},
        )
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_admin_password_change_writes_audit_log(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)
        old_pwd = f"T3st!{os.urandom(10).hex()}A#"
        csrf = _admin_init_and_login(client, password=old_pwd)

        new_pwd = f"N3w!{os.urandom(10).hex()}A#"

        resp = client.post(
            "/api/admin/password",
            headers={"X-Forwarded-For": "10.0.0.15", "X-CSRF-Token": csrf},
            json={"old_password": old_pwd, "new_password": new_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT action FROM audit_logs WHERE action = ?",
            ("ADMIN_CHANGE_PASSWORD",),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1, "expected one audit log entry")

    def test_admin_password_change_wrong_old_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        old_pwd = f"T3st!{os.urandom(10).hex()}A#"
        csrf = _admin_init_and_login(client, password=old_pwd)

        resp = client.post(
            "/api/admin/password",
            headers={"X-Forwarded-For": "10.0.0.16", "X-CSRF-Token": csrf},
            json={"old_password": "definitely_wrong", "new_password": "N3w!ValidP@ss1A#"},
        )
        # P1.7: wrong-password now goes through the lockout helper,
        # which returns 403 "管理员密码验证失败" (same as the other
        # sensitive admin endpoints) instead of the old 400 "原密码错误".
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertIn("密码验证失败", resp.json().get("detail", ""))

    def test_admin_password_change_weak_new_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)
        old_pwd = f"T3st!{os.urandom(10).hex()}A#"
        csrf = _admin_init_and_login(client, password=old_pwd)

        resp = client.post(
            "/api/admin/password",
            headers={"X-Forwarded-For": "10.0.0.17", "X-CSRF-Token": csrf},
            json={"old_password": old_pwd, "new_password": "x"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)


if __name__ == "__main__":
    unittest.main()
