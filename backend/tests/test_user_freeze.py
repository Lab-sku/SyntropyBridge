"""Integration tests for the freeze / unfreeze flow (Phase 11-E).

Covers:
- POST /api/admin/users/{id}/freeze
- POST /api/admin/users/{id}/unfreeze
- Login blocking after freeze
- Session invalidation on freeze
- End-to-end freeze / unfreeze lifecycle
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
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
    os.environ["ALLOW_API_KEY_LOGIN"] = "true"
    os.environ.pop("SECRET_KEY", None)
    os.environ.pop("ENCRYPTION_KEY", None)
    os.environ.pop("ALLOW_LEGACY_X_API_KEY", None)

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
    return f"T3st!{os.urandom(8).hex()}Z#"


def _admin_init_and_login(
    client: TestClient, *, ip: str = "10.0.0.1", password: str | None = None
) -> tuple[str, str]:
    """Init admin and log in. Returns (csrf, admin_password).

    If ``password`` is provided, use it instead of generating a new one.
    This is needed when re-logging-in as admin in the same test.
    """
    strong_pwd = password or _strong_password()
    init_resp = client.post(
        "/api/admin/init",
        headers={"X-Forwarded-For": ip},
        json={"username": "admin", "password": strong_pwd},
    )
    # 409 means admin already exists — that's fine, just login
    assert init_resp.status_code in (200, 409), init_resp.text
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": ip},
        json={"username": "admin", "password": strong_pwd},
    )
    assert resp.status_code == 200, resp.text
    return client.cookies.get("mm_csrf"), strong_pwd


def _admin_login(client: TestClient, password: str, *, ip: str = "10.0.0.1") -> str:
    """Log in as existing admin. Returns CSRF token."""
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": ip},
        json={"username": "admin", "password": password},
    )
    assert resp.status_code == 200, resp.text
    return client.cookies.get("mm_csrf")


def _create_user_with_password(
    client: TestClient,
    csrf: str,
    *,
    username: str = "u1",
    password: str | None = None,
    email: str | None = None,
    ip: str = "10.0.0.1",
) -> dict:
    payload: dict = {
        "username": username,
        "quota_5h": 500,
        "quota_week": 5000,
    }
    if password:
        payload["password"] = password
    if email:
        payload["email"] = email
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


def _api_key_login(client: TestClient, api_key: str, *, ip: str = "10.0.0.2"):
    return client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": ip},
        json={"api_key": api_key},
    )


def _freeze_user(
    client: TestClient,
    csrf: str,
    user_id: int,
    *,
    reason: str = "testing",
    ip: str = "10.0.0.1",
):
    return client.post(
        f"/api/admin/users/{user_id}/freeze",
        headers={"X-Forwarded-For": ip, "X-CSRF-Token": csrf},
        json={"reason": reason},
    )


def _unfreeze_user(
    client: TestClient,
    csrf: str,
    user_id: int,
    *,
    reason: str = "done testing",
    ip: str = "10.0.0.1",
):
    return client.post(
        f"/api/admin/users/{user_id}/unfreeze",
        headers={"X-Forwarded-For": ip, "X-CSRF-Token": csrf},
        json={"reason": reason},
    )


# ---------------------------------------------------------------------------
# Tests — Freeze
# ---------------------------------------------------------------------------


class TestFreezeUser(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_admin_freezes_user(self):
        """Admin freezes user: is_active=0, sessions deleted, audit log entry."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, admin_pwd = _admin_init_and_login(client)
        pwd = _strong_password()
        user = _create_user_with_password(client, csrf, username="freeze1", password=pwd)
        user_id = user["id"]

        # Log in as user to create a session (this replaces admin cookies)
        _user_login(client, "freeze1", pwd, ip="10.0.0.10")

        # Re-login as admin to get admin session back
        client.cookies.clear()
        csrf = _admin_login(client, admin_pwd, ip="10.0.0.1")

        resp = _freeze_user(client, csrf, user_id, reason="bad behavior")
        self.assertEqual(resp.status_code, 200, resp.text)

        # Check DB: is_active = 0
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()
        self.assertEqual(row["is_active"], 0)

        # Sessions deleted
        sessions = conn.execute(
            "SELECT COUNT(*) as cnt FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        self.assertEqual(sessions["cnt"], 0)

        # Audit log entry
        logs = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'ADMIN_FREEZE_USER' AND target_id = ?",
            (str(user_id),),
        ).fetchall()
        conn.close()
        self.assertGreaterEqual(len(logs), 1)

    def test_freeze_already_frozen_is_idempotent(self):
        """Freezing an already-frozen user returns 200."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user_with_password(client, csrf, username="freeze2")
        user_id = user["id"]

        resp1 = _freeze_user(client, csrf, user_id, reason="first")
        self.assertEqual(resp1.status_code, 200)

        resp2 = _freeze_user(client, csrf, user_id, reason="second")
        self.assertEqual(resp2.status_code, 200, resp2.text)

    def test_non_admin_gets_401(self):
        """Unauthenticated caller cannot freeze users."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        _admin_init_and_login(client)
        # Clear cookies
        client.cookies.clear()

        resp = client.post(
            "/api/admin/users/1/freeze",
            headers={"X-Forwarded-For": "10.0.0.20"},
            json={"reason": "hacking"},
        )
        self.assertIn(resp.status_code, (401, 403), resp.text)

    def test_freeze_self_rejected_or_allowed(self):
        """Admin freezing themselves: behavior depends on implementation.

        The current implementation does NOT prevent admin from freezing
        themselves (it operates on the users table, not admin_users).
        This test documents the current behavior.
        """
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, admin_pwd = _admin_init_and_login(client)

        # Get admin_id from the session
        session_resp = client.get("/api/auth/session", headers={"X-Forwarded-For": "10.0.0.1"})
        admin_id = session_resp.json().get("admin_id")

        # Try to freeze admin_id as a user (this won't find the admin
        # in the users table, so it returns 404)
        resp = _freeze_user(client, csrf, admin_id, reason="self-freeze")
        # admin_id references admin_users table, not users table.
        # So it should be 404 (user not found) unless admin_id happens
        # to collide with a user_id.
        self.assertIn(resp.status_code, (200, 404), resp.text)


# ---------------------------------------------------------------------------
# Tests — Unfreeze
# ---------------------------------------------------------------------------


class TestUnfreezeUser(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_admin_unfreezes_user(self):
        """Admin unfreezes user: is_active=1 + audit log."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user_with_password(client, csrf, username="unfr1")
        user_id = user["id"]

        _freeze_user(client, csrf, user_id)
        resp = _unfreeze_user(client, csrf, user_id)
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()
        self.assertEqual(row["is_active"], 1)

        logs = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'ADMIN_UNFREEZE_USER' AND target_id = ?",
            (str(user_id),),
        ).fetchall()
        conn.close()
        self.assertGreaterEqual(len(logs), 1)

    def test_unfreeze_already_active_is_idempotent(self):
        """Unfreezing an already-active user returns 200."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user_with_password(client, csrf, username="unfr2")
        user_id = user["id"]

        # User is already active, unfreeze should still succeed
        resp = _unfreeze_user(client, csrf, user_id)
        self.assertEqual(resp.status_code, 200, resp.text)


# ---------------------------------------------------------------------------
# Tests — Login blocking after freeze
# ---------------------------------------------------------------------------


class TestLoginBlockingAfterFreeze(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_password_login_frozen_user_401(self):
        """Password login for frozen user returns 401 (P1.5: do not
        leak that the password was correct by returning 403)."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        pwd = _strong_password()
        user = _create_user_with_password(client, csrf, username="froz1", password=pwd)
        user_id = user["id"]

        _freeze_user(client, csrf, user_id)

        resp = _user_login(client, "froz1", pwd, ip="10.0.0.30")
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertIn("用户名或密码错误", resp.json().get("detail", ""))

    def test_api_key_login_frozen_user_401(self):
        """API key login for frozen user returns 401 (P1.5: do not
        leak that the API key was valid by returning 403)."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user_with_password(client, csrf, username="froz2")
        api_key = user["api_key"]
        user_id = user["id"]

        _freeze_user(client, csrf, user_id)

        resp = _api_key_login(client, api_key, ip="10.0.0.31")
        # The get_user_by_api_key only queries WHERE is_active=1, so
        # the frozen user won't be found -> 401 "无效的API Key"
        self.assertEqual(resp.status_code, 401, resp.text)

    def test_existing_session_invalidated_after_freeze(self):
        """After freeze, API call with old session cookie returns 401."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _ = _build_client(db_path=tmp.name)

        csrf, admin_pwd = _admin_init_and_login(client)
        pwd = _strong_password()
        user = _create_user_with_password(client, csrf, username="froz3", password=pwd)
        user_id = user["id"]

        # User logs in (replaces admin session)
        login_resp = _user_login(client, "froz3", pwd, ip="10.0.0.32")
        self.assertEqual(login_resp.status_code, 200)

        # Verify session works
        cfg = client.get("/api/user/config", headers={"X-Forwarded-For": "10.0.0.32"})
        self.assertEqual(cfg.status_code, 200, cfg.text)

        # Save user session cookies for later
        user_session = client.cookies.get("mm_session")
        user_csrf_val = client.cookies.get("mm_csrf")

        # Re-login as admin to freeze the user
        client.cookies.clear()
        csrf_admin = _admin_login(client, admin_pwd, ip="10.0.0.1")

        _freeze_user(client, csrf_admin, user_id, ip="10.0.0.1")

        # Restore user cookies and check session is invalidated
        client.cookies.clear()
        client.cookies.set("mm_session", user_session)
        client.cookies.set("mm_csrf", user_csrf_val)

        cfg_after = client.get("/api/user/config", headers={"X-Forwarded-For": "10.0.0.32"})
        self.assertEqual(cfg_after.status_code, 401, cfg_after.text)


# ---------------------------------------------------------------------------
# Tests — End-to-end freeze / unfreeze lifecycle
# ---------------------------------------------------------------------------


class TestEndToEndFreezeUnfreeze(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_full_freeze_lifecycle(self):
        """
        1. Admin creates user U with password P
        2. U logs in -> gets session cookie
        3. U makes authenticated API call -> 200
        4. Admin freezes U
        5. U retries same API call with same session -> 401
        6. U tries to log in again -> 403
        7. Admin unfreezes U
        8. U logs in again -> 200
        9. U's API call works again
        """
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        # Step 1: Admin creates user
        csrf_admin, admin_pwd = _admin_init_and_login(client, ip="10.0.0.1")
        user_pwd = _strong_password()
        user = _create_user_with_password(
            client, csrf_admin, username="lifecycle", password=user_pwd
        )
        user_id = user["id"]

        # Step 2: User logs in (replaces admin session)
        user_ip = "10.0.0.40"
        client.cookies.clear()
        login_resp = _user_login(client, "lifecycle", user_pwd, ip=user_ip)
        self.assertEqual(login_resp.status_code, 200, login_resp.text)

        # Step 3: User makes authenticated call
        cfg = client.get("/api/user/config", headers={"X-Forwarded-For": user_ip})
        self.assertEqual(cfg.status_code, 200, cfg.text)

        # Save user session cookies
        user_session = client.cookies.get("mm_session")
        user_csrf_val = client.cookies.get("mm_csrf")

        # Step 4: Admin freezes user (re-login as admin)
        client.cookies.clear()
        csrf_admin = _admin_login(client, admin_pwd, ip="10.0.0.1")

        freeze_resp = _freeze_user(client, csrf_admin, user_id, reason="testing", ip="10.0.0.1")
        self.assertEqual(freeze_resp.status_code, 200, freeze_resp.text)

        # Step 5: User retries with old session -> 401
        client.cookies.clear()
        client.cookies.set("mm_session", user_session)
        client.cookies.set("mm_csrf", user_csrf_val)

        cfg_after = client.get("/api/user/config", headers={"X-Forwarded-For": user_ip})
        self.assertEqual(cfg_after.status_code, 401, cfg_after.text)

        # Step 6: User tries to log in -> 401 (P1.5: frozen accounts
        # return the same 401 as a wrong password so the response code
        # does not leak that the password was correct).
        client.cookies.clear()
        login_again = _user_login(client, "lifecycle", user_pwd, ip=user_ip)
        self.assertEqual(login_again.status_code, 401, login_again.text)
        self.assertIn("用户名或密码错误", login_again.json().get("detail", ""))

        # Step 7: Admin unfreezes user
        client.cookies.clear()
        csrf_admin = _admin_login(client, admin_pwd, ip="10.0.0.1")
        unfreeze_resp = _unfreeze_user(client, csrf_admin, user_id, ip="10.0.0.1")
        self.assertEqual(unfreeze_resp.status_code, 200, unfreeze_resp.text)

        # Step 8: User logs in again -> 200
        client.cookies.clear()
        login_new = _user_login(client, "lifecycle", user_pwd, ip=user_ip)
        self.assertEqual(login_new.status_code, 200, login_new.text)

        # Step 9: User's API call works again
        cfg_new = client.get("/api/user/config", headers={"X-Forwarded-For": user_ip})
        self.assertEqual(cfg_new.status_code, 200, cfg_new.text)


if __name__ == "__main__":
    unittest.main()
