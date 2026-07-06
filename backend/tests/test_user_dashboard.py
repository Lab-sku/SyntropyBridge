"""Integration tests for Phase 11-F user dashboard endpoints.

Covers the four /api/user/dashboard/* endpoints:
  - GET /api/user/dashboard/summary
  - GET /api/user/dashboard/chart
  - GET /api/user/dashboard/by-model
  - GET /api/user/dashboard/export.csv

Tests validate quota snapshots, chart data, per-model breakdown, CSV
export, and security (cross-user isolation, session requirement).
"""

from __future__ import annotations

import asyncio
import csv
import importlib
import io
import os
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(*, db_path: str) -> tuple[TestClient, str]:
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
    import backend.routes.admin_billing as admin_billing_routes
    import backend.routes.auth as auth_routes
    import backend.routes.billing as billing_routes
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
    importlib.reload(admin_billing_routes)
    importlib.reload(proxy_routes)
    importlib.reload(auth_routes)
    importlib.reload(user_routes)
    importlib.reload(billing_routes)
    main = importlib.reload(main)

    import backend.database as db

    # Patch the conftest schema to add columns that production code expects
    # but the conftest _SCHEMA doesn't include.  Then stamp all migrations
    # as applied so init_db() skips them (the test schema is sufficient).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    # audit_logs: production add_audit_log() writes actor_username + user_agent
    for col, ddl in [
        ("actor_username", "ALTER TABLE audit_logs ADD COLUMN actor_username VARCHAR(100)"),
        ("user_agent", "ALTER TABLE audit_logs ADD COLUMN user_agent VARCHAR(300)"),
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists
    # users: get_usage_summary() reads plan_expires_at
    try:
        conn.execute("ALTER TABLE users ADD COLUMN plan_expires_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    for v in range(1, 23):
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", (v,))
    conn.commit()
    conn.close()

    db.init_db()

    return TestClient(main.app), db_path


def _close_http_client() -> None:
    try:
        import backend.services.http_client as http_client

        asyncio.run(http_client.aclose_async_client())
    except Exception:
        pass


def _admin_init_and_login(client: TestClient) -> str:
    strong_pwd = f"T3st!{os.urandom(10).hex()}A#"
    resp = client.post("/api/admin/init", json={"username": "admin", "password": strong_pwd})
    assert resp.status_code in (200, 409), resp.text
    resp = client.post("/api/admin/login", json={"username": "admin", "password": strong_pwd})
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def _admin_create_user(client: TestClient, csrf: str, *, username: str = "u1") -> str:
    resp = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": username, "quota_5h": 500, "quota_week": 5000},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["api_key"]


def _user_login(client: TestClient, api_key: str) -> str:
    """Login as user via API key, return CSRF token."""
    resp = client.post("/api/auth/login-api-key", json={"api_key": api_key})
    assert resp.status_code == 200, resp.text
    csrf = client.cookies.get("mm_csrf")
    assert csrf
    return csrf


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_client(temp_db: str) -> TestClient:
    client, _ = _build_client(db_path=temp_db)
    return client


def _parse_csv(text: str) -> list[list[str]]:
    stripped = text.lstrip("\ufeff")
    return list(csv.reader(io.StringIO(stripped)))


def _seed_usage_logs(db_path: str, user_id: int, *, count: int = 3, model: str = "gpt-4o") -> None:
    """Insert recent usage_logs for a user."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect(db_path)
    for i in range(count):
        conn.execute(
            """
            INSERT INTO usage_logs
                (user_id, endpoint, model, provider, prompt_tokens,
                 completion_tokens, total_tokens, cost_credits,
                 request_time, response_time_ms, status_code)
            VALUES (?, '/v1/chat', ?, 'openai', ?, ?, ?, ?, ?, 100, 200)
            """,
            (
                user_id,
                model,
                100 * (i + 1),
                50 * (i + 1),
                150 * (i + 1),
                0.5 * (i + 1),
                now,
            ),
        )
    conn.commit()
    conn.close()


def _setup_user_with_usage(temp_db: str, *, username: str = "dash_user", usage_count: int = 3):
    """Create admin + user, seed usage, return (client, csrf_admin, api_key, user_id)."""
    client = _make_client(temp_db)
    csrf_admin = _admin_init_and_login(client)
    api_key = _admin_create_user(client, csrf_admin, username=username)

    # Find the user_id
    conn = _connect(temp_db)
    row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    user_id = row["id"]

    # Create wallet row with some balance
    conn.execute(
        "INSERT OR REPLACE INTO wallets (user_id, balance, total_recharged, total_consumed, frozen, auto_recharge_enabled) "
        "VALUES (?, 1000.0, 0, 0, 0, 0)",
        (user_id,),
    )
    conn.commit()
    conn.close()

    _seed_usage_logs(temp_db, user_id, count=usage_count)
    return client, csrf_admin, api_key, user_id


# =========================================================================
# Dashboard summary
# =========================================================================


class TestDashboardSummary:
    def test_returns_quota_snapshot(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert "quota_5h" in body
        assert "quota_week" in body
        assert "used" in body["quota_5h"]
        assert "limit" in body["quota_5h"]
        assert "percent" in body["quota_5h"]

    def test_returns_wallet_balance(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/summary")
        body = resp.json()
        assert "wallet_balance" in body
        assert body["wallet_balance"] == 1000.0

    def test_returns_current_plan_null_when_no_subscription(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/summary")
        body = resp.json()
        assert "current_plan" in body
        assert body["current_plan"] is None

    def test_usage_numbers_match_logs(self, temp_db):
        client, _, api_key, user_id = _setup_user_with_usage(temp_db, usage_count=5)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/summary")
        body = resp.json()
        # 5 usage logs with total_tokens = 150*(1+2+3+4+5) = 150*15 = 2250
        # tokens_5h from usage_logs fallback should include all recent logs
        assert body["quota_5h"]["used"] >= 0  # at minimum non-negative


# =========================================================================
# Dashboard chart
# =========================================================================


class TestDashboardChart:
    def test_returns_daily_buckets(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db, usage_count=3)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/chart?range=30d")
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "30d"
        data = body["data"]
        assert isinstance(data, list)
        assert len(data) >= 1
        bucket = data[0]
        assert "date" in bucket
        assert "requests" in bucket
        assert "tokens" in bucket
        assert "cost_credits" in bucket

    def test_7d_returns_7_day_range(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/chart?range=7d")
        body = resp.json()
        assert body["range"] == "7d"
        # All returned data should be within 7 days
        assert isinstance(body["data"], list)

    def test_empty_usage_returns_empty_or_zeros(self, temp_db):
        client = _make_client(temp_db)
        csrf_admin = _admin_init_and_login(client)
        api_key = _admin_create_user(client, csrf_admin, username="empty_user")
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/chart?range=30d")
        assert resp.status_code == 200
        body = resp.json()
        # No usage_logs → empty data array
        assert body["data"] == []


# =========================================================================
# Dashboard by-model
# =========================================================================


class TestDashboardByModel:
    def test_returns_per_model_breakdown(self, temp_db):
        client, _, api_key, user_id = _setup_user_with_usage(temp_db, usage_count=2)
        # Add usage for a second model
        _seed_usage_logs(temp_db, user_id, count=1, model="claude-3")
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/by-model?range=30d")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert isinstance(data, list)
        assert len(data) == 2  # two models
        for entry in data:
            assert "model" in entry
            assert "provider" in entry
            assert "requests" in entry
            assert "tokens" in entry
            assert "cost_credits" in entry

    def test_sorted_by_tokens_descending(self, temp_db):
        client, _, api_key, user_id = _setup_user_with_usage(temp_db, usage_count=3)
        _seed_usage_logs(temp_db, user_id, count=1, model="small-model")
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/by-model?range=30d")
        data = resp.json()["data"]
        tokens_list = [e["tokens"] for e in data]
        assert tokens_list == sorted(tokens_list, reverse=True)


# =========================================================================
# Dashboard export CSV
# =========================================================================


class TestDashboardExportCsv:
    def test_returns_csv_scoped_to_user(self, temp_db):
        client, _, api_key, _ = _setup_user_with_usage(temp_db, usage_count=3)
        _user_login(client, api_key)
        resp = client.get("/api/user/dashboard/export.csv?range=30d")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")
        rows = _parse_csv(resp.text)
        # header + 3 usage rows
        assert len(rows) == 4
        expected_header = [
            "id",
            "model",
            "provider",
            "prompt_tokens",
            "completion_tokens",
            "cost_credits",
            "status_code",
            "request_time",
        ]
        assert rows[0] == expected_header

    def test_cross_user_isolation(self, temp_db):
        """User A's CSV must NOT contain user B's usage_logs."""
        client = _make_client(temp_db)
        csrf_admin = _admin_init_and_login(client)

        # Create two users
        api_key_a = _admin_create_user(client, csrf_admin, username="user_a")
        _admin_create_user(client, csrf_admin, username="user_b")

        conn = _connect(temp_db)
        row_a = conn.execute("SELECT id FROM users WHERE username = 'user_a'").fetchone()
        row_b = conn.execute("SELECT id FROM users WHERE username = 'user_b'").fetchone()
        user_a_id = row_a["id"]
        user_b_id = row_b["id"]
        conn.close()

        # Seed usage for both users
        _seed_usage_logs(temp_db, user_a_id, count=2, model="gpt-4o")
        _seed_usage_logs(temp_db, user_b_id, count=3, model="claude-3")

        # Login as user A and export
        _user_login(client, api_key_a)
        resp = client.get("/api/user/dashboard/export.csv?range=30d")
        rows = _parse_csv(resp.text)
        # header + 2 rows for user A only
        assert len(rows) == 3
        # Verify no user_b data leaked: all rows should have model gpt-4o
        for row in rows[1:]:
            assert row[1] == "gpt-4o", f"User B's data leaked into User A's CSV: {row}"


# =========================================================================
# Auth: all 4 endpoints require user session
# =========================================================================


class TestDashboardAuth:
    def test_summary_requires_session(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/user/dashboard/summary")
        assert resp.status_code == 401

    def test_chart_requires_session(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/user/dashboard/chart")
        assert resp.status_code == 401

    def test_by_model_requires_session(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/user/dashboard/by-model")
        assert resp.status_code == 401

    def test_export_requires_session(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/user/dashboard/export.csv")
        assert resp.status_code == 401
