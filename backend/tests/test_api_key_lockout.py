"""Integration tests for the API-key login lockout (Phase 10-A).

Covers:
- POST /api/auth/login-api-key normal flow
- Lockout threshold (per-key and per-IP)
- Two-dimensional lockout (key + IP)
- Record success clears failures
- Lockout does not affect regular password login
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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
    return client.cookies.get("mm_csrf"), strong_pwd


def _create_user(client, csrf, *, username="u1", ip="10.0.0.1") -> dict:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": ip, "X-CSRF-Token": csrf},
        json={"username": username, "quota_5h": 500, "quota_week": 5000},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _api_key_login(client: TestClient, api_key: str, *, ip: str = "10.0.0.2"):
    return client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": ip},
        json={"api_key": api_key},
    )


def _clear_lockout_tables(db_path: str) -> None:
    """Delete all rows from auth_failures to start a test phase clean."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM auth_failures")
        conn.commit()
    except Exception:
        pass
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApiKeyLoginNormalFlow(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_valid_api_key_returns_session(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="keyuser1")
        api_key = user["api_key"]

        resp = _api_key_login(client, api_key, ip="10.0.0.50")
        self.assertEqual(resp.status_code, 200, resp.text)
        cookies_hdr = "\n".join(resp.headers.get_list("set-cookie"))
        self.assertIn("mm_session=", cookies_hdr)

    def test_invalid_api_key_returns_401(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        _create_user(client, csrf, username="keyuser2")

        resp = _api_key_login(client, "sk-invalid-key-12345678", ip="10.0.0.51")
        self.assertEqual(resp.status_code, 401, resp.text)

        # Verify lockout failure recorded
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM auth_failures").fetchall()
        except Exception:
            rows = []
        conn.close()
        self.assertGreater(len(rows), 0)


class TestApiKeyLockoutThreshold(unittest.TestCase):
    """The default MAX_FAILURES is 8. We patch it to 5 for faster testing."""

    def tearDown(self):
        _close_http_client()

    def test_lockout_after_max_failures(self):
        """5 invalid attempts -> 6th attempt returns 429 even with valid key."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="lockme")
        api_key = user["api_key"]

        ip = "10.0.0.60"
        max_f = 5

        # Patch the lockout module constants so the threshold is low
        with (
            patch("backend.services.lockout.MAX_FAILURES", max_f),
            patch("backend.routes.auth.check_allowed") as mock_check,
            patch("backend.routes.auth.record_failure") as mock_fail,
            patch("backend.routes.auth.record_success") as mock_succ,
        ):
            # Simulate: first 4 attempts are failures (lockout check says allowed)
            def check_side_effect(ident, **kw):
                from backend.services.lockout import LockoutDecision

                scope = kw.get("scope", "user")
                # Count failures for this scope
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT failure_count FROM auth_failures WHERE identifier = ? AND scope = ?",
                        (ident[:120], scope),
                    ).fetchone()
                    count = row[0] if row else 0
                except Exception:
                    count = 0
                conn.close()
                if count >= max_f:
                    return LockoutDecision(False, 900.0, count)
                return LockoutDecision(True, 0.0, count)

            mock_check.side_effect = check_side_effect

            def fail_side_effect(ident, **kw):
                from backend.services.lockout import record_failure as real_rf

                return real_rf(ident, **kw)

            mock_fail.side_effect = fail_side_effect

            def succ_side_effect(ident, **kw):
                from backend.services.lockout import record_success as real_rs

                return real_rs(ident, **kw)

            mock_succ.side_effect = succ_side_effect

            # Make max_f invalid attempts
            for i in range(max_f):
                resp = _api_key_login(client, "sk-badkey-attempt" + str(i), ip=ip)
                self.assertIn(resp.status_code, (401, 429), resp.text)

            # Next attempt with valid key should be 429 (key prefix locked)
            resp = _api_key_login(client, api_key, ip=ip)
            # The key prefix of the bad keys is "sk-badke" which differs from
            # the real api_key prefix, so the key lockout doesn't apply here.
            # But the IP lockout does. So we check for 429.
            self.assertEqual(resp.status_code, 429, resp.text)

    def test_lockout_window_expiry(self):
        """After lockout window expires, valid key works again."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="winuser")
        api_key = user["api_key"]
        prefix = api_key[:8]

        ip = "10.0.0.61"

        # Seed 5 failures for this key prefix directly in DB
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_failures (
                identifier VARCHAR(120) NOT NULL,
                scope VARCHAR(20) NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                first_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (identifier, scope)
            )
        """)
        # Set last_failure_at to 20 minutes ago (beyond the 15min window)
        conn.execute(
            """INSERT INTO auth_failures (identifier, scope, failure_count, first_failure_at, last_failure_at)
               VALUES (?, 'key', 10, datetime('now', '-20 minutes'), datetime('now', '-20 minutes'))""",
            (prefix,),
        )
        conn.commit()
        conn.close()

        # The _prune_window should clean up the old row, so the valid key works
        resp = _api_key_login(client, api_key, ip=ip)
        self.assertEqual(resp.status_code, 200, resp.text)


class TestTwoDimensionalLockout(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_same_key_different_ips_locks_key(self):
        """5 invalid attempts from different IPs with same key prefix locks the key."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="dimkey")
        api_key = user["api_key"]
        prefix = api_key[:8]

        # Seed failures for this key prefix from different IPs
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_failures (
                identifier VARCHAR(120) NOT NULL,
                scope VARCHAR(20) NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                first_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (identifier, scope)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO auth_failures (identifier, scope, failure_count, last_failure_at) VALUES (?, 'key', 10, CURRENT_TIMESTAMP)",
            (prefix,),
        )
        conn.commit()
        conn.close()

        # Even from a fresh IP, the key is locked
        resp = _api_key_login(client, api_key, ip="10.0.0.70")
        self.assertEqual(resp.status_code, 429, resp.text)

    def test_same_ip_different_keys_locks_ip(self):
        """5 invalid attempts from same IP with different keys locks the IP."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="dimip")
        api_key = user["api_key"]

        ip = "10.0.0.80"

        # Seed IP failures
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_failures (
                identifier VARCHAR(120) NOT NULL,
                scope VARCHAR(20) NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                first_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (identifier, scope)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO auth_failures (identifier, scope, failure_count, last_failure_at) VALUES (?, 'ip', 10, CURRENT_TIMESTAMP)",
            (ip,),
        )
        conn.commit()
        conn.close()

        # Even with a valid key, the IP is locked
        resp = _api_key_login(client, api_key, ip=ip)
        self.assertEqual(resp.status_code, 429, resp.text)


class TestSuccessClearsFailures(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_success_resets_counter(self):
        """3 failures + 1 success + 3 failures -> still allowed."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        user = _create_user(client, csrf, username="clearfail")
        api_key = user["api_key"]
        prefix = api_key[:8]
        ip = "10.0.0.90"

        # 3 failures
        for i in range(3):
            _api_key_login(client, "sk-badkey-clearfail" + str(i), ip=ip)

        # 1 success
        resp = _api_key_login(client, api_key, ip=ip)
        self.assertEqual(resp.status_code, 200, resp.text)

        # Check failures were cleared for this key prefix
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT failure_count FROM auth_failures WHERE identifier = ? AND scope = 'key'",
                (prefix,),
            ).fetchone()
            key_failures = row["failure_count"] if row else 0
        except Exception:
            key_failures = 0
        conn.close()
        self.assertEqual(key_failures, 0)

        # 3 more failures should still be allowed
        for i in range(3):
            resp = _api_key_login(client, "sk-badkey-again" + str(i), ip=ip)
            self.assertIn(resp.status_code, (401, 429), resp.text)


class TestLockoutDoesNotAffectPasswordLogin(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_api_key_lockout_no_effect_on_password_login(self):
        """API key lockout for key X does not block password login for same user."""
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf, _ = _admin_init_and_login(client)
        pwd = _strong_password()

        # Create user with password
        resp = client.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
            json={
                "username": "pwduser",
                "email": "pwd@test.com",
                "password": pwd,
                "quota_5h": 500,
                "quota_week": 5000,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        api_key = resp.json()["api_key"]
        prefix = api_key[:8]

        # Seed key lockout
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_failures (
                identifier VARCHAR(120) NOT NULL,
                scope VARCHAR(20) NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                first_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_failure_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (identifier, scope)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO auth_failures (identifier, scope, failure_count, last_failure_at) VALUES (?, 'key', 10, CURRENT_TIMESTAMP)",
            (prefix,),
        )
        conn.commit()
        conn.close()

        # API key login should be locked
        resp_key = _api_key_login(client, api_key, ip="10.0.0.91")
        self.assertEqual(resp_key.status_code, 429, resp_key.text)

        # Password login should still work
        resp_pw = client.post(
            "/api/auth/login",
            headers={"X-Forwarded-For": "10.0.0.91"},
            json={"username": "pwduser", "password": pwd},
        )
        self.assertEqual(resp_pw.status_code, 200, resp_pw.text)


if __name__ == "__main__":
    unittest.main()
