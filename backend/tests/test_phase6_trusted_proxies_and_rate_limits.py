import importlib
import os
import tempfile
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request


def _build_request(*, client_ip: str, x_forwarded_for: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if x_forwarded_for is not None:
        headers.append((b"x-forwarded-for", x_forwarded_for.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (client_ip, 1234),
        "server": ("testserver", 80),
        "extensions": {},
    }
    return Request(scope)


def test_get_client_ip_ignores_xff_when_proxy_not_trusted():
    os.environ["ENV"] = "production"
    os.environ["TRUSTED_PROXIES"] = "2.2.2.0/24"
    import backend.config as config

    importlib.reload(config)
    from backend.database import get_client_ip

    request = _build_request(client_ip="1.1.1.1", x_forwarded_for="9.9.9.9")
    assert get_client_ip(request) == "1.1.1.1"


def test_get_client_ip_uses_xff_when_proxy_trusted():
    os.environ["ENV"] = "production"
    os.environ["TRUSTED_PROXIES"] = "2.2.2.0/24"
    import backend.config as config

    importlib.reload(config)
    from backend.database import get_client_ip

    request = _build_request(client_ip="2.2.2.2", x_forwarded_for="9.9.9.9")
    assert get_client_ip(request) == "9.9.9.9"


def _build_client(
    *, db_path: str, rate_limit_per_minute: str = "1000", rate_limit_per_hour: str = "10000"
) -> TestClient:
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["RATE_LIMIT_PER_MINUTE"] = rate_limit_per_minute
    os.environ["RATE_LIMIT_PER_HOUR"] = rate_limit_per_hour
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MINIMAX_API_KEY"] = "test_upstream_key"
    os.environ.pop("SECRET_KEY", None)
    os.environ.pop("ENCRYPTION_KEY", None)
    os.environ.pop("TRUSTED_PROXIES", None)
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
    assert resp.status_code == 200, resp.text
    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "10.0.0.1"},
        json={"username": "admin", "password": strong_pwd},
    )
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def _admin_create_user(client: TestClient, csrf_admin: str) -> str:
    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.2", "X-CSRF-Token": csrf_admin},
        json={"username": "u1", "quota_5h": 50000, "quota_week": 500000},
    )
    assert resp.status_code == 200, resp.text
    api_key = resp.json().get("api_key")
    assert api_key
    return api_key


def _user_login_api_key(client: TestClient, api_key: str) -> str:
    resp = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.3"},
        json={"api_key": api_key},
    )
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def test_token_rate_limit_override_applies_to_bearer_token():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    client = _build_client(
        db_path=tmp.name, rate_limit_per_minute="1000", rate_limit_per_hour="10000"
    )

    csrf_admin = _admin_init_and_login(client)
    api_key = _admin_create_user(client, csrf_admin)
    csrf_user = _user_login_api_key(client, api_key)

    resp = client.post(
        "/api/user/tokens",
        headers={"X-CSRF-Token": csrf_user},
        json={"rate_limit_per_minute": 1, "rate_limit_per_hour": 1000},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]

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
        ok1 = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.4", "Authorization": f"Bearer {token}"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert ok1.status_code == 200, ok1.text

        limited = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.4", "Authorization": f"Bearer {token}"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert limited.status_code == 429, limited.text
