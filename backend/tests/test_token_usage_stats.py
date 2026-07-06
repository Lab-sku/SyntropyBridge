import asyncio
import importlib
import os
import sqlite3
import sys
import tempfile

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


def test_usage_logs_columns_migrated():
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.close()
    os.environ["DATABASE_PATH"] = tmp.name

    import backend.database as db

    db = importlib.reload(db)
    db.init_db()

    # Reload every service / route module that captured a `get_db`
    # reference at import time, otherwise those modules would still
    # write to the previous test's temp file (which has been deleted
    # by now).
    _reload_db_dependents()

    conn = sqlite3.connect(tmp.name)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(usage_logs)")
    cols = [row[1] for row in cur.fetchall()]
    conn.close()

    assert "token_id" in cols
    assert "channel_id" in cols


def _reload_db_dependents() -> None:
    """Force-reload every backend module that depends on the
    ``backend.database`` module-level ``DATABASE_PATH``.

    Without this, after :func:`test_usage_logs_columns_migrated` reloads
    ``backend.database`` the *next* test in the suite will still see
    the old ``get_db`` / ``get_db_context`` references cached inside
    the service modules, which means inserts and queries from those
    modules land in the *previous* (deleted) temp file instead of the
    new one — manifesting as ``no such column`` and ``database is
    locked`` errors.
    """
    candidates = [
        "backend.services.auth_service",
        "backend.services.usage_service",
        "backend.services.health_service",
        "backend.services.quota_service",
        "backend.services.billing_service",
        "backend.services.proxy_service",
        "backend.services.key_pool",
        "backend.services.model_aggregator",
        "backend.services.lockout",
        "backend.services.api_key_service",
        "backend.services.audit",
        "backend.services.channel_service",
        "backend.services.order_service",
        "backend.services.custom_providers",
        "backend.services.token_service",
        "backend.services.user_service",
        "backend.services.http_client",
        "backend.routes.admin",
        "backend.routes.admin_billing",
        "backend.routes.admin_stats",
        "backend.routes.auth",
        "backend.routes.billing",
        "backend.routes.openai_compat",
        "backend.routes.platform",
        "backend.routes.providers",
        "backend.routes.proxy",
        "backend.routes.user",
        "backend.routes.usage",
        "backend.main",
        "backend.utils.idempotency",
        "backend.utils.db_pool",
    ]
    for name in candidates:
        mod = sys.modules.get(name)
        if mod is None:
            try:
                __import__(name)
            except Exception:
                continue
            mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
        except Exception:
            pass


def test_token_usage_stats_and_admin_tokens_usage():
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
        json={"username": "u_stats_1", "quota_5h": 500, "quota_week": 5000},
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
    token_id = int(resp.json().get("id"))
    token = resp.json().get("token")
    assert token_id
    assert token and token.startswith("mmx_tk_")

    import backend.database as db
    import backend.security as security
    import backend.services.channel_service as channel_service

    db = importlib.reload(db)
    security = importlib.reload(security)
    channel_service = importlib.reload(channel_service)

    enc = security.Security.encrypt("k1")
    with db.get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO channels (provider, name, base_url, api_key_encrypted, weight, is_active)
            VALUES ('minimax', 'c1', 'https://c1.example', ?, 100, 1)
            """,
            (enc,),
        )
        channel_id = int(cursor.lastrowid)

    async def fake_post_with_retry(
        url: str, *, json: dict, headers: dict, retries: int = 2, backoff_base: float = 0.4
    ):
        assert url.startswith("https://c1.example")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )

    from unittest.mock import patch

    with patch("backend.services.proxy_service.post_with_retry", new=fake_post_with_retry):
        resp = client.post(
            "/v1/text/chatcompletion_v2",
            headers={"X-Forwarded-For": "10.0.0.4", "Authorization": f"Bearer {token}"},
            json={
                "model": "MiniMax-M1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200, resp.text

    with db.get_db_context() as conn:
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT token_id, channel_id, total_tokens FROM usage_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert int(row["token_id"]) == token_id
        assert int(row["channel_id"]) == channel_id
        assert int(row["total_tokens"]) == 3

    stats = client.get("/api/user/tokens-stats", headers={"X-Forwarded-For": "10.0.0.5"})
    assert stats.status_code == 200, stats.text
    data = stats.json()
    item = next((x for x in data if int(x.get("id")) == token_id), None)
    assert item
    assert int(item.get("usage_24h")) == 3
    assert int(item.get("usage_7d")) == 3
    assert item.get("last_used_at")

    resp = client.post(
        "/api/admin/login",
        headers={"X-Forwarded-For": "10.0.0.6"},
        json={"username": "admin", "password": strong_pwd},
    )
    assert resp.status_code == 200, resp.text

    admin_stats = client.get(
        "/api/admin/tokens-usage?limit=10", headers={"X-Forwarded-For": "10.0.0.6"}
    )
    assert admin_stats.status_code == 200, admin_stats.text
    top = admin_stats.json()
    item2 = next((x for x in top if int(x.get("token_id")) == token_id), None)
    assert item2
    assert int(item2.get("usage_24h")) == 3
    assert int(item2.get("usage_7d")) == 3

    _close_http_client()


def test_reconciliation_summary_endpoint_counts_anomalies():
    """The /admin/stats/reconciliation-summary endpoint should return
    zero-counts on a fresh DB and reflect orders flagged as
    ``pending_review`` plus ``stripe_recon.orphan`` /
    ``stripe_recon.late_payment`` audit rows."""
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

    # Fresh DB → all zero.
    resp = client.get(
        "/api/admin/stats/reconciliation-summary",
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_review"] == 0
    assert body["orphans"] == 0
    assert body["late_payments"] == 0
    assert body["total"] == 0

    # Seed an order in pending_review + orphan / late_payment audit rows.
    # Insert via raw SQL inside a single context — calling log_action()
    # here would open a nested connection and deadlock on SQLite.
    import backend.database as db

    with db.get_db_context() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO orders (order_no, user_id, amount, credits, status)
            VALUES ('ORD_TEST_RECON', 1, 10.00, 1000, 'pending_review')
            """
        )
        cur.execute(
            """
            INSERT INTO audit_logs (actor_id, actor_type, action, target_type, metadata)
            VALUES (1, 'system', 'stripe_recon.orphan', 'order', '{"order_no":"ORD_ORPHAN"}')
            """
        )
        cur.execute(
            """
            INSERT INTO audit_logs (actor_id, actor_type, action, target_type, metadata)
            VALUES (1, 'system', 'stripe_recon.late_payment', 'order', '{"order_no":"ORD_LATE"}')
            """
        )

    resp = client.get(
        "/api/admin/stats/reconciliation-summary",
        headers={"X-Forwarded-For": "10.0.0.3"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending_review"] == 1
    assert body["orphans"] == 1
    assert body["late_payments"] == 1
    assert body["total"] == 3

    _close_http_client()
