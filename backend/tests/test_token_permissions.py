import importlib
import os
import sqlite3
import tempfile
import unittest

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


def _admin_create_user(client: TestClient, csrf: str, *, username: str = "u1") -> str:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.1", "X-CSRF-Token": csrf},
        json={"username": username, "quota_5h": 500, "quota_week": 5000},
    )
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    api_key = resp.json().get("api_key")
    if not api_key:
        raise AssertionError("missing api_key")
    return api_key


def _user_login_api_key(client: TestClient, api_key: str) -> str:
    resp = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.2"},
        json={"api_key": api_key},
    )
    if resp.status_code != 200:
        raise AssertionError(resp.text)
    csrf = client.cookies.get("mm_csrf")
    if not csrf:
        raise AssertionError("missing mm_csrf cookie")
    return csrf


class TokenPermissionsTest(unittest.TestCase):
    def test_expired_token_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)

        csrf_admin = _admin_init_and_login(client)
        api_key = _admin_create_user(client, csrf_admin, username="u_expired")
        csrf_user = _user_login_api_key(client, api_key)

        resp = client.post("/api/user/tokens", headers={"X-CSRF-Token": csrf_user}, json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        token_id = int(resp.json()["id"])
        token = resp.json()["token"]

        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "UPDATE tokens SET expires_at = ? WHERE id = ?", ("2000-01-01 00:00:00", token_id)
        )
        conn.commit()
        conn.close()

        denied = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.3", "Authorization": f"Bearer {token}"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        self.assertEqual(denied.status_code, 401, denied.text)

    def test_model_not_in_whitelist_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)

        csrf_admin = _admin_init_and_login(client)
        api_key = _admin_create_user(client, csrf_admin, username="u_model")
        csrf_user = _user_login_api_key(client, api_key)

        resp = client.post(
            "/api/user/tokens",
            headers={"X-CSRF-Token": csrf_user},
            json={"allowed_models": ["MiniMax-M1"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]

        denied = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.4", "Authorization": f"Bearer {token}"},
            json={
                "model": "Some-Other-Model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json().get("detail"), "Token 无权限访问该模型")

    def test_ip_not_match_rejected(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client = _build_client(db_path=tmp.name)

        csrf_admin = _admin_init_and_login(client)
        api_key = _admin_create_user(client, csrf_admin, username="u_ip")
        csrf_user = _user_login_api_key(client, api_key)

        resp = client.post(
            "/api/user/tokens",
            headers={"X-CSRF-Token": csrf_user},
            json={"allowed_ips": ["10.0.0.10"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]

        denied = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.11", "Authorization": f"Bearer {token}"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json().get("detail"), "Token 不允许该 IP")


if __name__ == "__main__":
    unittest.main()
