import asyncio
import importlib
import os
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient


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


def _admin_init_and_login(client: TestClient) -> str:
    strong_pwd = f"T3st!{os.urandom(10).hex()}A#"
    resp = client.post("/api/admin/init", json={"username": "admin", "password": strong_pwd})
    if resp.status_code not in (200, 409):
        raise AssertionError(resp.text)

    resp = client.post("/api/admin/login", json={"username": "admin", "password": strong_pwd})
    if resp.status_code != 200:
        raise AssertionError(resp.text)

    csrf = client.cookies.get("mm_csrf")
    if not csrf:
        raise AssertionError("missing mm_csrf cookie")
    return csrf


class UserDeleteCascadeTest(unittest.TestCase):
    def tearDown(self):
        _close_http_client()

    def test_admin_delete_user_cascades_dependents(self):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.close()
        client, db_path = _build_client(db_path=tmp.name)

        csrf = _admin_init_and_login(client)
        username = f"u_del_{uuid4().hex[:8]}"

        resp = client.post(
            "/api/admin/users",
            headers={"X-CSRF-Token": csrf},
            json={"username": username, "quota_5h": 5, "quota_week": 10},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        user_id = resp.json().get("id")
        self.assertTrue(user_id)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO usage_logs (user_id, endpoint, model, status_code) VALUES (?, ?, ?, ?)",
            (user_id, "http://localhost", "MiniMax-M1", 200),
        )
        cur.execute(
            "INSERT INTO quota_resets (user_id, reset_type) VALUES (?, ?)",
            (user_id, "5h"),
        )
        cur.execute(
            "INSERT INTO conversations (user_id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (user_id, "default", "user", "hi"),
        )
        cur.execute(
            "INSERT INTO usage_rollups (user_id, bucket_minute, request_count, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, 1, 1, 1, 1, 2),
        )
        conn.commit()
        conn.close()

        resp = client.delete(f"/api/admin/users/{user_id}", headers={"X-CSRF-Token": csrf})
        self.assertEqual(resp.status_code, 200, resp.text)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.cursor()
        self.assertEqual(
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE id = ?", (user_id,)).fetchone()["c"],
            0,
        )
        self.assertEqual(
            cur.execute(
                "SELECT COUNT(*) AS c FROM usage_logs WHERE user_id = ?", (user_id,)
            ).fetchone()["c"],
            0,
        )
        self.assertEqual(
            cur.execute(
                "SELECT COUNT(*) AS c FROM quota_resets WHERE user_id = ?", (user_id,)
            ).fetchone()["c"],
            0,
        )
        self.assertEqual(
            cur.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchone()["c"],
            0,
        )
        self.assertEqual(
            cur.execute(
                "SELECT COUNT(*) AS c FROM usage_rollups WHERE user_id = ?", (user_id,)
            ).fetchone()["c"],
            0,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
