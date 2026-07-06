import importlib
import os
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient


def _build_client(
    *,
    db_path: str,
    allow_legacy_x_api_key: str | None = None,
    allow_api_key_login: str | None = None,
) -> TestClient:
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["RATE_LIMIT_PER_HOUR"] = "10000"
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MINIMAX_API_KEY"] = "test_upstream_key"
    os.environ.pop("SECRET_KEY", None)
    os.environ.pop("ENCRYPTION_KEY", None)
    os.environ.pop("TRUSTED_PROXIES", None)

    if allow_legacy_x_api_key is None:
        os.environ.pop("ALLOW_LEGACY_X_API_KEY", None)
    else:
        os.environ["ALLOW_LEGACY_X_API_KEY"] = allow_legacy_x_api_key

    if allow_api_key_login is None:
        os.environ.pop("ALLOW_API_KEY_LOGIN", None)
    else:
        os.environ["ALLOW_API_KEY_LOGIN"] = allow_api_key_login

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
    assert resp.status_code in (200, 409), resp.text
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": strong_pwd},
    )
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def _admin_create_user(client: TestClient, csrf_admin: str, *, username: str) -> str:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.2", "X-CSRF-Token": csrf_admin},
        json={"username": username, "quota_5h": 50000, "quota_week": 500000},
    )
    assert resp.status_code == 200, resp.text
    api_key = resp.json().get("api_key")
    assert api_key
    return api_key


def test_legacy_x_api_key_rejected_when_disabled():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    client = _build_client(db_path=tmp.name, allow_legacy_x_api_key="false")

    csrf_admin = _admin_init_and_login(client)
    api_key = _admin_create_user(client, csrf_admin, username="u_legacy_off")

    async def should_not_call_upstream(
        url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
    ):
        raise RuntimeError("should_not_call_upstream")

    with patch("backend.services.proxy_service.post_with_retry", new=should_not_call_upstream):
        denied = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.3", "X-API-Key": api_key},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )

    assert denied.status_code == 403, denied.text
    assert denied.json().get("detail") == "X-API-Key 鉴权已关闭"


def test_login_api_key_rejected_when_disabled():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    client = _build_client(db_path=tmp.name, allow_api_key_login="false")

    csrf_admin = _admin_init_and_login(client)
    api_key = _admin_create_user(client, csrf_admin, username="u_login_key_off")

    denied = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.3"},
        json={"api_key": api_key},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json().get("detail") == "API Key 登录已关闭"
