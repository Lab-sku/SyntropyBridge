from __future__ import annotations

"""Unit tests for usage / quota / health services.

These tests run against a temporary SQLite file. See
``conftest.py`` for the fixture that isolates the database.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.services import health_service, quota_service, usage_service


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_user(
    path: str,
    *,
    user_id: int = 1,
    username: str = "alice",
    api_key: str = "ak_test_alice",
    quota_5h: int = 1000,
    quota_week: int = 10000,
    quota_month: int = 100000,
    monthly_budget: float = 0.0,
    is_active: int = 1,
    plan_id: int = None,
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO users (id, username, api_key, quota_5h, quota_week,
                           quota_month, monthly_budget, is_active, plan_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            api_key,
            quota_5h,
            quota_week,
            quota_month,
            monthly_budget,
            is_active,
            plan_id,
        ),
    )
    conn.execute("INSERT INTO wallets (user_id, balance) VALUES (?, ?)", (user_id, 100.0))
    conn.commit()
    conn.close()


def _insert_log(
    path: str,
    user_id: int,
    *,
    request_time: str,
    model: str = "gpt-4o-mini",
    provider: str = "openai",
    total_tokens: int = 100,
    cost_credits: float = 1.0,
    status_code: int = 200,
) -> None:
    conn = _connect(path)
    conn.execute(
        """
        INSERT INTO usage_logs
            (user_id, endpoint, model, provider, prompt_tokens,
             completion_tokens, total_tokens, cost_credits, request_time,
             response_time_ms, status_code)
        VALUES (?, '/v1/chat', ?, ?, 0, 0, ?, ?, ?, 100, ?)
        """,
        (user_id, model, provider, total_tokens, cost_credits, request_time, status_code),
    )
    conn.commit()
    conn.close()


class TestUsageService:
    def setup_method(self, method):
        # ``temp_db`` is provided by conftest.py. The fixture is bound
        # at the function level so we accept it indirectly via pytest's
        # ``request`` mechanism — see the helper wrappers below.
        pass

    def _setup(self, temp_db, **kwargs):
        _insert_user(temp_db, **kwargs)
        return temp_db

    def test_daily_aggregation(self, temp_db):
        self._setup(temp_db)
        base = datetime.now(timezone.utc)
        # 5 distinct days with varying token counts.
        for delta_days, tokens in [
            (0, 100),
            (1, 200),
            (2, 300),
            (3, 400),
            (4, 500),
        ]:
            when = (base - timedelta(days=delta_days)).strftime("%Y-%m-%d 12:00:00")
            _insert_log(temp_db, user_id=1, request_time=when, total_tokens=tokens)

        rows = usage_service.get_user_daily_usage(user_id=1, days=30)
        assert len(rows) == 5
        # Ordered ascending by date.
        assert rows[0]["tokens"] == 500  # 4 days ago
        assert rows[-1]["tokens"] == 100  # today
        # Total tokens in summary should match.
        total = sum(r["tokens"] for r in rows)
        assert total == 100 + 200 + 300 + 400 + 500

    def test_user_summary(self, temp_db):
        self._setup(temp_db)
        now = datetime.now(timezone.utc)
        # One log today
        _insert_log(
            temp_db,
            1,
            request_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            total_tokens=50,
            cost_credits=1.0,
        )
        # One log 3 days ago
        _insert_log(
            temp_db,
            1,
            request_time=(now - timedelta(days=3)).strftime("%Y-%m-%d 12:00:00"),
            total_tokens=80,
            cost_credits=2.0,
        )
        # One log 40 days ago (out of this-month window)
        _insert_log(
            temp_db,
            1,
            request_time=(now - timedelta(days=40)).strftime("%Y-%m-%d 12:00:00"),
            total_tokens=999,
            cost_credits=50.0,
        )

        summary = usage_service.get_user_summary(1)
        assert summary["today"]["tokens"] == 50
        assert summary["today"]["requests"] == 1
        assert summary["this_week"]["tokens"] == 50 + 80  # 3 days ago is in 7d window
        assert summary["this_month"]["tokens"] == 50 + 80
        assert summary["all_time"]["tokens"] == 50 + 80 + 999

    def test_csv_export(self, temp_db):
        self._setup(temp_db)
        for i in range(3):
            _insert_log(
                temp_db,
                1,
                request_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                total_tokens=10 * (i + 1),
            )

        csv_text = usage_service.export_csv(user_id=1, days=30)
        lines = csv_text.strip().splitlines()
        # 1 header + 3 data rows
        assert len(lines) == 4
        assert lines[0].startswith("request_time,user_id,username")
        for line in lines[1:]:
            # Sanity: row should be comma-separated
            assert "," in line


class TestQuotaService:
    def _setup(self, temp_db, **kwargs):
        _insert_user(temp_db, **kwargs)
        return temp_db

    def test_quota_5h_exceeded(self, temp_db):
        # quota_5h = 200; we'll add 250 tokens in last 5h plus 500 old tokens
        self._setup(temp_db, quota_5h=200, quota_week=0, quota_month=0, monthly_budget=0)
        now = datetime.now(timezone.utc)
        # Recent: 250 tokens within 5h (insert first so it has the lowest id)
        _insert_log(
            temp_db,
            1,
            request_time=(now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            total_tokens=250,
        )
        # Old: 500 tokens 2 days ago — must NOT count toward 5h window
        _insert_log(
            temp_db,
            1,
            request_time=(now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
            total_tokens=500,
        )

        result = quota_service.check_user_quota(user_id=1)
        assert result.allowed is False
        assert "5小时" in result.reason

        # Bring the recent row under the quota by updating via its time stamp
        # rather than id (the highest id is the OLD log, not the recent one).
        conn = _connect(temp_db)
        conn.execute("""
            UPDATE usage_logs SET total_tokens = 50
            WHERE request_time > datetime('now', '-5 hours')
        """)
        conn.commit()
        conn.close()

        result2 = quota_service.check_user_quota(user_id=1)
        assert result2.allowed is True

    def test_quota_budget(self, temp_db):
        self._setup(temp_db, quota_5h=0, quota_week=0, quota_month=0, monthly_budget=10.0)
        now = datetime.now(timezone.utc)
        # Spend 12 credits in the last 25 days
        _insert_log(
            temp_db,
            1,
            request_time=(now - timedelta(days=25)).strftime("%Y-%m-%d %H:%M:%S"),
            cost_credits=12.0,
        )

        result = quota_service.check_user_quota(user_id=1)
        assert result.allowed is False
        assert "预算" in result.reason

        # Bring it under the budget
        conn = _connect(temp_db)
        conn.execute("UPDATE usage_logs SET cost_credits = 5.0")
        conn.commit()
        conn.close()

        result2 = quota_service.check_user_quota(user_id=1)
        assert result2.allowed is True


class TestHealthService:
    def test_health_record_request(self, temp_db):
        provider = "test-provider"
        # Three successes
        for _ in range(3):
            health_service.record_request(provider, latency_ms=100, success=True)
        # Two failures
        for _ in range(2):
            health_service.record_request(provider, latency_ms=200, success=False, error_msg="boom")

        health = health_service.get_provider_health(provider)
        assert health["requests_1h"] == 5
        assert health["errors_1h"] == 2
        # 60% success rate
        assert abs(health["success_rate_1h"] - 0.6) < 1e-6
        # p50 and p95 should be populated
        assert health["latency_p50"] > 0
        assert health["latency_p95"] > 0

        # Provider should still be considered "up" — only 2 of 5 errors
        assert health_service.check_provider_up(provider) is True

        # Now drive it over the 50% error threshold
        for _ in range(10):
            health_service.record_request(provider, latency_ms=300, success=False, error_msg="down")
        health = health_service.get_provider_health(provider)
        assert health_service.check_provider_up(provider) is False
        assert health["success_rate_1h"] < 0.5

        # Per P2.4, record_request must NOT touch provider_keys
        # cooldowns — that is key_pool's responsibility. Cooldowning
        # every active key on a single failure starves the pool the
        # moment one upstream errors; clearing cooldowns on a single
        # success un-cooldowns keys that may still be failing. So
        # after a failure, the provider_keys row should still have a
        # NULL cooldown_until and unchanged last_error.
        conn = _connect(temp_db)
        conn.execute(
            "INSERT INTO provider_keys (provider, key_hash, key_prefix) VALUES (?, ?, ?)",
            (provider, "k1", "pk1"),
        )
        conn.commit()
        conn.close()
        health_service.record_request(provider, latency_ms=400, success=False, error_msg="nope")
        conn = _connect(temp_db)
        row = conn.execute(
            "SELECT cooldown_until, last_error FROM provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
        conn.close()
        assert row["cooldown_until"] is None
        assert row["last_error"] is None
