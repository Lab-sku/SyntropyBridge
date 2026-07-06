"""Integration tests for Phase 11-E CSV export endpoints.

Covers the four admin CSV exports:
  - GET /api/admin/users/export.csv
  - GET /api/admin/audit-logs/export.csv
  - GET /api/admin/orders/export.csv
  - GET /api/admin/wallet-transactions/export.csv

Each test validates HTTP status, Content-Type, UTF-8 BOM, header row,
data rows, and filter query parameters.  CSV bodies are parsed with
``csv.reader`` to confirm structural correctness.
"""

from __future__ import annotations

import asyncio
import csv
import importlib
import io
import os
import sqlite3

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


def _parse_csv(text: str) -> list[list[str]]:
    """Parse CSV text (stripping BOM) into a list of rows."""
    stripped = text.lstrip("\ufeff")
    return list(csv.reader(io.StringIO(stripped)))


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_client(temp_db: str) -> TestClient:
    """Build a TestClient backed by the temp_db fixture path."""
    client, _ = _build_client(db_path=temp_db)
    return client


def _admin_login(client: TestClient) -> str:
    """Init admin + login, return CSRF token."""
    return _admin_init_and_login(client)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_users(db_path: str) -> None:
    """Insert two test users directly into the DB."""
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO users (id, username, email, api_key, quota_5h, quota_week) "
        "VALUES (10, 'alice', 'alice@test.com', 'ak_alice', 500, 5000)"
    )
    conn.execute(
        "INSERT INTO users (id, username, email, api_key, quota_5h, quota_week) "
        "VALUES (11, 'bob', 'bob@test.com', 'ak_bob', 300, 3000)"
    )
    conn.commit()
    conn.close()


def _seed_audit_logs(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO audit_logs (id, actor_id, actor_type, action, target_type, target_id, created_at) "
        "VALUES (1, 1, 'admin', 'user_freeze', 'user', 10, '2026-06-15 10:00:00')"
    )
    conn.execute(
        "INSERT INTO audit_logs (id, actor_id, actor_type, action, target_type, target_id, created_at) "
        "VALUES (2, 1, 'admin', 'user_unfreeze', 'user', 10, '2026-06-16 10:00:00')"
    )
    conn.execute(
        "INSERT INTO audit_logs (id, actor_id, actor_type, action, target_type, target_id, created_at) "
        "VALUES (3, 2, 'admin', 'user_freeze', 'user', 11, '2026-07-01 10:00:00')"
    )
    conn.commit()
    conn.close()


def _seed_orders(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO orders (id, order_no, user_id, amount, credits, status, payment_method, created_at) "
        "VALUES (1, 'ORD-001', 10, 50.0, 5000, 'paid', 'alipay', '2026-06-01 10:00:00')"
    )
    conn.execute(
        "INSERT INTO orders (id, order_no, user_id, amount, credits, status, payment_method, created_at) "
        "VALUES (2, 'ORD-002', 10, 20.0, 2000, 'pending', 'wechat', '2026-06-10 10:00:00')"
    )
    conn.execute(
        "INSERT INTO orders (id, order_no, user_id, amount, credits, status, payment_method, created_at) "
        "VALUES (3, 'ORD-003', 11, 100.0, 10000, 'paid', 'stripe', '2026-07-01 10:00:00')"
    )
    conn.commit()
    conn.close()


def _seed_wallet_transactions(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO wallet_transactions "
        "(id, user_id, type, amount, balance_after, related_type, related_id, note, created_at) "
        "VALUES (1, 10, 'recharge', 5000, 5000, 'order', 1, 'initial', '2026-06-01 10:00:00')"
    )
    conn.execute(
        "INSERT INTO wallet_transactions "
        "(id, user_id, type, amount, balance_after, related_type, related_id, note, created_at) "
        "VALUES (2, 10, 'consume', -50, 4950, 'usage', 100, 'api call', '2026-06-02 10:00:00')"
    )
    conn.execute(
        "INSERT INTO wallet_transactions "
        "(id, user_id, type, amount, balance_after, related_type, related_id, note, created_at) "
        "VALUES (3, 11, 'recharge', 10000, 10000, 'order', 3, 'big order', '2026-07-01 10:00:00')"
    )
    conn.commit()
    conn.close()


# =========================================================================
# Users CSV export
# =========================================================================


class TestAdminUsersExportCsv:
    def test_returns_200_with_csv_content_type(self, temp_db):
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/users/export.csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    def test_starts_with_utf8_bom(self, temp_db):
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/users/export.csv")
        assert resp.text.startswith("\ufeff"), "Missing UTF-8 BOM"

    def test_header_row_contains_expected_columns(self, temp_db):
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/users/export.csv")
        rows = _parse_csv(resp.text)
        assert len(rows) >= 1
        expected = ["id", "username", "email", "is_active", "quota_5h", "quota_week", "created_at"]
        assert rows[0] == expected

    def test_body_contains_seed_users(self, temp_db):
        _seed_users(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/users/export.csv")
        rows = _parse_csv(resp.text)
        # header + 2 users
        assert len(rows) == 3
        usernames = [r[1] for r in rows[1:]]
        assert "alice" in usernames
        assert "bob" in usernames

    def test_csv_parses_cleanly(self, temp_db):
        _seed_users(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/users/export.csv")
        rows = _parse_csv(resp.text)
        for row in rows:
            assert len(row) == 7, f"Row has {len(row)} cols: {row}"


# =========================================================================
# Audit logs CSV export
# =========================================================================


class TestAdminAuditLogsExportCsv:
    def test_returns_audit_logs(self, temp_db):
        _seed_audit_logs(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/audit-logs/export.csv")
        assert resp.status_code == 200
        rows = _parse_csv(resp.text)
        # header + 3 seeded logs + admin init/login logs (>= 4)
        assert len(rows) >= 4

    def test_filter_by_action(self, temp_db):
        _seed_audit_logs(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/audit-logs/export.csv?action=user_freeze")
        rows = _parse_csv(resp.text)
        # header + 2 freeze logs (ids 1 and 3)
        assert len(rows) == 3
        for row in rows[1:]:
            assert row[2] == "user_freeze"

    def test_filter_by_user_id(self, temp_db):
        _seed_audit_logs(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        # actor_id = 2 only appears in log id=3
        resp = client.get("/api/admin/audit-logs/export.csv?user_id=2")
        rows = _parse_csv(resp.text)
        assert len(rows) == 2  # header + 1
        assert rows[1][1] == "2"  # actor_id column

    def test_filter_by_date_range(self, temp_db):
        _seed_audit_logs(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get(
            "/api/admin/audit-logs/export.csv?date_from=2026-06-15&date_to=2026-06-16"
        )
        rows = _parse_csv(resp.text)
        # Only logs from 2026-06-15 and 2026-06-16 (ids 1 and 2)
        assert len(rows) == 3  # header + 2


# =========================================================================
# Orders CSV export
# =========================================================================


class TestAdminOrdersExportCsv:
    # Route ordering fixed in admin_billing.py — CSV endpoint now reachable.
    def test_returns_orders_with_correct_columns(self, temp_db):
        _seed_orders(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/orders/export.csv")
        assert resp.status_code == 200
        rows = _parse_csv(resp.text)
        assert len(rows) == 4  # header + 3 orders
        expected_header = [
            "id",
            "order_no",
            "user_id",
            "amount",
            "credits",
            "status",
            "payment_method",
            "created_at",
            "paid_at",
        ]
        assert rows[0] == expected_header

    # Route ordering fixed in admin_billing.py — CSV endpoint now reachable.
    def test_filter_by_status_pending(self, temp_db):
        _seed_orders(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/orders/export.csv?status=pending")
        rows = _parse_csv(resp.text)
        assert len(rows) == 2  # header + 1 pending order
        assert rows[1][5] == "pending"

    # Route ordering fixed in admin_billing.py — CSV endpoint now reachable.
    def test_filter_by_user_id(self, temp_db):
        _seed_orders(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/orders/export.csv?user_id=11")
        rows = _parse_csv(resp.text)
        assert len(rows) == 2  # header + 1 order for user 11
        assert rows[1][2] == "11"


# =========================================================================
# Wallet transactions CSV export
# =========================================================================


class TestAdminWalletTransactionsExportCsv:
    def test_returns_transactions_with_correct_columns(self, temp_db):
        _seed_wallet_transactions(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/wallet-transactions/export.csv")
        assert resp.status_code == 200
        rows = _parse_csv(resp.text)
        assert len(rows) == 4  # header + 3 transactions
        expected_header = [
            "id",
            "user_id",
            "type",
            "amount",
            "balance_after",
            "related_type",
            "related_id",
            "note",
            "created_at",
        ]
        assert rows[0] == expected_header

    def test_filter_by_type_consume(self, temp_db):
        _seed_wallet_transactions(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/wallet-transactions/export.csv?type=consume")
        rows = _parse_csv(resp.text)
        assert len(rows) == 2  # header + 1 consume transaction
        assert rows[1][2] == "consume"

    def test_filter_by_user_id(self, temp_db):
        _seed_wallet_transactions(temp_db)
        client = _make_client(temp_db)
        _admin_login(client)
        resp = client.get("/api/admin/wallet-transactions/export.csv?user_id=11")
        rows = _parse_csv(resp.text)
        assert len(rows) == 2  # header + 1 transaction for user 11


# =========================================================================
# Security: all 4 endpoints require admin session
# =========================================================================


class TestCsvExportSecurity:
    def test_users_export_requires_admin(self, temp_db):
        client = _make_client(temp_db)
        # Do NOT login as admin
        resp = client.get("/api/admin/users/export.csv")
        assert resp.status_code == 401

    def test_audit_logs_export_requires_admin(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/admin/audit-logs/export.csv")
        assert resp.status_code == 401

    def test_orders_export_requires_admin(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/admin/orders/export.csv")
        assert resp.status_code == 401

    def test_wallet_transactions_export_requires_admin(self, temp_db):
        client = _make_client(temp_db)
        resp = client.get("/api/admin/wallet-transactions/export.csv")
        assert resp.status_code == 401
