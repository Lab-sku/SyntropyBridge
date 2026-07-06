import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


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


class RegressionFlowsTest(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def _admin_init_and_login(
        self, client: TestClient, *, init_ip: str = "10.0.0.1", login_ip: str | None = None
    ) -> str:
        strong_pwd = f"T3st!{os.urandom(10).hex()}A#"
        if login_ip is None:
            login_ip = init_ip
        headers = {"X-Forwarded-For": init_ip}
        resp = client.post(
            "/api/admin/init", headers=headers, json={"username": "admin", "password": strong_pwd}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = client.post(
            "/api/admin/login",
            headers={"X-Forwarded-For": login_ip},
            json={"username": "admin", "password": strong_pwd},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        cookies_hdr = "\n".join(resp.headers.get_list("set-cookie"))
        self.assertIn("mm_admin_session=", cookies_hdr)
        self.assertIn("HttpOnly", cookies_hdr)
        self.assertIn("mm_csrf=", cookies_hdr)

        csrf = client.cookies.get("mm_csrf")
        self.assertTrue(csrf)
        return csrf

    def _admin_create_user(
        self,
        client: TestClient,
        csrf: str,
        *,
        ip: str = "10.0.0.1",
        username: str = "u1",
        quota_5h: int = 3000,
        quota_week: int = 5000,
    ) -> str:
        resp = client.post(
            "/api/admin/users",
            headers={"X-Forwarded-For": ip, "X-CSRF-Token": csrf},
            json={"username": username, "quota_5h": quota_5h, "quota_week": quota_week},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        api_key = resp.json().get("api_key")
        self.assertTrue(api_key)
        return api_key

    def test_admin_config_is_encrypted_and_masked(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = self._admin_init_and_login(client)

        secret = f"k_{os.urandom(12).hex()}"
        resp = client.post(
            "/api/admin/config",
            headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
            json={"minimax_api_key": secret, "enabled_providers": ["minimax"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value, is_encrypted FROM settings WHERE key = ?", ("minimax_api_key",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(int(row["is_encrypted"]), 1)
        self.assertNotEqual(row["value"], secret)

        resp = client.get("/api/admin/config", headers={"X-Forwarded-For": "10.0.0.1"})
        self.assertEqual(resp.status_code, 200, resp.text)
        masked = resp.json().get("minimax_api_key")
        self.assertTrue(masked.startswith("****") and masked.endswith(secret[-4:]))

    def test_proxy_requires_valid_api_key(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(db_path=tmp.name)

        csrf = self._admin_init_and_login(client)
        api_key = self._admin_create_user(client, csrf)

        resp = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.2", "X-API-Key": "invalid"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        self.assertEqual(resp.status_code, 401, resp.text)
        self.assertTrue(resp.json().get("request_id"))

        async def fake_post_with_retry(
            url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
        ):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.2", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)

    def test_user_token_crud_and_proxy_bearer(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(db_path=tmp.name)

        csrf_admin = self._admin_init_and_login(client)
        api_key = self._admin_create_user(client, csrf_admin, username="u_token_1")

        resp = client.post(
            "/api/auth/login-api-key",
            headers={"X-Forwarded-For": "10.0.0.9"},
            json={"api_key": api_key},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        csrf_user = client.cookies.get("mm_csrf")
        self.assertTrue(csrf_user)

        resp = client.post("/api/user/tokens", headers={"X-CSRF-Token": csrf_user}, json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        token_id = body.get("id")
        token = body.get("token")
        self.assertTrue(token_id)
        self.assertTrue(token and token.startswith("mmx_tk_"))

        resp = client.get("/api/user/tokens")
        self.assertEqual(resp.status_code, 200, resp.text)
        tokens_list = resp.json()
        self.assertTrue(any(int(t.get("id")) == int(token_id) for t in tokens_list))

        async def fake_post_with_retry(
            url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
        ):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.10", "Authorization": f"Bearer {token}"},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)

        resp = client.post(
            f"/api/user/tokens/{token_id}/disable", headers={"X-CSRF-Token": csrf_user}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.11", "Authorization": f"Bearer {token}"},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 401, resp.text)

        resp = client.post(
            f"/api/user/tokens/{token_id}/revoke", headers={"X-CSRF-Token": csrf_user}
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_proxy_api_key_rate_limit(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(
            db_path=tmp.name, rate_limit_per_minute="1", rate_limit_per_hour="1000"
        )

        csrf = self._admin_init_and_login(client, init_ip="10.0.0.3", login_ip="10.0.0.4")
        api_key = self._admin_create_user(client, csrf, ip="10.0.0.5")

        async def fake_post_with_retry(
            url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
        ):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.6", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            resp2 = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.7", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp2.status_code, 429, resp2.text)
            body = resp2.json()
            self.assertEqual(body.get("code"), "RATE_LIMITED")
            self.assertTrue(body.get("request_id"))

    def test_quota_enforced(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(
            db_path=tmp.name, rate_limit_per_minute="100", rate_limit_per_hour="1000"
        )

        csrf = self._admin_init_and_login(client, init_ip="10.0.0.6")
        api_key = self._admin_create_user(
            client, csrf, ip="10.0.0.6", username="q1", quota_5h=1, quota_week=1
        )

        async def fake_post_with_retry(
            url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
        ):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.7", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)

            resp2 = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.7", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp2.status_code, 429, resp2.text)
            body = resp2.json()
            self.assertTrue(body.get("request_id"))

    def test_upstream_error_is_sanitized(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(db_path=tmp.name)

        csrf = self._admin_init_and_login(client, init_ip="10.0.0.8")
        api_key = self._admin_create_user(client, csrf, ip="10.0.0.8")

        async def fake_post_with_retry(
            url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
        ):
            return httpx.Response(500, json={"error": "very_sensitive_upstream_error"})

        with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
            resp = client.post(
                "/v1/text/chatcompletion_v2",
                headers={"X-Forwarded-For": "10.0.0.9", "X-API-Key": api_key},
                json={
                    "model": "MiniMax-M1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
            self.assertEqual(resp.status_code, 502, resp.text)
            body = resp.json()
            self.assertEqual(body.get("detail"), "上游服务错误")
            self.assertNotIn("very_sensitive_upstream_error", resp.text)

    def test_admin_created_user_can_login_via_api_key_and_access_panel(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(db_path=tmp.name)

        csrf = self._admin_init_and_login(client, init_ip="10.0.0.10")
        api_key = self._admin_create_user(client, csrf, ip="10.0.0.10", username="paneluser")

        resp = client.post(
            "/api/auth/login-api-key",
            headers={"X-Forwarded-For": "10.0.0.11"},
            json={"api_key": api_key},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        cookies_hdr = "\n".join(resp.headers.get_list("set-cookie"))
        self.assertIn("mm_session=", cookies_hdr)
        self.assertIn("mm_csrf=", cookies_hdr)

        session = client.get("/api/auth/session", headers={"X-Forwarded-For": "10.0.0.11"})
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json().get("role"), "user")

        stats = client.get("/api/user/stats", headers={"X-Forwarded-For": "10.0.0.11"})
        self.assertEqual(stats.status_code, 200, stats.text)
        self.assertIn("quota_5h", stats.json())

        cfg = client.get("/api/user/config", headers={"X-Forwarded-For": "10.0.0.11"})
        self.assertEqual(cfg.status_code, 200, cfg.text)
        masked_key = cfg.json().get("api_key")
        # C1: endpoint returns masked key (prefix...suffix), never the full secret.
        #     After the api_key_hash migration, the column stores a random placeholder,
        #     so we verify masking format rather than prefix/suffix of the plaintext.
        self.assertNotEqual(masked_key, api_key)
        self.assertIn("...", masked_key)
        self.assertTrue(len(masked_key) > 3)


    def test_user_session_survives_admin_endpoint_401(self):
        """Regression: a regular user hitting an admin endpoint gets 401
        (not 500), and the user session is NOT invalidated.

        The original bug: Account.jsx called ``GET /admin/config`` for all
        users.  The admin route returned 401 because the user had no
        ``mm_admin_session`` cookie.  The frontend's 401 handler then
        expired the user session and redirected to /login — a P0 UX
        regression.  The backend was correct all along; this test locks
        in that contract so nobody "fixes" it into something worse.
        """
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, _db_path = _build_client(db_path=tmp.name)

        csrf_admin = self._admin_init_and_login(client, init_ip="10.0.0.20")
        api_key = self._admin_create_user(
            client, csrf_admin, ip="10.0.0.20", username="survivor"
        )

        # Log in as the regular user (sets mm_session + mm_csrf cookies).
        resp = client.post(
            "/api/auth/login-api-key",
            headers={"X-Forwarded-For": "10.0.0.21"},
            json={"api_key": api_key},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # Sanity: user session is active.
        session = client.get(
            "/api/auth/session", headers={"X-Forwarded-For": "10.0.0.21"}
        )
        self.assertEqual(session.status_code, 200)
        self.assertTrue(session.json().get("authenticated"))
        self.assertEqual(session.json().get("role"), "user")

        # Hit an admin endpoint — should get 401, not 500.
        admin_resp = client.get(
            "/api/admin/config", headers={"X-Forwarded-For": "10.0.0.21"}
        )
        self.assertEqual(admin_resp.status_code, 401, admin_resp.text)

        # Critical: user session must STILL be valid after the 401.
        session2 = client.get(
            "/api/auth/session", headers={"X-Forwarded-For": "10.0.0.21"}
        )
        self.assertEqual(session2.status_code, 200)
        self.assertTrue(session2.json().get("authenticated"))
        self.assertEqual(session2.json().get("role"), "user")

        # User-scoped endpoints must continue to work.
        stats = client.get(
            "/api/user/stats", headers={"X-Forwarded-For": "10.0.0.21"}
        )
        self.assertEqual(stats.status_code, 200, stats.text)


if __name__ == "__main__":
    unittest.main()
