import asyncio
import importlib
import os
import tempfile
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


def _build_client(*, db_path: str) -> TestClient:
    os.environ["ENV"] = "development"
    os.environ["CORS_ORIGINS"] = "*"
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MINIMAX_API_KEY"] = "fallback_key"
    os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
    os.environ["RATE_LIMIT_PER_HOUR"] = "1000"
    os.environ["CHANNEL_COOLDOWN_SECONDS"] = "60"
    os.environ["CHANNEL_FALLBACK_MAX"] = "1"
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

    importlib.reload(config)
    importlib.reload(security)
    importlib.reload(http_client)
    importlib.reload(proxy_service)
    importlib.reload(channel_service)
    importlib.reload(admin_routes)
    importlib.reload(proxy_routes)
    importlib.reload(auth_routes)
    importlib.reload(user_routes)
    main = importlib.reload(main)

    import backend.database as db

    db.init_db()

    return TestClient(main.app)


def _close_http_client() -> None:
    try:
        import backend.services.http_client as http_client

        asyncio.run(http_client.aclose_async_client())
    except Exception:
        pass


def test_channel_failover_and_cooldown():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    client = _build_client(db_path=tmp.name)

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
    csrf_admin = client.cookies.get("mm_csrf")
    assert csrf_admin

    resp = client.post(
        "/api/admin/users",
        headers={"X-Forwarded-For": "10.0.0.2", "X-CSRF-Token": csrf_admin},
        json={"username": "u1", "quota_5h": 500, "quota_week": 5000},
    )
    assert resp.status_code == 200, resp.text
    api_key = resp.json().get("api_key")
    assert api_key

    resp = client.post(
        "/api/auth/login-api-key",
        headers={"X-Forwarded-For": "10.0.0.3"},
        json={"api_key": api_key},
    )
    assert resp.status_code == 200, resp.text
    csrf_user = client.cookies.get("mm_csrf")
    assert csrf_user

    resp = client.post(
        "/api/user/tokens",
        headers={"X-Forwarded-For": "10.0.0.3", "X-CSRF-Token": csrf_user},
        json={},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json().get("token")
    assert token

    import backend.database as database
    import backend.security as security
    import backend.services.channel_service as channel_service

    security = importlib.reload(security)
    database = importlib.reload(database)
    channel_service = importlib.reload(channel_service)

    enc1 = security.Security.encrypt("k1")
    enc2 = security.Security.encrypt("k2")
    with database.get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO channels (provider, name, base_url, api_key_encrypted, weight, is_active)
            VALUES ('minimax', 'c1', 'https://c1.example', ?, 100, 1)
            """,
            (enc1,),
        )
        cursor.execute(
            """
            INSERT INTO channels (provider, name, base_url, api_key_encrypted, weight, is_active)
            VALUES ('minimax', 'c2', 'https://c2.example', ?, 100, 1)
            """,
            (enc2,),
        )
        # Upgrade the test user to the basic plan so its 60 RPM limit
        # doesn't trip the 20-iteration channel-routing loop. The
        # auto_activate_free_plan default (20 RPM) is correct for
        # production but would dominate this tight test loop.
        cursor.execute(
            "UPDATE users SET plan_id = (SELECT id FROM plans WHERE code = 'basic' LIMIT 1) WHERE username = 'u1'"
        )
        conn.commit()

    async def fake_post_with_retry(
        url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
    ):
        if url.startswith("https://c1.example"):
            return httpx.Response(500, json={"error": "fail"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
        # Send enough requests that weighted-random selection (50/50 for two
        # equal-weight channels) is statistically guaranteed to hit c1 at
        # least once. P(c1 never picked in 20 tries) = 0.5^20 ≈ 1e-6.
        # After the first c1 pick, mark_failed puts it into cooldown and
        # every subsequent request routes to c2. Every request must still
        # succeed — that's the failover contract.
        for _ in range(20):
            resp = client.post(
                "/v1/chat",
                headers={"X-Forwarded-For": "10.0.0.3", "Authorization": f"Bearer {token}"},
                json={"session_id": "s1", "message": "hi", "model": "MiniMax-M1"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json().get("reply") == "ok"

    with database.get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cooldown_until, last_error FROM channels WHERE name = 'c1'")
        row = cursor.fetchone()
        assert row is not None
        assert row["cooldown_until"] is not None, (
            "c1 should be in cooldown after failing at least once"
        )
        assert row["last_error"] is not None

    _close_http_client()
