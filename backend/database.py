import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Any, Callable, Dict, List, Optional, Tuple

# Module-level constant kept for backward compatibility with tests and
# any deployment that patches it directly. Prefer :func:`get_database_path`
# in new code so the env-var override continues to work.
DATABASE_PATH = os.getenv("DATABASE_PATH", "")


def get_database_path() -> str:
    override = os.getenv("DATABASE_PATH")
    if override:
        return override
    if DATABASE_PATH:
        return DATABASE_PATH
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "minimax_proxy.db")


_usage_windows_cache: dict[int, tuple[float, int, int, int]] = {}
_usage_windows_cache_ttl_seconds = 1.0
_last_rollup_cleanup_minute: Optional[int] = None
_rollup_cleanup_lock = threading.Lock()


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA wal_autocheckpoint=10000")
    conn.execute("PRAGMA cache_size=-40000")  # 40 MB
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=536870912")  # 512 MB


def get_db():
    conn = sqlite3.connect(get_database_path(), timeout=30)
    _apply_connection_pragmas(conn)
    return conn


@contextmanager
def get_db_context():
    conn = get_db()
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _migration_1_baseline(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE NOT NULL,
            email VARCHAR(100),
            password_hash VARCHAR(255) NOT NULL,
            api_key VARCHAR(64) UNIQUE NOT NULL,
            quota_5h INTEGER DEFAULT 3000,
            quota_week INTEGER DEFAULT 5000,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
        """
    )

    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if "email" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN email VARCHAR(100)")
    if "password_hash" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")

    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            endpoint VARCHAR(200),
            model VARCHAR(100),
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            response_time_ms INTEGER DEFAULT 0,
            status_code INTEGER,
            ip_address VARCHAR(45),
            error_message TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reset_type VARCHAR(20),
            reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key VARCHAR(100) UNIQUE NOT NULL,
            value TEXT,
            is_encrypted INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id VARCHAR(64) NOT NULL,
            role VARCHAR(10) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id VARCHAR(100) UNIQUE NOT NULL,
            display_name VARCHAR(100),
            provider VARCHAR(20) NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Defensive column additions for the `models` table. Newer code reads
    # `context_length` and `last_synced`; the baseline migration above is
    # kept lean so legacy DBs without these columns keep working.
    cursor.execute("PRAGMA table_info(models)")
    _model_cols = {row[1] for row in cursor.fetchall()}
    if "context_length" not in _model_cols:
        cursor.execute("ALTER TABLE models ADD COLUMN context_length INTEGER DEFAULT 0")
    if "last_synced" not in _model_cols:
        cursor.execute("ALTER TABLE models ADD COLUMN last_synced TIMESTAMP")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier VARCHAR(100) NOT NULL,
            request_count INTEGER DEFAULT 0,
            window_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            limit_type VARCHAR(20) NOT NULL,
            UNIQUE(identifier, limit_type)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            is_active INTEGER DEFAULT 1
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_type VARCHAR(20) NOT NULL,
            actor_id INTEGER,
            actor_username VARCHAR(100),
            action VARCHAR(100) NOT NULL,
            target_type VARCHAR(50),
            target_id VARCHAR(100),
            ip_address VARCHAR(45),
            user_agent VARCHAR(300),
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id VARCHAR(100) PRIMARY KEY,
            role VARCHAR(20) NOT NULL,
            admin_id INTEGER,
            user_id INTEGER,
            username VARCHAR(100),
            email VARCHAR(100),
            csrf VARCHAR(128) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_rate_limits_identifier ON rate_limits(identifier)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_logs_user_time ON usage_logs(user_id, request_time)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_ip ON usage_logs(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_time ON audit_logs(created_at)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_type, actor_id)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")


def _migration_2_usage_rollups(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_rollups (
            user_id INTEGER NOT NULL,
            bucket_minute INTEGER NOT NULL,
            request_count INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, bucket_minute),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_rollups_bucket ON usage_rollups(bucket_minute)"
    )
    cursor.execute(
        """
        INSERT OR IGNORE INTO usage_rollups (
            user_id, bucket_minute, request_count, prompt_tokens, completion_tokens, total_tokens
        )
        SELECT
            user_id,
            CAST(strftime('%s', request_time) / 60 AS INTEGER) AS bucket_minute,
            COUNT(*) AS request_count,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(total_tokens), 0) AS total_tokens
        FROM usage_logs
        WHERE request_time > datetime('now', '-8 days')
        GROUP BY user_id, bucket_minute
        """
    )


def _migration_3_tokens_channels(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name VARCHAR(100),
            token_prefix VARCHAR(32) NOT NULL,
            token_hash VARCHAR(64) UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            revoked_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_user_id ON tokens(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash)")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name VARCHAR(100) NOT NULL,
            provider VARCHAR(20),
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_user_id ON channels(user_id)")


def _migration_4_token_permissions(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(tokens)")
    columns = [row[1] for row in cursor.fetchall()]

    if "expires_at" not in columns:
        cursor.execute("ALTER TABLE tokens ADD COLUMN expires_at TIMESTAMP")
    if "allowed_models" not in columns:
        cursor.execute("ALTER TABLE tokens ADD COLUMN allowed_models TEXT")
    if "allowed_ips" not in columns:
        cursor.execute("ALTER TABLE tokens ADD COLUMN allowed_ips TEXT")


def _migration_5_channels_routing(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(usage_logs)")
    usage_columns = [row[1] for row in cursor.fetchall()]
    if "metadata" not in usage_columns:
        cursor.execute("ALTER TABLE usage_logs ADD COLUMN metadata TEXT")

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channels_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider VARCHAR(20) NOT NULL,
            name VARCHAR(100) NOT NULL,
            base_url VARCHAR(300) NOT NULL,
            api_key_encrypted TEXT NOT NULL,
            weight INTEGER DEFAULT 100,
            is_active INTEGER DEFAULT 1,
            cooldown_until TIMESTAMP,
            last_health_at TIMESTAMP,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
    if cursor.fetchone():
        try:
            cursor.execute("PRAGMA table_info(channels)")
            old_cols = [row[1] for row in cursor.fetchall()]
            if "user_id" in old_cols:
                cursor.execute(
                    """
                    INSERT INTO channels_new (id, provider, name, base_url, api_key_encrypted, weight, is_active, created_at)
                    SELECT id, COALESCE(provider, 'minimax') AS provider, name, '' AS base_url, '' AS api_key_encrypted, 100, is_active, created_at
                    FROM channels
                    """
                )
        except Exception:
            pass
        cursor.execute("DROP TABLE IF EXISTS channels")
    cursor.execute("ALTER TABLE channels_new RENAME TO channels")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_channels_provider_active ON channels(provider, is_active)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_cooldown ON channels(cooldown_until)")


def _migration_6_usage_logs_token_channel(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(usage_logs)")
    usage_columns = [row[1] for row in cursor.fetchall()]
    if "token_id" not in usage_columns:
        cursor.execute("ALTER TABLE usage_logs ADD COLUMN token_id INTEGER")
    if "channel_id" not in usage_columns:
        cursor.execute("ALTER TABLE usage_logs ADD COLUMN channel_id INTEGER")

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_logs_token_time ON usage_logs(token_id, request_time)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_logs_channel_time ON usage_logs(channel_id, request_time)"
    )


def _migration_7_token_rate_limits(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(tokens)")
    columns = [row[1] for row in cursor.fetchall()]

    if "rate_limit_per_minute" not in columns:
        cursor.execute("ALTER TABLE tokens ADD COLUMN rate_limit_per_minute INTEGER")
    if "rate_limit_per_hour" not in columns:
        cursor.execute("ALTER TABLE tokens ADD COLUMN rate_limit_per_hour INTEGER")


def _migration_8_commercial_surfaces(conn: sqlite3.Connection) -> None:
    """v2 commercial surfaces: wallets, plans, pricing, orders, promo/redeem,
    api_keys, provider_keys, audit, notifications, health."""
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS wallets (
            user_id INTEGER PRIMARY KEY,
            balance NUMERIC DEFAULT 0,
            frozen NUMERIC DEFAULT 0,
            total_recharged NUMERIC DEFAULT 0,
            total_consumed NUMERIC DEFAULT 0,
            auto_recharge_enabled INTEGER DEFAULT 0,
            auto_recharge_threshold NUMERIC,
            auto_recharge_amount NUMERIC,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type VARCHAR(20) NOT NULL,
            amount NUMERIC NOT NULL,
            balance_after NUMERIC NOT NULL,
            related_type VARCHAR(20),
            related_id INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(50) NOT NULL,
            code VARCHAR(50) UNIQUE NOT NULL,
            monthly_price NUMERIC DEFAULT 0,
            monthly_credits INTEGER DEFAULT 0,
            discount_rate NUMERIC DEFAULT 1.0,
            max_api_keys INTEGER DEFAULT 1,
            max_concurrent INTEGER DEFAULT 5,
            rate_limit_rpm INTEGER DEFAULT 60,
            rate_limit_tpm INTEGER DEFAULT 100000,
            features TEXT,
            sort_order INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            status VARCHAR(20) DEFAULT 'active',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            credits_used_this_period INTEGER DEFAULT 0,
            auto_renew INTEGER DEFAULT 1,
            cancelled_at TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS model_pricing (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider VARCHAR(50) NOT NULL,
            model_id VARCHAR(200) NOT NULL,
            input_price_per_1k NUMERIC DEFAULT 0,
            output_price_per_1k NUMERIC DEFAULT 0,
            tier VARCHAR(20) DEFAULT 'standard',
            is_active INTEGER DEFAULT 1,
            is_custom INTEGER DEFAULT 0,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by INTEGER,
            UNIQUE(provider, model_id, tier)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no VARCHAR(64) UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            amount NUMERIC NOT NULL,
            credits NUMERIC NOT NULL,
            bonus_credits NUMERIC DEFAULT 0,
            payment_method VARCHAR(20) DEFAULT 'admin_grant',
            status VARCHAR(20) DEFAULT 'pending',
            promo_code VARCHAR(50),
            paid_at TIMESTAMP,
            approved_by INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code VARCHAR(50) UNIQUE NOT NULL,
            type VARCHAR(20) NOT NULL,
            value NUMERIC NOT NULL,
            bonus_credits NUMERIC DEFAULT 0,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            per_user_limit INTEGER DEFAULT 1,
            valid_from TIMESTAMP,
            valid_until TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promo_code_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_code_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            order_id INTEGER,
            credits_granted NUMERIC,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS redeem_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code VARCHAR(64) UNIQUE NOT NULL,
            type VARCHAR(20) NOT NULL,
            value NUMERIC NOT NULL,
            plan_id INTEGER,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            expires_at TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS redeem_code_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            redeem_code_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            credits_granted NUMERIC,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name VARCHAR(100) NOT NULL,
            key_hash VARCHAR(128) UNIQUE NOT NULL,
            key_prefix VARCHAR(20) NOT NULL,
            key_mask VARCHAR(30),
            monthly_token_limit INTEGER,
            monthly_credit_limit NUMERIC,
            allowed_models TEXT,
            denied_models TEXT,
            is_active INTEGER DEFAULT 1,
            last_used_at TIMESTAMP,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS provider_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider VARCHAR(50) NOT NULL,
            key_hash VARCHAR(128) NOT NULL,
            key_prefix VARCHAR(20) NOT NULL,
            label VARCHAR(100),
            weight INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            cooldown_until TIMESTAMP,
            last_error TEXT,
            last_used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_model_access (
            user_id INTEGER NOT NULL,
            model_id VARCHAR(200) NOT NULL,
            access_type VARCHAR(20) DEFAULT 'allow',
            granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            granted_by INTEGER,
            PRIMARY KEY (user_id, model_id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type VARCHAR(30) NOT NULL,
            title VARCHAR(200),
            content TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS provider_health (
            provider VARCHAR(50) PRIMARY KEY,
            status VARCHAR(20) DEFAULT 'unknown',
            last_check TIMESTAMP,
            latency_p50 INTEGER DEFAULT 0,
            latency_p95 INTEGER DEFAULT 0,
            success_rate_1h NUMERIC DEFAULT 1.0,
            requests_1h INTEGER DEFAULT 0,
            errors_1h INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_model_pricing_lookup ON model_pricing(provider, model_id);
        CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id, is_active);
        CREATE INDEX IF NOT EXISTS idx_provider_keys_provider ON provider_keys(provider, is_active);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read);
    """)

    # Seed default plans (idempotent)
    _seed_default_plans(conn)
    _seed_default_pricing(conn)


def _migration_9_custom_providers(conn: sqlite3.Connection) -> None:
    """Add the ``custom_providers`` table.

    Why this migration is required
    ------------------------------
    Migration #1 (baseline) creates the canonical provider/keys tables
    but never created ``custom_providers``. The aggregator
    (``backend.services.model_aggregator``) and the custom-providers
    admin pages both query it unconditionally, so an empty / fresh
    database blows up with ``no such table: custom_providers`` on the
    first ``GET /v1/models`` call. The schema mirrors what
    ``backend.services.custom_providers`` writes via
    ``INSERT INTO custom_providers (...)`` and ``UPDATE``.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS custom_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug VARCHAR(50) UNIQUE NOT NULL,
            display_name VARCHAR(100) NOT NULL,
            api_base VARCHAR(255) NOT NULL,
            api_key TEXT,
            api_keys TEXT,
            notes TEXT,
            is_enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_custom_providers_enabled
            ON custom_providers(is_enabled);
    """)


def _migration_10_usage_logs_cost_credits(conn: sqlite3.Connection) -> None:
    """Add ``cost_credits`` (NUMERIC) to ``usage_logs``.

    Why this migration is required
    ------------------------------
    The billing aggregator (``backend.services.auth_service`` /
    ``quota_service`` / ``usage_service`` / ``admin_stats``) sums
    ``cost_credits`` from ``usage_logs`` to compute monthly spend and
    per-model revenue, but the baseline schema never declared that
    column. That made every call to ``/api/admin/stats/overview``,
    ``/api/admin/stats/trend`` and ``/api/v1/usage`` blow up with
    ``sqlite3.OperationalError: no such column: cost_credits`` and
    500 the whole admin dashboard. This migration adds the column
    with a default of 0 so existing rows stay valid and the new
    write path (``quota_service.record_usage``) can populate it.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(usage_logs)")
    usage_columns = [row[1] for row in cursor.fetchall()]
    if "cost_credits" not in usage_columns:
        cursor.execute("ALTER TABLE usage_logs ADD COLUMN cost_credits NUMERIC DEFAULT 0")


def _migration_11_subscription_requests(conn: sqlite3.Connection) -> None:
    """Create the ``subscription_requests`` table.

    Why this migration is required
    ------------------------------
    The user-facing flow
    (``POST /api/user/subscriptions`` → admin review) and the admin
    pending-list endpoint
    (``GET /api/admin/subscriptions?status=pending``) both read &
    write ``subscription_requests``. The migration #1 baseline never
    declared it, so on any fresh install the very first call to those
    routes blew up with ``no such table: subscription_requests`` and
    the admin sidebar badge threw a 500. This migration creates the
    table with the schema both write paths use.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscription_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            provider VARCHAR(50) NOT NULL,
            model_id VARCHAR(100),
            requested_quota_5h INTEGER,
            requested_quota_week INTEGER,
            note TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            admin_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE INDEX IF NOT EXISTS idx_subscription_requests_status
            ON subscription_requests(status);
        CREATE INDEX IF NOT EXISTS idx_subscription_requests_user
            ON subscription_requests(user_id);
    """)


def _migration_12_usage_logs_provider(conn: sqlite3.Connection) -> None:
    """Add ``provider`` (VARCHAR) to ``usage_logs``.

    Why this migration is required
    ------------------------------
    The platform-wide aggregations in ``admin_stats.stats_top_models``
    and ``admin_stats.stats_by_provider`` group rows by
    ``usage_logs.provider`` to surface per-model and per-provider
    revenue / tokens. The baseline schema, however, only carried
    ``model`` / ``endpoint`` / ``channel_id`` and never declared a
    ``provider`` column, so every call to
    ``GET /api/admin/stats/top-models`` and
    ``GET /api/admin/stats/by-provider`` blew up with
    ``sqlite3.OperationalError: no such column: provider`` and
    returned HTTP 500. The fallback path
    (the SPA marks the card as "暂无数据" after the catch) makes
    this look like a missing-data issue rather than a hard 500.

    The new column defaults to NULL; the upstream proxy layer
    (``quota_service.record_usage``) now writes the resolved
    provider into the row so the aggregations can index it. SQLite
    cannot retroactively populate a column with a non-constant
    expression, so existing rows are left NULL — they contribute
    to the totals but show up under the ``unknown`` bucket in the
    breakdowns, which is the desired behaviour (no fabrication).
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(usage_logs)")
    usage_columns = [row[1] for row in cursor.fetchall()]
    if "provider" not in usage_columns:
        cursor.execute("ALTER TABLE usage_logs ADD COLUMN provider VARCHAR(50)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_logs_provider_time "
        "ON usage_logs(provider, request_time)"
    )


def _migration_13_models_context_length(conn: sqlite3.Connection) -> None:
    """Add ``context_length`` and ``last_synced`` columns to the ``models`` table.

    The upstream ``/v1/models`` catalog returns a context-window size
    for each model; persisting it lets the chat UI render a friendly
    "8k / 128k" badge next to the model name. The baseline migration
    only created the original columns so legacy DBs need a follow-up
    ALTER.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(models)")
    cols = {row[1] for row in cursor.fetchall()}
    if "context_length" not in cols:
        cursor.execute("ALTER TABLE models ADD COLUMN context_length INTEGER DEFAULT 0")
    if "last_synced" not in cols:
        cursor.execute("ALTER TABLE models ADD COLUMN last_synced TIMESTAMP")


def _migration_14_users_plan_columns(conn: sqlite3.Connection) -> None:
    """Add ``plan_id``, ``plan_expires_at``, ``monthly_budget`` to ``users``.

    Why this migration is required
    ------------------------------
    The plan-management surface (``admin_billing.admin_set_user_plan``,
    ``quota_service.check_user_quota``,
    ``billing_service._user_plan``) reads / writes ``users.plan_id``,
    ``users.plan_expires_at`` and ``users.monthly_budget``. The baseline
    migration only declared ``quota_5h`` and ``quota_week``, so on a
    fresh install:

      * ``POST /api/admin/users/{id}/plan`` 500's with
        ``no such column: plan_id``;
      * ``GET /api/user/quota`` 500's with
        ``no such column: monthly_budget``;
      * the public billing route ``POST /api/user/subscription`` can
        never persist a plan_id back to the user row.

    The new columns default to NULL / 0 so existing rows stay valid
    (NULL = "no plan attached, fall back to the free tier"). The unit
    test fixture ``backend/tests/conftest._SCHEMA`` already declares
    these columns, so this migration just closes the gap for
    production deployments.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "plan_id" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN plan_id INTEGER")
    if "plan_expires_at" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN plan_expires_at TIMESTAMP")
    if "monthly_budget" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN monthly_budget NUMERIC DEFAULT 0")
    if "quota_month" not in cols:
        # Quota engine reads quota_month (-30d window) but the baseline
        # never declared it. Mirror the unit-test schema here.
        cursor.execute("ALTER TABLE users ADD COLUMN quota_month INTEGER DEFAULT 0")


def _migration_15_encrypt_custom_provider_keys(conn: sqlite3.Connection) -> None:
    """Encrypt plaintext API keys in the custom_providers table.

    Prior to this migration, custom provider API keys were stored in
    plaintext. This migration encrypts them in-place using the platform's
    Fernet-based Security.encrypt(). Keys that are already encrypted
    (contain no commas and decrypt successfully) are left untouched.
    """
    from backend.security import Security

    cursor = conn.cursor()
    cursor.execute("SELECT id, api_key, api_keys FROM custom_providers")
    rows = cursor.fetchall()
    for row in rows:
        pk = row[0]
        raw_key = row[1] or ""
        raw_keys = row[2] or ""

        # Try to detect if already encrypted (Fernet tokens are base64url-encoded
        # and contain no commas, while plaintext keys often do)
        needs_encrypt_key = bool(raw_key) and not raw_key.startswith("gAAAAA")
        needs_encrypt_keys = bool(raw_keys) and not raw_keys.startswith("gAAAAA")

        if needs_encrypt_key:
            encrypted_key = Security.encrypt(raw_key)
            cursor.execute(
                "UPDATE custom_providers SET api_key = ? WHERE id = ?", (encrypted_key, pk)
            )
        if needs_encrypt_keys:
            encrypted_keys = Security.encrypt(raw_keys)
            cursor.execute(
                "UPDATE custom_providers SET api_keys = ? WHERE id = ?", (encrypted_keys, pk)
            )


def _migration_16_financial_check_constraints(conn: sqlite3.Connection) -> None:
    """Recreate financial tables with CHECK constraints.

    SQLite does not support ``ALTER TABLE ... ADD CONSTRAINT``, so we
    use the standard rebuild approach: create ``_new`` tables with the
    constraints, copy data (sanitising any existing invalid rows), drop
    the old tables, and rename.
    """
    cursor = conn.cursor()

    cursor.execute("BEGIN")

    cursor.execute("UPDATE wallets SET balance = 0 WHERE balance < 0")
    cursor.execute("UPDATE wallets SET frozen = 0 WHERE frozen < 0")
    cursor.execute("DELETE FROM wallet_transactions WHERE amount = 0")
    cursor.execute("UPDATE orders SET amount = 0 WHERE amount < 0")
    cursor.execute("UPDATE orders SET credits = 0 WHERE credits < 0")

    # -- wallets (add CHECK balance >= 0, CHECK frozen >= 0) ---------------
    cursor.execute("""
        CREATE TABLE wallets_new (
            user_id INTEGER PRIMARY KEY,
            balance NUMERIC DEFAULT 0 CHECK (balance >= 0),
            frozen NUMERIC DEFAULT 0 CHECK (frozen >= 0),
            total_recharged NUMERIC DEFAULT 0,
            total_consumed NUMERIC DEFAULT 0,
            auto_recharge_enabled INTEGER DEFAULT 0,
            auto_recharge_threshold NUMERIC,
            auto_recharge_amount NUMERIC,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO wallets_new (
            user_id, balance, frozen, total_recharged, total_consumed,
            auto_recharge_enabled, auto_recharge_threshold,
            auto_recharge_amount, updated_at
        )
        SELECT
            user_id, COALESCE(balance, 0), COALESCE(frozen, 0),
            COALESCE(total_recharged, 0), COALESCE(total_consumed, 0),
            COALESCE(auto_recharge_enabled, 0), auto_recharge_threshold,
            auto_recharge_amount, updated_at
        FROM wallets
    """)
    cursor.execute("DROP TABLE wallets")
    cursor.execute("ALTER TABLE wallets_new RENAME TO wallets")

    # -- orders (add CHECK amount >= 0, CHECK credits >= 0) ----------------
    cursor.execute("""
        CREATE TABLE orders_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no VARCHAR(64) UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            amount NUMERIC NOT NULL CHECK (amount >= 0),
            credits NUMERIC NOT NULL CHECK (credits >= 0),
            bonus_credits NUMERIC DEFAULT 0,
            payment_method VARCHAR(20) DEFAULT 'admin_grant',
            status VARCHAR(20) DEFAULT 'pending',
            promo_code VARCHAR(50),
            paid_at TIMESTAMP,
            approved_by INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO orders_new (
            id, order_no, user_id, amount, credits, bonus_credits,
            payment_method, status, promo_code, paid_at,
            approved_by, note, created_at
        )
        SELECT
            id, order_no, user_id,
            COALESCE(amount, 0), COALESCE(credits, 0),
            COALESCE(bonus_credits, 0), payment_method, status,
            promo_code, paid_at, approved_by, note, created_at
        FROM orders
    """)
    cursor.execute("DROP TABLE orders")
    cursor.execute("ALTER TABLE orders_new RENAME TO orders")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")

    # -- wallet_transactions (add CHECK amount != 0) -----------------------
    cursor.execute("""
        CREATE TABLE wallet_transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type VARCHAR(20) NOT NULL,
            amount NUMERIC NOT NULL CHECK (amount != 0),
            balance_after NUMERIC NOT NULL,
            related_type VARCHAR(20),
            related_id INTEGER,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO wallet_transactions_new (
            id, user_id, type, amount, balance_after,
            related_type, related_id, note, created_at
        )
        SELECT
            id, user_id, type, amount, balance_after,
            related_type, related_id, note, created_at
        FROM wallet_transactions
        WHERE amount != 0
    """)
    cursor.execute("DROP TABLE wallet_transactions")
    cursor.execute("ALTER TABLE wallet_transactions_new RENAME TO wallet_transactions")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_tx_user ON wallet_transactions(user_id, created_at)"
    )

    cursor.execute("COMMIT")


def _migration_17_session_binding_columns(conn: sqlite3.Connection) -> None:
    """Add ``user_agent``, ``ip_address``, ``absolute_expires_at`` to ``sessions``.

    Why this migration is required
    ------------------------------
    ``backend.session.create_session`` now persists the client's user-agent,
    IP address, and an absolute expiry timestamp (wall-clock cap independent
    of the idle TTL) so that session binding and absolute-timeout enforcement
    work correctly. The baseline migration (#1) only declared the original
    columns, so on a production DB that has already been migrated through #16
    these columns are absent and the defensive ``_columns_exist()`` fallback
    in ``session.py`` silently drops the data. This migration closes the gap
    so the binding data is actually persisted.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(sessions)")
    cols = {row[1] for row in cursor.fetchall()}
    if "user_agent" not in cols:
        cursor.execute("ALTER TABLE sessions ADD COLUMN user_agent VARCHAR(512)")
    if "ip_address" not in cols:
        cursor.execute("ALTER TABLE sessions ADD COLUMN ip_address VARCHAR(45)")
    if "absolute_expires_at" not in cols:
        cursor.execute("ALTER TABLE sessions ADD COLUMN absolute_expires_at TIMESTAMP")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_absolute_expires ON sessions(absolute_expires_at)"
    )


def _migration_18_notifications_metadata(conn: sqlite3.Connection) -> None:
    """Add ``metadata`` JSON column to ``notifications`` table.

    The notifications table was created in migration 8 without a
    metadata column.  The notification service needs it to store
    structured data (e.g., order number, related URL) that the
    frontend uses for deep-linking.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(notifications)")
    cols = {row[1] for row in cursor.fetchall()}
    if "metadata" not in cols:
        cursor.execute("ALTER TABLE notifications ADD COLUMN metadata TEXT")


def _migration_20_password_reset_tokens(conn: sqlite3.Connection) -> None:
    """Create the ``password_reset_tokens`` table.

    Stores one-time password reset tokens with an expiry timestamp.
    Replaces the Redis-based reset flow so the platform works without
    a Redis dependency. The ``used_at`` column marks a token as
    consumed (soft-delete) so the same link cannot be replayed.
    """
    cursor = conn.cursor()

    # Idempotent: only create if the table does not exist yet.
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='password_reset_tokens'"
    )
    if cursor.fetchone():
        return

    cursor.execute("""
        CREATE TABLE password_reset_tokens (
            token VARCHAR(64) PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    cursor.execute("CREATE INDEX idx_prt_expires ON password_reset_tokens(expires_at)")
    cursor.execute("CREATE INDEX idx_prt_user ON password_reset_tokens(user_id)")


def _migration_19_payment_gateway(conn: sqlite3.Connection) -> None:
    """Add payment gateway columns to the ``orders`` table.

    Why this migration is required
    ------------------------------
    The payment gateway (``backend.services.payment``) stores the
    provider's checkout session id, the provider name, and the
    provider's transaction reference on each order so that webhook
    callbacks and status polling can look up the correct row. Without
    these columns the ``POST /api/billing/orders/{order_no}/pay``
    endpoint fails with ``no such column: payment_session_id`` and the
    entire online-payment flow is broken.

    The new columns all default to NULL so existing orders (which were
    manually approved by admin) are unaffected.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(orders)")
    cols = {row[1] for row in cursor.fetchall()}
    if "payment_session_id" not in cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN payment_session_id VARCHAR(200)")
    if "payment_provider" not in cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN payment_provider VARCHAR(20)")
    if "payment_reference" not in cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN payment_reference VARCHAR(200)")
    # paid_at already exists from migration 8 baseline — only add if missing.
    if "paid_at" not in cols:
        cursor.execute("ALTER TABLE orders ADD COLUMN paid_at TIMESTAMP")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_session ON orders(payment_session_id)"
    )


def _migration_21_subscription_lifecycle(conn: sqlite3.Connection) -> None:
    """Add ``pending_plan_id`` to ``subscriptions`` for scheduled downgrades.

    The ``auto_renew`` and ``cancelled_at`` columns already exist from
    migration 8. This migration only adds ``pending_plan_id`` which is
    used by the downgrade flow to record the plan the subscription will
    switch to at period end.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(subscriptions)")
    cols = {row[1] for row in cursor.fetchall()}
    if "pending_plan_id" not in cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN pending_plan_id INTEGER")


def _migration_22_query_optimization_indexes(conn: sqlite3.Connection) -> None:
    """Add covering indexes for the hottest read queries.

    The ``usage_logs`` table is the most-read table in the system and is
    queried along several different column combinations that the existing
    indexes (from migrations 1, 6, 12) do not cover.  This migration adds
    composite indexes that let the following hot paths avoid full table
    scans or temp B-trees:

    * ``user_service.get_usage_chart`` -- filtered by ``(user_id, request_time)``
      and grouped by ``date(request_time)``.  Already served by
      ``idx_usage_logs_user_time`` but the *status-filtered* admin export
      path also predicates on ``status_code``.
    * ``user_service.get_usage_by_model`` -- filtered by
      ``(user_id, request_time)`` and grouped by ``model``.
    * ``quota_service.get_quota_snapshot`` -- sums ``cost_credits`` over a
      ``(user_id, request_time)`` window.
    * ``billing_service._bill_latest_usage`` -- filtered by
      ``(user_id, endpoint, model, status_code, request_time)``.
    * Admin CSV exports -- filtered by ``(user_id, status_code, request_time)``.
    * Wallet transaction dedupe -- filtered by
      ``(user_id, type, related_type, related_id)``.
    * Notification listing -- ordered by ``created_at DESC`` per user.

    Each index is created only when the required columns actually exist in
    the target table (``PRAGMA table_info``), so the migration is safe to
    re-run and tolerant of partial schema states.
    """
    cursor = conn.cursor()

    def _table_columns(table: str) -> set[str]:
        try:
            cursor.execute(f"PRAGMA table_info({table})")
            return {row[1] for row in cursor.fetchall()}
        except Exception:
            return set()

    # -- usage_logs indexes --------------------------------------------------
    ul_cols = _table_columns("usage_logs")

    if {"user_id", "status_code", "request_time"}.issubset(ul_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_logs_user_status_time "
            "ON usage_logs(user_id, status_code, request_time)"
        )

    if {"user_id", "model", "request_time"}.issubset(ul_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_logs_user_model_time "
            "ON usage_logs(user_id, model, request_time)"
        )

    if {"user_id", "cost_credits"}.issubset(ul_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_logs_user_cost "
            "ON usage_logs(user_id, cost_credits)"
        )

    if "request_time" in ul_cols:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_time ON usage_logs(request_time)")

    # usage_logs does NOT have type / related_type / related_id columns
    # (those belong to wallet_transactions).  Skip gracefully.
    if {"user_id", "type", "related_type", "related_id"}.issubset(ul_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_logs_user_type_related "
            "ON usage_logs(user_id, type, related_type, related_id)"
        )

    # -- wallet_transactions indexes -----------------------------------------
    wt_cols = _table_columns("wallet_transactions")

    if {"user_id", "created_at"}.issubset(wt_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_created "
            "ON wallet_transactions(user_id, created_at)"
        )

    if {"user_id", "type", "related_type", "related_id"}.issubset(wt_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_type_related "
            "ON wallet_transactions(user_id, type, related_type, related_id)"
        )

    # -- orders indexes ------------------------------------------------------
    ord_cols = _table_columns("orders")

    if {"user_id", "status"}.issubset(ord_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status)"
        )

    # -- notifications indexes -----------------------------------------------
    notif_cols = _table_columns("notifications")

    if {"user_id", "created_at"}.issubset(notif_cols):
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
            "ON notifications(user_id, created_at DESC)"
        )


def _migration_23_conversation_model_column(conn: sqlite3.Connection) -> None:
    """Add model column to conversations table.

    Records which model was used for each conversation turn, enabling
    sidebar model display and per-turn model tracking.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(conversations)")
    cols = {row[1] for row in cursor.fetchall()}
    if "model" not in cols:
        cursor.execute(
            "ALTER TABLE conversations ADD COLUMN model VARCHAR(100) DEFAULT ''"
        )


def _migration_24_conversation_title_column(conn: sqlite3.Connection) -> None:
    """Add title column to conversations table.

    Stores an AI-generated summary title for the conversation session.
    The title is generated once from the first user message and persists.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(conversations)")
    cols = {row[1] for row in cursor.fetchall()}
    if "title" not in cols:
        cursor.execute(
            "ALTER TABLE conversations ADD COLUMN title VARCHAR(100) DEFAULT ''"
        )


def _migration_25_users_api_key_hash(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "api_key_hash" not in cols:
        cursor.execute("ALTER TABLE users ADD COLUMN api_key_hash VARCHAR(64)")
    import hashlib as _hashlib
    rows = cursor.execute(
        "SELECT id, api_key FROM users WHERE api_key IS NOT NULL AND api_key != ''"
    ).fetchall()
    for row in rows:
        digest = _hashlib.sha256(row[1].encode("utf-8")).hexdigest()
        cursor.execute("UPDATE users SET api_key_hash = ? WHERE id = ?", (digest, row[0]))
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash)"
    )


def _migration_27_api_keys_allowed_ips(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(api_keys)")
    cols = {row[1] for row in cursor.fetchall()}
    if "allowed_ips" not in cols:
        cursor.execute("ALTER TABLE api_keys ADD COLUMN allowed_ips TEXT")


def _migration_28_global_freeze_setting(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('global_freeze', 'false')"
    )


def _migration_29_token_reservations(conn: sqlite3.Connection) -> None:
    """Short-lived per-user token reservations.

    The hot-path quota gate (:func:`quota_service.assert_request_allowed`)
    adds each user's active reservation to their already-consumed token
    total before deciding whether a new request fits under their
    5-hour / week / month quotas. This prevents the classic
    double-spend window: two concurrent requests, each seeing
    ``used = 400`` against a ``quota_5h = 500`` cap, both passing the
    check, and together consuming 800 tokens.

    Rows are ephemeral — released explicitly after the upstream
    response returns, or swept by the hourly
    ``purge_expired_reservations`` job if the process crashed mid-flight.
    The ``reserved_until`` column carries the TTL.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_reservations (
            user_id          INTEGER PRIMARY KEY,
            reserved_tokens  INTEGER   NOT NULL DEFAULT 0,
            reserved_until   TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_reservations_until"
        " ON token_reservations(reserved_until)"
    )


def _migration_30_wallet_credits_expiry(conn: sqlite3.Connection) -> None:
    """Per-credit-entry expiration.

    Each credit-side wallet_transaction (bonus / recharge / refund) may
    carry its own ``expires_at`` timestamp. When the daily
    ``sweep_expired_credits`` job finds an un-debited expired row it
    reverses the entry by debiting the wallet and flipping
    ``expiry_debited`` to 1 so the sweep never fires twice.

    Consumes and admin-debits leave ``expires_at`` NULL — they don't
    represent credits the user holds.

    Idempotent: the ALTERs only run when their target column is
    missing, so re-running the migration (and running it against the
    test schema, which already includes the columns) is safe.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(wallet_transactions)")
    existing = {row[1] for row in cursor.fetchall()}
    if "expires_at" not in existing:
        conn.execute(
            "ALTER TABLE wallet_transactions ADD COLUMN expires_at TIMESTAMP"
        )
    if "expiry_debited" not in existing:
        conn.execute(
            "ALTER TABLE wallet_transactions ADD COLUMN expiry_debited INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_tx_expiry"
        " ON wallet_transactions(expires_at, expiry_debited)"
    )


def _migration_31_admin_super_admin(conn: sqlite3.Connection) -> None:
    """Add ``is_super_admin`` column to ``admin_users``.

    The ``/admin/users/{id}/reveal-api-key`` endpoint can extract any
    user's API key, so it's gated behind a super-admin flag to prevent
    junior admins in multi-admin deployments from exfiltrating keys.

    Idempotent: PRAGMA-guarded ALTER, plus the "promote first admin"
    UPDATE uses ``WHERE is_super_admin IS NULL OR is_super_admin = 0``
    so re-running is a no-op once an admin is already flagged.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(admin_users)")
    existing = {row[1] for row in cursor.fetchall()}
    if "is_super_admin" not in existing:
        conn.execute(
            "ALTER TABLE admin_users ADD COLUMN is_super_admin INTEGER NOT NULL DEFAULT 0"
        )
    # Promote the earliest admin (lowest id) so existing deployments
    # retain reveal-api-key capability post-migration. Subsequent
    # admins must be promoted explicitly via UPDATE.
    conn.execute(
        """
        UPDATE admin_users
        SET is_super_admin = 1
        WHERE id = (SELECT MIN(id) FROM admin_users)
          AND (is_super_admin IS NULL OR is_super_admin = 0)
        """
    )


def _migration_32_token_reservations_multirow(conn: sqlite3.Connection) -> None:
    """将 ``token_reservations`` 从单行-per-user 改为多行 ``(user_id, request_id)`` 设计。

    旧设计 ``UNIQUE(user_id)`` 在同一用户并发多个请求时会相互覆盖,
    导致预留统计失真,quota 闸门无法准确阻挡超额请求。新设计:

    * 新增 ``request_id TEXT NOT NULL DEFAULT ''`` 列
    * 新增 ``created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`` 列
    * 主键改为复合 ``(user_id, request_id)``
    * 新增索引 ``(user_id, reserved_until)`` 便于按用户 + TTL 查询
    * ``get_quota_snapshot`` 改为对同用户所有未过期行求 ``SUM``

    SQLite 不能直接 DROP CONSTRAINT,因此采用 rebuild table 范式:
    新建临时表 → 拷贝数据 → DROP 旧表 → RENAME → 重建索引。

    幂等:用 ``PRAGMA table_info(token_reservations)`` 检测是否已含
    ``request_id`` 列,已迁移过的 DB(以及 conftest 的并行 schema)直接跳过。
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(token_reservations)")
    cols = {row[1] for row in cursor.fetchall()}
    if "request_id" in cols:
        # 已经迁移过(可能是 conftest 的并行 schema),无需再 rebuild
        # 仅补建索引以防遗漏
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_reservations_user_until"
            " ON token_reservations(user_id, reserved_until)"
        )
        return

    # 旧表存在但缺 request_id 列 -> rebuild
    conn.execute(
        """
        CREATE TABLE token_reservations_new (
            user_id          TEXT NOT NULL,
            request_id       TEXT NOT NULL DEFAULT '',
            reserved_tokens  INTEGER NOT NULL DEFAULT 0,
            reserved_until   TIMESTAMP NOT NULL,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, request_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO token_reservations_new
            (user_id, request_id, reserved_tokens, reserved_until)
        SELECT user_id, '', reserved_tokens, reserved_until
        FROM token_reservations
        """
    )
    conn.execute("DROP TABLE token_reservations")
    conn.execute("ALTER TABLE token_reservations_new RENAME TO token_reservations")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_token_reservations_user_until"
        " ON token_reservations(user_id, reserved_until)"
    )


def _migration_33_subscriptions_unique_active(conn: sqlite3.Connection) -> None:
    """为 ``subscriptions`` 添加部分索引,优化 ``get_active`` 查询并清理历史冲突。

    原计划用 ``CREATE UNIQUE INDEX ... WHERE status='active'`` 在 DB 层强制
    "每用户至多一条 active 订阅"。但实测发现这会阻断合法的过渡态:
    ``process_expiry()`` 尚未运行时,旧订阅可能仍为 ``status='active'``
    但 ``expires_at`` 已过期,此时新订阅的插入会被 UNIQUE 约束拒绝。

    实际的"唯一 active"不变量已由应用层维护:
      * ``renew()`` 先 ``UPDATE ... SET status='renewed'`` 再 INSERT 新行
      * ``upgrade()`` / ``downgrade()`` 同样先失效旧订阅
      * ``process_expiry()`` 兜底把过期 active 标记为 expired

    因此本迁移退化为普通(非 UNIQUE)部分索引,仅用于加速
    ``WHERE user_id=? AND status='active'`` 查询,并顺手清理历史冲突数据
    (同 user 多条 active 时把 id 较小者标记为 expired)。
    """
    # 1. 检测并修复历史冲突:同 user 多条 active,保留 id 最大者
    conflicts = conn.execute(
        """
        SELECT user_id, COUNT(*) AS cnt
        FROM subscriptions
        WHERE status = 'active'
        GROUP BY user_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in conflicts:
        user_id = row[0]
        conn.execute(
            """
            UPDATE subscriptions
            SET status = 'expired',
                expires_at = COALESCE(expires_at, CURRENT_TIMESTAMP)
            WHERE user_id = ?
              AND status = 'active'
              AND id NOT IN (
                  SELECT MAX(id) FROM subscriptions
                  WHERE user_id = ? AND status = 'active'
              )
            """,
            (user_id, user_id),
        )

    # 2. 创建部分索引(非 UNIQUE,仅用于查询加速)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_subscriptions_active_user
        ON subscriptions(user_id) WHERE status = 'active'
        """
    )


def _migration_26_admin_totp(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(admin_users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "totp_secret" not in cols:
        cursor.execute("ALTER TABLE admin_users ADD COLUMN totp_secret TEXT")
    if "totp_enabled" not in cols:
        cursor.execute("ALTER TABLE admin_users ADD COLUMN totp_enabled INTEGER DEFAULT 0")


def _migration_34_admin_totp_encryption(conn: sqlite3.Connection) -> None:
    """Encrypt every existing ``admin_users.totp_secret`` at rest.

    The TOTP secret is the seed for 2FA code generation — if the DB
    leaks, plaintext secrets let an attacker forge valid codes and
    bypass the second factor entirely. This migration encrypts every
    existing row's secret with ``Security.encrypt`` (Fernet +
    double-base64). Reads/writes in ``routes/admin.py`` decrypt on the
    fly, so the column stays ``TEXT`` and the encryption is
    transparent to the rest of the codebase.

    Idempotent: rows whose ``totp_secret`` already looks like a
    Fernet token (starts with ``gAAAAA`` after optional base64
    unwrapping) are left alone, so re-running is a no-op. ``NULL`` and
    empty values are skipped.
    """
    from backend.security import Security

    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(admin_users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "totp_secret" not in cols:
        return  # table shape not yet ready; migration 26 will add the column

    cursor.execute("SELECT id, totp_secret FROM admin_users WHERE totp_secret IS NOT NULL AND totp_secret != ''")
    rows = cursor.fetchall()
    for row in rows:
        admin_id = row[0]
        stored = row[1]
        # ``Security.decrypt`` returns the original plaintext for
        # non-Fernet inputs, so a non-None, non-empty return that
        # equals the stored value means the row is still plaintext.
        decrypted = Security.decrypt(stored)
        if decrypted is None:
            # Looks like a Fernet token but failed to decrypt — leave
            # it untouched (it may be a value encrypted with a key we
            # no longer have; admin should regenerate 2FA).
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "admin_users.id=%s totp_secret looks encrypted but failed to decrypt; leaving as-is",
                admin_id,
            )
            continue
        if decrypted == stored:
            # Plaintext row — encrypt it now.
            cursor.execute(
                "UPDATE admin_users SET totp_secret = ? WHERE id = ?",
                (Security.encrypt(stored), admin_id),
            )
        # else: already encrypted (decrypted to a different value) — skip.


def _migration_35_user_model_pools(conn: sqlite3.Connection) -> None:
    """Create the ``user_model_pools`` and ``user_model_pool_keys`` tables.

    Each user can register their own pool of OpenAI-compatible upstream
    endpoints (api_base + api_key + model_name) ordered by priority.
    The proxy can route requests through these user-owned pools when
    the user has opted in (e.g. for bring-your-own-key flows).

    Both ``api_base`` and ``api_key`` are stored encrypted at rest
    using ``Security.encrypt`` (Fernet + double base64). Reads
    decrypt on the fly via the helper functions in this module.

    Idempotent: PRAGMA-guarded so re-running on a DB where the tables
    already exist (including the conftest parallel schema) is a no-op.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(user_model_pools)")
    existing = {row[1] for row in cursor.fetchall()}
    if not existing:
        conn.execute(
            """
            CREATE TABLE user_model_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'openai',
                api_base TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                model_name TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                max_tokens INTEGER DEFAULT 0,
                used_tokens INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                cooldown_until TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_model_pools_user"
        " ON user_model_pools(user_id, is_active, priority)"
    )

    cursor.execute("PRAGMA table_info(user_model_pool_keys)")
    existing_keys = {row[1] for row in cursor.fetchall()}
    if not existing_keys:
        conn.execute(
            """
            CREATE TABLE user_model_pool_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                key_prefix TEXT NOT NULL,
                name TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_model_pool_keys_hash"
        " ON user_model_pool_keys(key_hash)"
    )


def _migration_36_notification_cooldowns(conn: sqlite3.Connection) -> None:
    """正式化 ``notification_cooldowns`` 表(之前由 notification_service 懒创建)。

    原先该表通过 ``_ensure_cooldowns_table()`` 在首次使用时懒创建,不在
    迁移体系中,且 conftest 的并行 schema 也没有它。一旦懒创建失败,
    ``should_notify`` 的 except 直接 return True,冷却失效。本迁移将其正式
    纳入 schema_migrations,确保全新部署也有该表。

    列定义与 ``notification_service._ensure_cooldowns_table`` 完全一致
    (``user_id`` / ``type`` / ``last_sent_at`` / ``UNIQUE(user_id, type)``),
    这样运行时兜底的 ``CREATE TABLE IF NOT EXISTS`` 永远是 no-op。

    幂等:用 ``PRAGMA table_info`` 守卫 —— 已存在的表(无论是懒创建还是
    conftest 的并行 schema)直接跳过 CREATE,仅补建索引。
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(notification_cooldowns)")
    existing = {row[1] for row in cursor.fetchall()}
    if not existing:
        conn.execute(
            """
            CREATE TABLE notification_cooldowns (
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                last_sent_at TIMESTAMP NOT NULL,
                UNIQUE(user_id, type)
            )
            """
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_cooldowns_user "
        "ON notification_cooldowns(user_id)"
    )


def _migration_37_users_version(conn: sqlite3.Connection) -> None:
    """Add ``users.version`` for optimistic locking on user updates.

    Without a version column, two concurrent admin edits to the same
    user silently overwrite each other (last-write-wins). The
    ``update_user`` service method now reads the current version,
    includes ``AND version = ?`` in the UPDATE, increments the version
    on success, and raises ``ValueError("用户数据已被其他操作修改")``
    when the rowcount is 0 — letting the route layer return a clean
    409 instead of silently clobbering the other admin's edit.

    Idempotent: PRAGMA table_info guard — conftest's parallel schema
    already declares the column.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cursor.fetchall()}
    if "version" not in cols:
        cursor.execute(
            "ALTER TABLE users ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )


def _migration_38_notifications_user_id_not_null(conn: sqlite3.Connection) -> None:
    """Ensure ``notifications.user_id`` is ``NOT NULL``.

    The baseline schema (migration 8) created ``notifications.user_id``
    without a ``NOT NULL`` constraint even though every code path that
    inserts a row supplies a non-null ``user_id``. A NULL row could never
    be delivered to a user (the listing query filters by ``user_id``), so
    such rows are orphans that silently consume space. Closing the gap at
    the schema layer prevents future regressions.

    SQLite cannot ``ALTER COLUMN`` in place, so the table is rebuilt:
    a ``notifications_new`` copy is created with the corrected column,
    non-null rows are copied across, the old table is dropped, and the
    copy is renamed. Both pre-existing indexes
    (``idx_notifications_user`` from migration 8 and
    ``idx_notifications_user_created`` from migration 22) are recreated
    so query plans are unchanged.

    Idempotent: a ``PRAGMA table_info`` guard skips the rebuild entirely
    when ``user_id`` already carries the ``notnull`` flag — conftest's
    parallel schema declares ``NOT NULL`` directly so the migration is a
    no-op there.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(notifications)")
    cols = cursor.fetchall()
    for col in cols:
        if col[1] == "user_id" and col[3] == 0:  # col[3] = notnull flag
            conn.executescript(
                """
                CREATE TABLE notifications_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    type VARCHAR(30) NOT NULL,
                    title VARCHAR(200),
                    content TEXT,
                    is_read INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                );
                INSERT INTO notifications_new
                    (id, user_id, type, title, content, is_read, created_at, metadata)
                SELECT id, user_id, type, title, content, is_read, created_at, metadata
                FROM notifications
                WHERE user_id IS NOT NULL;
                DROP TABLE notifications;
                ALTER TABLE notifications_new RENAME TO notifications;
                CREATE INDEX IF NOT EXISTS idx_notifications_user
                    ON notifications(user_id, is_read);
                CREATE INDEX IF NOT EXISTS idx_notifications_user_created
                    ON notifications(user_id, created_at DESC);
                """
            )
            break


_MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "baseline", _migration_1_baseline),
    (2, "usage_rollups", _migration_2_usage_rollups),
    (3, "tokens_channels", _migration_3_tokens_channels),
    (4, "token_permissions", _migration_4_token_permissions),
    (5, "channels_routing", _migration_5_channels_routing),
    (6, "usage_logs_token_channel", _migration_6_usage_logs_token_channel),
    (7, "token_rate_limits", _migration_7_token_rate_limits),
    (8, "commercial_surfaces", _migration_8_commercial_surfaces),
    (9, "custom_providers", _migration_9_custom_providers),
    (10, "usage_logs_cost_credits", _migration_10_usage_logs_cost_credits),
    (11, "subscription_requests", _migration_11_subscription_requests),
    (12, "usage_logs_provider", _migration_12_usage_logs_provider),
    (13, "models_context_length", _migration_13_models_context_length),
    (14, "users_plan_columns", _migration_14_users_plan_columns),
    (15, "encrypt_custom_provider_keys", _migration_15_encrypt_custom_provider_keys),
    (16, "financial_check_constraints", _migration_16_financial_check_constraints),
    (17, "session_binding_columns", _migration_17_session_binding_columns),
    (18, "notifications_metadata", _migration_18_notifications_metadata),
    (19, "payment_gateway", _migration_19_payment_gateway),
    (20, "password_reset_tokens", _migration_20_password_reset_tokens),
    (21, "subscription_lifecycle", _migration_21_subscription_lifecycle),
    (22, "query_optimization_indexes", _migration_22_query_optimization_indexes),
    (23, "conversation_model_column", _migration_23_conversation_model_column),
    (24, "conversation_title_column", _migration_24_conversation_title_column),
    (25, "users_api_key_hash", _migration_25_users_api_key_hash),
    (26, "admin_totp", _migration_26_admin_totp),
    (27, "api_keys_allowed_ips", _migration_27_api_keys_allowed_ips),
    (28, "global_freeze_setting", _migration_28_global_freeze_setting),
    (29, "token_reservations", _migration_29_token_reservations),
    (30, "wallet_credits_expiry", _migration_30_wallet_credits_expiry),
    (31, "admin_super_admin", _migration_31_admin_super_admin),
    (32, "token_reservations_multirow", _migration_32_token_reservations_multirow),
    (33, "subscriptions_unique_active", _migration_33_subscriptions_unique_active),
    (34, "admin_totp_encryption", _migration_34_admin_totp_encryption),
    (35, "user_model_pools", _migration_35_user_model_pools),
    (36, "notification_cooldowns", _migration_36_notification_cooldowns),
    (37, "users_version", _migration_37_users_version),
    (38, "notifications_user_id_not_null", _migration_38_notifications_user_id_not_null),
]


def init_db():
    conn = get_db()
    try:
        _ensure_schema_migrations(conn)
        applied = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for version, _name, fn in _MIGRATIONS:
            if version in applied:
                continue
            try:
                fn(conn)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


def add_audit_log(
    *,
    actor_type: str,
    action: str,
    actor_id: Optional[int] = None,
    actor_username: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    try:
        raw_meta = json.dumps(metadata or {}, ensure_ascii=False)
    except Exception:
        raw_meta = "{}"

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO audit_logs (
                actor_type, actor_id, actor_username, action,
                target_type, target_id, ip_address, user_agent, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_type,
                actor_id,
                actor_username,
                action,
                target_type,
                target_id,
                ip_address,
                user_agent,
                raw_meta,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_audit_logs(limit: int = 100, offset: int = 0) -> list[dict]:
    limit = max(1, min(int(limit or 100), 200))
    offset = max(0, int(offset or 0))
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id, actor_type, actor_id, actor_username, action,
                target_type, target_id, ip_address, user_agent, metadata, created_at
            FROM audit_logs
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = cursor.fetchall()

    results: list[dict] = []
    for row in rows:
        raw_meta = row["metadata"] if isinstance(row, sqlite3.Row) else row[9]
        meta: dict = {}
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except Exception:
                meta = {}
        results.append(
            {
                "id": row["id"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "actor_username": row["actor_username"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "ip_address": row["ip_address"],
                "user_agent": row["user_agent"],
                "metadata": meta,
                "created_at": row["created_at"],
            }
        )
    return results


def admin_exists() -> bool:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM admin_users")
        return (cursor.fetchone()["count"] or 0) > 0


def get_admin_by_username(username: str) -> Optional[sqlite3.Row]:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM admin_users WHERE username = ?", (username,))
        return cursor.fetchone()


def create_admin_user(username: str, password_hash: str) -> int:
    with get_db_context() as conn:
        cursor = conn.cursor()
        # First admin is auto-promoted to super-admin so the
        # /admin/users/{id}/reveal-api-key endpoint is callable on a
        # fresh deployment. Subsequent admins land as regular admins
        # (is_super_admin = 0) and must be promoted explicitly.
        cursor.execute("SELECT COUNT(*) FROM admin_users")
        existing = int(cursor.fetchone()[0] or 0)
        is_super_admin = 1 if existing == 0 else 0
        cursor.execute(
            """
            INSERT INTO admin_users
                (username, password_hash, is_active, is_super_admin)
            VALUES (?, ?, 1, ?)
            """,
            (username, password_hash, is_super_admin),
        )
        return cursor.lastrowid


def update_admin_last_login(admin_id: int) -> None:
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE admin_users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (admin_id,)
        )


def get_usage_windows(user_id: int) -> tuple[int, int]:
    now = time.time()
    now_minute = int(now // 60)
    cached = _usage_windows_cache.get(user_id)
    if cached and cached[1] == now_minute and (now - cached[0]) < _usage_windows_cache_ttl_seconds:
        return cached[2], cached[3]

    threshold_5h = now_minute - 5 * 60
    threshold_week = now_minute - 7 * 24 * 60

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN bucket_minute >= ? THEN request_count ELSE 0 END), 0) AS usage_5h,
                COALESCE(SUM(request_count), 0) AS usage_week
            FROM usage_rollups
            WHERE user_id = ? AND bucket_minute >= ?
            """,
            (threshold_5h, user_id, threshold_week),
        )
        row = cursor.fetchone()
        conn.close()
        usage_5h = int(row[0] or 0)
        usage_week = int(row[1] or 0)
    except sqlite3.OperationalError:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) FROM usage_logs
            WHERE user_id = ? AND request_time > datetime('now', '-5 hours')
            """,
            (user_id,),
        )
        usage_5h = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT COUNT(*) FROM usage_logs
            WHERE user_id = ? AND request_time > datetime('now', '-7 days')
            """,
            (user_id,),
        )
        usage_week = int(cursor.fetchone()[0] or 0)
        conn.close()

    _usage_windows_cache[user_id] = (now, now_minute, usage_5h, usage_week)
    return usage_5h, usage_week


def get_usage_5h(user_id: int) -> int:
    usage_5h, _usage_week = get_usage_windows(user_id)
    return usage_5h


def get_usage_week(user_id: int) -> int:
    _usage_5h, usage_week = get_usage_windows(user_id)
    return usage_week


def get_quota_snapshot(user_id: int) -> dict:
    """Read all quota-related data for *user_id* in a single connection.

    Returns a dict with the user's quota limits, wallet balance, current
    usage across the 5-hour / week / month windows (from ``usage_rollups``
    when available, falling back to ``usage_logs``), monthly cost, plan
    details, and current-minute RPM / TPM counters.

    This lets the hot-path quota check run on **one** connection instead
    of the 4-6 separate connections that ``check_user_quota`` opens,
    shrinking the TOCTOU window dramatically.
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # --- user row (quota limits, monthly budget, active flag) --------
        cursor.execute(
            "SELECT is_active, quota_5h, quota_week, quota_month, monthly_budget"
            " FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = cursor.fetchone()
        if not user_row:
            return {"exists": False}

        result: Dict[str, Any] = {
            "exists": True,
            "is_active": bool(user_row["is_active"]),
            "quota_5h": int(user_row["quota_5h"] or 0),
            "quota_week": int(user_row["quota_week"] or 0),
            "quota_month": int(user_row["quota_month"] or 0),
            "monthly_budget": float(user_row["monthly_budget"] or 0),
        }

        # --- wallet balance ---------------------------------------------
        cursor.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
        wallet_row = cursor.fetchone()
        result["balance"] = float(wallet_row["balance"]) if wallet_row else 0.0

        # --- active token reservation (TTL-guarded) --------------------
        # 多行设计(migration 32)下,同一用户可能有多个并发请求各持有一条
        # 未过期预留行。这里对所有未过期行求 SUM,作为该用户的总预留量,
        # 用于闸门判断 (used + reserved) 是否超出 quota。
        try:
            cursor.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0) AS reserved_tokens
                FROM token_reservations
                WHERE user_id = ?
                  AND reserved_until > datetime('now')
                """,
                (user_id,),
            )
            res_row = cursor.fetchone()
            result["reserved_tokens"] = int(res_row["reserved_tokens"] or 0) if res_row else 0
        except sqlite3.OperationalError:
            # Migration 29 not yet applied — behave as if no reservations
            result["reserved_tokens"] = 0

        # --- usage windows (prefer usage_rollups, fallback usage_logs) --
        now = time.time()
        now_minute = int(now // 60)
        threshold_5h = now_minute - 5 * 60
        threshold_week = now_minute - 7 * 24 * 60
        threshold_month = now_minute - 30 * 24 * 60

        try:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN bucket_minute >= ?
                        THEN total_tokens ELSE 0 END), 0) AS tokens_5h,
                    COALESCE(SUM(CASE WHEN bucket_minute >= ?
                        THEN total_tokens ELSE 0 END), 0) AS tokens_week,
                    COALESCE(SUM(total_tokens), 0) AS tokens_month,
                    COALESCE(SUM(CASE WHEN bucket_minute >= ?
                        THEN request_count ELSE 0 END), 0) AS rpm_count
                FROM usage_rollups
                WHERE user_id = ? AND bucket_minute >= ?
                """,
                (threshold_5h, threshold_week, now_minute, user_id, threshold_month),
            )
            rollup = cursor.fetchone()
            result["tokens_5h"] = int(rollup["tokens_5h"] or 0)
            result["tokens_week"] = int(rollup["tokens_week"] or 0)
            result["tokens_month"] = int(rollup["tokens_month"] or 0)
            result["rpm_count"] = int(rollup["rpm_count"] or 0)
        except sqlite3.OperationalError:
            # usage_rollups table not yet created — fall back to raw logs
            cursor.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs"
                " WHERE user_id = ? AND request_time > datetime('now', '-5 hours')",
                (user_id,),
            )
            result["tokens_5h"] = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs"
                " WHERE user_id = ? AND request_time > datetime('now', '-7 days')",
                (user_id,),
            )
            result["tokens_week"] = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs"
                " WHERE user_id = ? AND request_time > datetime('now', '-30 days')",
                (user_id,),
            )
            result["tokens_month"] = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                "SELECT COUNT(*) FROM usage_logs"
                " WHERE user_id = ? AND request_time > datetime('now', '-1 minute')",
                (user_id,),
            )
            result["rpm_count"] = int(cursor.fetchone()[0] or 0)

        # --- monthly cost (from usage_logs.cost_credits) ----------------
        cursor.execute(
            "SELECT COALESCE(SUM(cost_credits), 0) FROM usage_logs"
            " WHERE user_id = ? AND request_time > datetime('now', '-30 days')",
            (user_id,),
        )
        result["monthly_cost"] = float(cursor.fetchone()[0] or 0)

        # --- TPM counter (current minute tokens from usage_logs) --------
        cursor.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs"
            " WHERE user_id = ?"
            " AND request_time > datetime('now', '-1 minute')",
            (user_id,),
        )
        result["tpm_used"] = int(cursor.fetchone()[0] or 0)

        # --- plan (via users.plan_id → plans, fallback subscriptions) ---
        cursor.execute("SELECT plan_id FROM users WHERE id = ?", (user_id,))
        plan_id_row = cursor.fetchone()
        plan_id = plan_id_row["plan_id"] if plan_id_row else None

        if not plan_id:
            cursor.execute(
                "SELECT plan_id FROM subscriptions"
                " WHERE user_id = ? AND status = 'active'"
                " ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
            sub_row = cursor.fetchone()
            plan_id = sub_row["plan_id"] if sub_row else None

        if plan_id:
            cursor.execute(
                "SELECT rate_limit_rpm, rate_limit_tpm, discount_rate FROM plans WHERE id = ?",
                (plan_id,),
            )
            plan_row = cursor.fetchone()
            if plan_row:
                result["plan_id"] = plan_id
                result["plan_rpm"] = int(plan_row["rate_limit_rpm"] or 0)
                result["plan_tpm"] = int(plan_row["rate_limit_tpm"] or 0)
                discount = float(plan_row["discount_rate"] or 1.0)
                result["discount_rate"] = discount if discount > 0 else 1.0
            else:
                result["plan_id"] = None
                result["plan_rpm"] = 0
                result["plan_tpm"] = 0
                result["discount_rate"] = 1.0
        else:
            result["plan_id"] = None
            result["plan_rpm"] = 0
            result["plan_tpm"] = 0
            result["discount_rate"] = 1.0

        return result
    finally:
        conn.close()


def add_usage_log(
    user_id: int,
    endpoint: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_time_ms: int,
    status_code: int,
    token_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[dict] = None,
    provider: Optional[str] = None,
    cost_credits: float = 0.0,
):
    conn = get_db()
    try:
        cursor = conn.cursor()
        total_tokens = prompt_tokens + completion_tokens
        raw_meta: Optional[str] = None
        if metadata is not None:
            try:
                raw_meta = json.dumps(metadata, ensure_ascii=False)
            except Exception:
                raw_meta = None
        cursor.execute(
            """
            INSERT INTO usage_logs (
                user_id, endpoint, model, prompt_tokens, completion_tokens,
                total_tokens, response_time_ms, status_code, token_id, channel_id,
                ip_address, error_message, metadata, provider, cost_credits
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                endpoint,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                response_time_ms,
                status_code,
                token_id,
                channel_id,
                ip_address,
                error_message,
                raw_meta,
                provider,
                cost_credits,
            ),
        )

        now_minute = int(time.time() // 60)
        try:
            cursor.execute(
                """
                INSERT INTO usage_rollups (
                    user_id, bucket_minute, request_count, prompt_tokens, completion_tokens, total_tokens
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id, bucket_minute) DO UPDATE SET
                    request_count = request_count + 1,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    total_tokens = total_tokens + excluded.total_tokens
                """,
                (user_id, now_minute, prompt_tokens, completion_tokens, total_tokens),
            )
        except sqlite3.OperationalError:
            pass

        global _last_rollup_cleanup_minute
        with _rollup_cleanup_lock:
            if _last_rollup_cleanup_minute != now_minute and (now_minute % 60) == 0:
                try:
                    cursor.execute(
                        "DELETE FROM usage_rollups WHERE bucket_minute < ?",
                        (now_minute - 9 * 24 * 60,),
                    )
                    _last_rollup_cleanup_minute = now_minute
                except sqlite3.OperationalError:
                    pass

        conn.commit()
        _usage_windows_cache.pop(user_id, None)
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_usage_log_cost(usage_log_id: int, cost_credits: float) -> None:
    """Backfill ``cost_credits`` on an existing usage_logs row.

    Used when the wallet charge happens *after* the usage log was created
    (e.g. ``openai_compat`` non-streaming path, or streaming reconciliation).
    This keeps the cost visible in dashboards and CSV exports.

    Retries up to 3 times with exponential backoff (100ms, 200ms) on
    ``sqlite3.OperationalError`` indicating a locked database, so a
    transient write-contention spike no longer leaves ``cost_credits``
    pinned at 0 and silently breaks billing reconciliation. Other
    exceptions are logged and swallowed (matching the prior best-effort
    contract) so a logging failure never aborts the request path.
    """
    if usage_log_id is None or cost_credits <= 0:
        return
    import logging as _logging

    _log = _logging.getLogger(__name__)
    for attempt in range(3):
        try:
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE usage_logs SET cost_credits = ? WHERE id = ? AND (cost_credits IS NULL OR cost_credits = 0)",
                    (cost_credits, usage_log_id),
                )
            return
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 2:
                time.sleep(0.1 * (2 ** attempt))  # 100ms, 200ms
                continue
            _log.error(
                "Failed to update usage_log cost after %d attempts: "
                "log_id=%s err=%s",
                attempt + 1,
                usage_log_id,
                exc,
            )
            return
        except Exception as exc:
            _log.error(
                "Failed to update usage_log cost: log_id=%s err=%s",
                usage_log_id,
                exc,
            )
            return


def get_setting(key: str) -> Optional[str]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT value, is_encrypted FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    value = row["value"]
    if row["is_encrypted"] == 1 and value:
        from backend.security import Security

        decrypted = Security.decrypt(value)
        if decrypted is not None:
            value = decrypted
        else:
            # Decryption failed. If the stored value looks like a Fernet
            # token (ciphertext), returning it would send garbage to
            # upstream providers. Log a warning and return empty instead.
            import logging

            _log = logging.getLogger(__name__)
            if value.startswith(("gAAAAA", "Z0FB")):
                _log.warning(
                    "get_setting(%s): decrypt failed and stored value "
                    "looks like ciphertext. Returning empty string to "
                    "prevent sending garbage upstream. "
                    "Re-enter the value in admin UI.",
                    key,
                )
                return ""
            # Not Fernet-shaped — treat as legacy plaintext.
            _log.warning(
                "get_setting(%s): decrypt failed, falling back to raw value",
                key,
            )
    return value


def set_setting(key: str, value: str, encrypt: bool = False) -> None:
    conn = get_db()
    cursor = conn.cursor()
    if encrypt and value:
        from backend.security import Security

        value = Security.encrypt(value)
    cursor.execute(
        """
        INSERT INTO settings (key, value, is_encrypted, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = ?, is_encrypted = ?, updated_at = CURRENT_TIMESTAMP
    """,
        (key, value, 1 if encrypt else 0, value, 1 if encrypt else 0),
    )
    conn.commit()
    conn.close()


def check_rate_limit(
    identifier: str, limit_type: str, limit_count: int, window_seconds: int
) -> tuple[bool, int]:
    """Check and increment the rate limit counter atomically.

    Uses a single UPDATE with a conditional WHERE clause to prevent
    the TOCTOU race condition where multiple concurrent requests could
    all pass the limit check before any writes.

    Returns (allowed, remaining_count).
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Match SQLite's `datetime()` output format exactly so the string
        # comparison in the UPDATE's WHERE clause is byte-for-byte valid.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Gracefully handle missing table (e.g. during startup before init_db)
        try:
            cursor.execute(
                """
                UPDATE rate_limits
                SET request_count = request_count + 1
                WHERE identifier = ? AND limit_type = ?
                  AND datetime(window_start, '+' || ? || ' seconds') > ?
                  AND request_count < ?
                RETURNING request_count, window_start
            """,
                (identifier, limit_type, window_seconds, now, limit_count),
            )
        except sqlite3.OperationalError:
            return True, limit_count - 1
        row = cursor.fetchone()

        if row:
            conn.commit()
            remaining = limit_count - row["request_count"]
            return True, max(int(remaining), 0)

        conn.commit()

        cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            """
            SELECT id, request_count, window_start FROM rate_limits
            WHERE identifier = ? AND limit_type = ?
        """,
            (identifier, limit_type),
        )
        existing = cursor.fetchone()

        if existing:
            window_start_raw = existing["window_start"]
            try:
                window_start = datetime.fromisoformat(window_start_raw)
            except ValueError:
                window_start = datetime.strptime(window_start_raw, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            if window_start.tzinfo is None:
                window_start = window_start.replace(tzinfo=timezone.utc)
            now_dt = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            elapsed = (now_dt - window_start).total_seconds()

            if elapsed >= window_seconds:
                cursor.execute(
                    """
                    UPDATE rate_limits SET request_count = 1, window_start = ?
                    WHERE id = ?
                """,
                    (now, existing["id"]),
                )
                cursor.execute("COMMIT")
                return True, limit_count - 1
            else:
                cursor.execute("ROLLBACK")
                return False, 0
        else:
            cursor.execute(
                """
                INSERT INTO rate_limits (identifier, request_count, window_start, limit_type)
                VALUES (?, 1, ?, ?)
            """,
                (identifier, now, limit_type),
            )
            cursor.execute("COMMIT")
            return True, limit_count - 1
    finally:
        conn.close()


def sanitize_input(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"[<>]", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# v2 commercial surfaces: 套餐 / 钱包 / 定价 helpers
# ---------------------------------------------------------------------------

# 5 个默认订阅套餐。管理员可在后台覆盖（name/price/discount/limits）
DEFAULT_PLANS = [
    {
        "name": "免费版",
        "code": "free",
        "monthly_price": 0,
        "monthly_credits": 10000,
        "discount_rate": 1.0,
        "max_api_keys": 1,
        "max_concurrent": 2,
        "rate_limit_rpm": 20,
        "rate_limit_tpm": 50000,
        "features": '["1 把 API Key","2 并发","5 万 TPM","标准定价"]',
        "sort_order": 0,
    },
    {
        "name": "基础版",
        "code": "basic",
        "monthly_price": 29,
        "monthly_credits": 200000,
        "discount_rate": 0.9,
        "max_api_keys": 3,
        "max_concurrent": 5,
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 200000,
        "features": '["3 把 API Key","5 并发","20 万 TPM","9 折定价"]',
        "sort_order": 1,
    },
    {
        "name": "专业版",
        "code": "pro",
        "monthly_price": 99,
        "monthly_credits": 800000,
        "discount_rate": 0.8,
        "max_api_keys": 10,
        "max_concurrent": 15,
        "rate_limit_rpm": 200,
        "rate_limit_tpm": 1000000,
        "features": '["10 把 API Key","15 并发","100 万 TPM","8 折定价"]',
        "sort_order": 2,
    },
    {
        "name": "团队版",
        "code": "team",
        "monthly_price": 299,
        "monthly_credits": 3000000,
        "discount_rate": 0.7,
        "max_api_keys": 50,
        "max_concurrent": 50,
        "rate_limit_rpm": 600,
        "rate_limit_tpm": 5000000,
        "features": '["50 把 API Key","50 并发","500 万 TPM","7 折定价"]',
        "sort_order": 3,
    },
    {
        "name": "企业版",
        "code": "enterprise",
        "monthly_price": 0,
        "monthly_credits": 0,
        "discount_rate": 0.6,
        "max_api_keys": 999,
        "max_concurrent": 200,
        "rate_limit_rpm": 2000,
        "rate_limit_tpm": 20000000,
        "features": '["不限 API Key","200 并发","2000 万 TPM","6 折定价","SLA 保障"]',
        "sort_order": 4,
    },
]

# 官方原始定价（credits / 1K tokens；1 USD ≈ 700 credits）
DEFAULT_MODEL_PRICING = [
    ("openai", "gpt-4o", 1.75, 7.0, "standard"),
    ("openai", "gpt-4o-mini", 0.105, 0.42, "economy"),
    ("openai", "gpt-4-turbo", 7.0, 21.0, "premium"),
    ("openai", "gpt-3.5-turbo", 0.35, 1.05, "economy"),
    ("openai", "o1", 10.5, 42.0, "premium"),
    ("openai", "o1-mini", 2.1, 8.4, "standard"),
    ("openai", "o3-mini", 0.77, 3.08, "standard"),
    ("anthropic", "claude-3-5-sonnet-20241022", 2.1, 10.5, "standard"),
    ("anthropic", "claude-sonnet-4-20250514", 2.1, 10.5, "standard"),
    ("anthropic", "claude-opus-4-20250514", 10.5, 52.5, "premium"),
    ("anthropic", "claude-3-5-haiku-20241022", 0.56, 2.8, "economy"),
    ("google", "gemini-2.0-flash", 0.07, 0.28, "economy"),
    ("google", "gemini-1.5-pro", 0.875, 3.5, "standard"),
    ("google", "gemini-1.5-flash", 0.0525, 0.21, "economy"),
    ("deepseek", "deepseek-chat", 0.189, 0.77, "economy"),
    ("deepseek", "deepseek-reasoner", 0.385, 1.533, "standard"),
    ("moonshot", "moonshot-v1-8k", 0.84, 0.84, "economy"),
    ("moonshot", "moonshot-v1-128k", 4.2, 4.2, "standard"),
    ("zhipu", "glm-4-plus", 3.5, 3.5, "standard"),
    ("zhipu", "glm-4-air", 0.07, 0.07, "economy"),
    ("aliyun", "qwen-max", 1.4, 4.2, "standard"),
    ("aliyun", "qwen-plus", 0.28, 0.84, "standard"),
    ("aliyun", "qwen-turbo", 0.14, 0.42, "economy"),
    ("doubao", "doubao-pro-32k", 0.056, 0.14, "economy"),
    ("doubao", "doubao-lite-32k", 0.021, 0.042, "economy"),
    ("minimax", "MiniMax-Text-01", 0.84, 0.84, "standard"),
    ("nvidia", "meta/llama-3.1-70b-instruct", 0.0, 0.0, "standard"),
    ("nvidia", "meta/llama-3.3-70b-instruct", 0.0, 0.0, "standard"),
    ("siliconflow", "Qwen/Qwen2.5-7B-Instruct", 0.035, 0.035, "economy"),
    ("siliconflow", "deepseek-ai/DeepSeek-V3", 0.21, 0.84, "standard"),
    ("xai", "grok-2", 2.8, 14.0, "standard"),
    ("xai", "grok-3-mini", 0.21, 0.7, "economy"),
    ("mistral", "mistral-large-latest", 1.75, 5.25, "standard"),
    ("groq", "llama-3.3-70b-versatile", 0.0, 0.0, "standard"),
    ("hunyuan", "hunyuan-pro", 0.42, 0.42, "standard"),
]


def _seed_default_plans(conn) -> None:
    """Insert default subscription plans. Only inserts when no row with
    the same code exists, so admin customizations are never overwritten."""
    cursor = conn.cursor()
    for plan in DEFAULT_PLANS:
        cursor.execute(
            """
            INSERT INTO plans (name, code, monthly_price, monthly_credits, discount_rate,
                               max_api_keys, max_concurrent, rate_limit_rpm, rate_limit_tpm,
                               features, sort_order, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(code) DO NOTHING
        """,
            (
                plan["name"],
                plan["code"],
                plan["monthly_price"],
                plan["monthly_credits"],
                plan["discount_rate"],
                plan["max_api_keys"],
                plan["max_concurrent"],
                plan["rate_limit_rpm"],
                plan["rate_limit_tpm"],
                plan["features"],
                plan["sort_order"],
            ),
        )


def _seed_default_pricing(conn) -> None:
    """Seed official default pricing. Only inserts when no row exists, so admin
    customizations (`is_custom=1`) are never overwritten."""
    cursor = conn.cursor()
    for provider, model_id, in_price, out_price, tier in DEFAULT_MODEL_PRICING:
        cursor.execute(
            """
            INSERT INTO model_pricing
                (provider, model_id, input_price_per_1k, output_price_per_1k, tier, is_active, is_custom)
            VALUES (?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(provider, model_id, tier) DO NOTHING
        """,
            (provider, model_id, in_price, out_price, tier),
        )


def _credits_expire_at() -> Optional[str]:
    """Return the ``expires_at`` timestamp for a freshly-granted credit
    entry, or ``None`` when :attr:`Config.CREDITS_EXPIRE_DAYS` is 0
    (disabled). The ISO-formatted string is what the
    ``wallet_transactions.expires_at`` column expects.
    """
    from backend.config import Config

    days = int(getattr(Config, "CREDITS_EXPIRE_DAYS", 0) or 0)
    if days <= 0:
        return None
    return (
        datetime.now(timezone.utc) + timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")


def grant_credits(
    user_id: int,
    amount: float,
    tx_type: str,
    *,
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
    note: Optional[str] = None,
    expires_at: Optional[str] = "_auto",
    conn: Optional[sqlite3.Connection] = None,
) -> float:
    """Atomically credit ``amount`` to the user's wallet and record the
    matching ``wallet_transactions`` row.

    Parameters
    ----------
    user_id:
        Wallet owner.
    amount:
        Positive credit amount. ``ValueError`` if ``<= 0``.
    tx_type:
        One of ``bonus`` / ``recharge`` / ``refund`` / ``admin_adjust``
        / ``expiry_reversal`` — anything credit-side.
    expires_at:
        ``"_auto"`` (default) computes the horizon from
        :attr:`Config.CREDITS_EXPIRE_DAYS`. ``None`` disables expiry
        for this entry (e.g. compensatory refunds on old orders).
        A literal timestamp string is accepted for callers that want
        to mirror an existing entry's horizon (e.g. refund of an
        old recharge).
    conn:
        Optional external connection. When provided, the operation
        runs within the caller's transaction (no BEGIN/COMMIT/ROLLBACK
        and no close) so it can be composed atomically with other
        writes — e.g. ``auto_activate_free_plan`` embedding the credit
        grant inside the registration transaction. When ``None``
        (default) a fresh connection is opened and the operation runs
        in its own ``BEGIN IMMEDIATE`` transaction.

    Returns the post-credit balance.
    """
    if float(amount or 0) <= 0:
        raise ValueError("grant_credits requires amount > 0")

    if expires_at == "_auto":
        expires_at = _credits_expire_at()

    own_conn = conn is None
    if own_conn:
        conn = get_db()
        conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    try:
        if own_conn:
            cursor.execute("BEGIN IMMEDIATE")

        cursor.execute(
            "INSERT OR IGNORE INTO wallets (user_id, balance, frozen, total_recharged) VALUES (?, 0, 0, 0)",
            (int(user_id),),
        )
        cursor.execute(
            """
            UPDATE wallets
            SET balance = balance + ?,
                total_recharged = total_recharged + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (float(amount), float(amount), int(user_id)),
        )
        cursor.execute(
            "SELECT balance FROM wallets WHERE user_id = ?",
            (int(user_id),),
        )
        bal_row = cursor.fetchone()
        if isinstance(bal_row, tuple):
            new_balance = float(bal_row[0] or 0)
        else:
            new_balance = float(bal_row["balance"])

        cursor.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, type, amount, balance_after,
                 related_type, related_id, note, expires_at, expiry_debited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                int(user_id),
                tx_type,
                float(amount),
                new_balance,
                related_type,
                int(related_id) if related_id is not None else None,
                note,
                expires_at,
            ),
        )
        if own_conn:
            cursor.execute("COMMIT")
        return new_balance
    except Exception:
        if own_conn:
            try:
                cursor.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        if own_conn:
            conn.close()


def get_wallet(user_id: int) -> dict:
    """Read or auto-create a wallet for a user.

    Note: the ``frozen`` column is intentionally omitted from the
    returned dict — it is a legacy zombie field that is always 0 (no
    code path writes a non-zero value). Keeping it in the response
    misled the frontend and admin tooling into thinking the platform
    supports balance freezing. The column itself is retained in the
    schema for backward compatibility.
    """
    # Explicit column list (no ``frozen``) so the dict shape matches
    # what the frontend / admin tooling expects post-cleanup.
    _cols = (
        "user_id, balance, total_recharged, total_consumed, "
        "auto_recharge_enabled, auto_recharge_threshold, "
        "auto_recharge_amount, updated_at"
    )
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"SELECT {_cols} FROM wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        cursor.execute("INSERT INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.commit()
        cursor.execute(f"SELECT {_cols} FROM wallets WHERE user_id = ?", (user_id,))
        return dict(cursor.fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Token-reservation ledger (migration 29 → multi-row in migration 32)
#
# Short-lived per-request reservations used by the hot-path quota gate to
# close the double-spend window between concurrent requests. Each row
# carries a TTL (reserved_until); the sweep in purge_expired_reservations
# reaps rows whose TTL has elapsed (e.g. after a process crash).
#
# Migration 32 把单行-per-user (UNIQUE(user_id)) 改为多行 (user_id, request_id):
# 同一用户并发多个请求时,每条请求获得独立的预留行,quota 闸门对所有未过期
# 行求 SUM,既避免覆盖,也无需锁竞争。
# ---------------------------------------------------------------------------


def reserve_tokens(
    user_id: int,
    tokens: int,
    ttl_seconds: int = 300,
    request_id: Optional[str] = None,
) -> bool:
    """为当前请求插入一条新的预留行。

    ``request_id`` 为 ``None`` 时自动生成 ``secrets.token_urlsafe(8)``。
    多行设计允许同一用户多条并发预留,因此不再使用 ``ON CONFLICT`` 覆盖。

    Returns ``True`` on success. Returns ``False`` if the table has not
    been created yet (migration 29 not applied) — callers treat this as
    a soft failure and proceed without reservation, preserving
    backwards-compatible behaviour on un-migrated DBs.
    """
    if int(tokens or 0) <= 0:
        return False
    rid = request_id or secrets.token_urlsafe(8)
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO token_reservations
                    (user_id, request_id, reserved_tokens, reserved_until)
                VALUES (?, ?, ?, datetime('now', '+' || ? || ' seconds'))
                """,
                (str(int(user_id)), rid, int(tokens), str(int(ttl_seconds))),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError:
            return False
    finally:
        conn.close()


def get_active_reservation(user_id: int) -> Optional[Dict[str, Any]]:
    """返回该用户所有未过期预留的总和与最新过期时间。

    多行设计下同一用户可能有多条未过期行,这里聚合为单条诊断 dict:
    ``reserved_tokens`` 为 SUM,``reserved_until`` 为 MAX(最近过期时间)。
    若无任何活动行,返回 ``None``。
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(reserved_tokens), 0) AS reserved_tokens,
                    MAX(reserved_until)              AS reserved_until
                FROM token_reservations
                WHERE user_id = ?
                  AND reserved_until > datetime('now')
                """,
                (str(int(user_id)),),
            )
            row = cursor.fetchone()
            if row is None or int(row["reserved_tokens"] or 0) == 0:
                return None
            return dict(row)
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()


def release_reservation(user_id: int, request_id: Optional[str] = None) -> None:
    """删除该用户(可选:特定 request_id)的预留行。

    * ``request_id`` 为 ``None`` 时删除该用户的所有预留行(向后兼容旧调用)。
    * 指定 ``request_id`` 时仅删除对应行,不影响其他并发请求的预留。

    Idempotent and table-missing safe.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            if request_id is None:
                cursor.execute(
                    "DELETE FROM token_reservations WHERE user_id = ?",
                    (str(int(user_id)),),
                )
            else:
                cursor.execute(
                    "DELETE FROM token_reservations WHERE user_id = ? AND request_id = ?",
                    (str(int(user_id)), request_id),
                )
            conn.commit()
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def purge_expired_reservations() -> int:
    """Delete rows whose TTL has elapsed. Returns the number removed.

    Hooked into :meth:`SubscriptionService.run_hourly_jobs`. Safe to
    call when the table does not exist (returns 0).
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM token_reservations WHERE reserved_until <= datetime('now')"
            )
            removed = cursor.rowcount
            conn.commit()
            return int(removed or 0)
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def sweep_expired_credits() -> int:
    """Debit each expired credit entry exactly once.

    Scans ``wallet_transactions`` for rows whose ``expires_at`` is in
    the past and whose ``expiry_debited`` flag is still 0 (never
    debited). For each match:

    1. Reduce ``wallets.balance`` by ``abs(amount)`` atomically. The
       CHECK(balance >= 0) constraint on ``wallets`` is still enforced
       — the debit is capped at the current balance so partially-spent
       entries don't violate the constraint. Any remaining un-debited
       amount is forfeited (the user already consumed it).
    2. Insert a mirror ``expiry`` row so the ledger stays balanced.
    3. Flip ``expiry_debited`` to 1 so the sweep never fires twice.

    Returns the count of rows debited. Hooked into
    :meth:`SubscriptionService.run_daily_jobs`. Safe to call when the
    new columns don't exist yet (returns 0).
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT id, user_id, amount
                FROM wallet_transactions
                WHERE expires_at IS NOT NULL
                  AND expires_at <= datetime('now')
                  AND expiry_debited = 0
                  AND amount > 0
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Migration 30 not yet applied — behave as if no expiries
            return 0

        debited = 0
        for row in rows:
            tx_id = int(row["id"])
            user_id = int(row["user_id"])
            credit_amount = float(row["amount"])
            if credit_amount <= 0:
                # Defensive: only credit-side rows should have matched
                # the ``amount > 0`` filter, but skip silently if not.
                cursor.execute(
                    "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                    (tx_id,),
                )
                continue

            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "SELECT balance FROM wallets WHERE user_id = ?",
                    (user_id,),
                )
                wrow = cursor.fetchone()
                if not wrow:
                    # No wallet, nothing to debit — still flip the flag
                    # so we don't revisit this row.
                    cursor.execute(
                        "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                        (tx_id,),
                    )
                    cursor.execute("COMMIT")
                    continue
                old_balance = float(wrow["balance"] or 0)
                # Cap the debit at the current balance — the user may
                # have already spent part of this credit entry, and
                # clawing back more than they hold would violate the
                # wallets.balance CHECK constraint.
                debit = min(credit_amount, max(old_balance, 0.0))
                if debit <= 0:
                    # Balance already at/below zero — nothing to claw
                    # back, just flip the flag so we don't revisit.
                    cursor.execute(
                        "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                        (tx_id,),
                    )
                    cursor.execute("COMMIT")
                    debited += 1
                    continue
                new_balance = old_balance - debit
                cursor.execute(
                    """
                    UPDATE wallets
                    SET balance = ?,
                        total_consumed = total_consumed + ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ?
                    """,
                    (new_balance, debit, user_id),
                )
                # Record the mirror 'expiry' row with the actual debit
                # amount (may be less than the original credit if the
                # user already consumed part of it).
                cursor.execute(
                    """
                    INSERT INTO wallet_transactions
                        (user_id, type, amount, balance_after,
                         related_type, related_id, note)
                    VALUES (?, 'expiry', ?, ?, 'wallet_transaction', ?,
                            ?)
                    """,
                    (
                        user_id,
                        -debit,
                        new_balance,
                        tx_id,
                        f"Credits expired (original {credit_amount}, debited {debit})",
                    ),
                )
                cursor.execute(
                    "UPDATE wallet_transactions SET expiry_debited = 1 WHERE id = ?",
                    (tx_id,),
                )
                cursor.execute("COMMIT")
                debited += 1
            except Exception:
                cursor.execute("ROLLBACK")
                # Best-effort: log and move to the next row.
                import logging as _logging
                _logging.getLogger(__name__).exception(
                    "sweep_expired_credits failed for tx_id=%s user_id=%s",
                    tx_id,
                    user_id,
                )
        return debited
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 历史数据清理 helpers(可挂到 run_daily_jobs / 独立 cron)
#
# 这些 sweep_* 函数遵循统一约定:
#   * 返回 int —— 实际删除的行数
#   * 表不存在时静默返回 0(OperationalError 被吞掉)
#   * 单条 DELETE 事务,出错回滚并记 warning
# ---------------------------------------------------------------------------


def sweep_old_usage_logs(retention_days: int = 90) -> int:
    """删除超过保留期的 ``usage_logs`` 行。返回删除行数。

    ``usage_logs`` 是高频写表 —— 每条 API 请求一行,长期不清理会拖慢
    quota 查询与统计聚合。默认保留 90 天,可由调用方按需调整。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM usage_logs WHERE request_time < datetime('now', ?)",
                (f'-{int(retention_days)} days',),
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_usage_logs: deleted %s rows older than %s days",
                deleted,
                retention_days,
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_usage_logs failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_old_audit_logs(retention_days: int = 365) -> int:
    """删除超过保留期的 ``audit_logs`` 行。返回删除行数。

    审计日志默认保留 365 天以满足合规追溯需求;如需更短保留期可显式传参。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM audit_logs WHERE created_at < datetime('now', ?)",
                (f'-{int(retention_days)} days',),
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_audit_logs: deleted %s rows older than %s days",
                deleted,
                retention_days,
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_audit_logs failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_old_notifications(retention_days: int = 30) -> int:
    """删除已读且超过保留期的 ``notifications`` 行。返回删除行数。

    只清理 ``is_read = 1`` 的通知 —— 未读通知即使用户长期未读也不应被
    自动删除(可能是重要订阅 / 充值事件)。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM notifications WHERE is_read = 1"
                " AND created_at < datetime('now', ?)",
                (f'-{int(retention_days)} days',),
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_notifications: deleted %s rows older than %s days",
                deleted,
                retention_days,
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_notifications failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_old_conversations(retention_days: int = 90) -> int:
    """删除超过保留期的 ``conversations`` 行。返回删除行数。

    对话历史是用户内容,清理前应确认产品策略(是否提供导出)。默认 90 天
    保留期与 ``usage_logs`` 对齐。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM conversations WHERE created_at < datetime('now', ?)",
                (f'-{int(retention_days)} days',),
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_conversations: deleted %s rows older than %s days",
                deleted,
                retention_days,
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_conversations failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_expired_sessions() -> int:
    """清理过期 sessions,返回清理行数。

    ``sessions`` 表的 ``expires_at``(滑动窗口)和 ``absolute_expires_at``
    (绝对超时,由 migration 17 添加)任意一个过期即视为失效。本函数由
    run_daily_jobs 调用,防止 sessions 表无限增长。

    表不存在或列缺失时静默返回 0(OperationalError 被吞掉)。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM sessions "
                "WHERE expires_at < datetime('now') "
                "OR (absolute_expires_at IS NOT NULL "
                "AND absolute_expires_at < datetime('now'))"
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_expired_sessions: deleted %s expired sessions", deleted
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_expired_sessions failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_old_rate_limits() -> int:
    """清理过期的 ``rate_limits`` 行,返回清理行数。

    ``rate_limits`` 是 IP 限流中间件的计数器表,``window_start`` 标记窗口
    起始。超过 1 小时的行已无任何限流窗口会引用,可安全删除。本函数由
    run_daily_jobs 调用,防止表无限增长。

    表不存在时静默返回 0(OperationalError 被吞掉)。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM rate_limits "
                "WHERE window_start < datetime('now', '-1 hour')"
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_rate_limits: deleted %s stale rows", deleted
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_rate_limits failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def sweep_old_idempotency_keys() -> int:
    """清理超过 24h 的 ``idempotency_keys`` 行,返回清理行数。

    ``idempotency_keys`` 由 ``backend/utils/idempotency.py`` 懒创建,其
    ``_maybe_cleanup`` 是机会式清理(每 ``CLEANUP_INTERVAL`` 秒才跑一次,
    且只在有新请求时触发)。本函数提供确定性的批量清理入口,可由
    run_daily_jobs 调用,确保即使无新请求也能回收空间。

    表不存在时静默返回 0(OperationalError 被吞掉)。
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM idempotency_keys "
                "WHERE created_at < datetime('now', '-24 hours')"
            )
            deleted = cursor.rowcount
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                "sweep_old_idempotency_keys: deleted %s rows older than 24h",
                deleted,
            )
            return int(deleted or 0)
        except sqlite3.OperationalError:
            return 0
        except Exception:
            conn.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "sweep_old_idempotency_keys failed", exc_info=True
            )
            return 0
    finally:
        conn.close()


def run_wal_checkpoint() -> Dict[str, Any]:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` to fold the WAL back
    into the main DB file and truncate ``-wal`` / ``-shm`` to zero bytes.

    Why this matters
    ----------------
    SQLite WAL mode keeps recent writes in a ``-wal`` sidecar file so
    readers don't block writers. Without periodic checkpointing the
    ``-wal`` file grows unbounded — long-running deployments see disk
    usage climb steadily and read latency degrade as the WAL grows.

    The daily worker hooks this into :meth:`run_daily_jobs` so the
    WAL is folded once a day during the low-traffic maintenance
    window. ``TRUNCATE`` mode is used (vs the default ``PASSIVE``)
    because the maintenance window is the only safe place to do a
    blocking checkpoint — during the day readers/writers must not
    stall.

    Returns a dict with ``wal_frames`` and ``wal_checkpointed`` raw
    values from the PRAGMA so operators can see how much work was
    done. Errors are logged and returned as ``{"error": "..."}``
    rather than raised — a failed checkpoint must not abort the rest
    of the daily jobs.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = cursor.fetchone()
            # PRAGMA wal_checkpoint returns (busy, log_frames, checkpointed_frames)
            import logging as _logging

            _logging.getLogger(__name__).info(
                "wal_checkpoint(TRUNCATE): busy=%s log_frames=%s checkpointed=%s",
                row[0] if row else "?",
                row[1] if row else "?",
                row[2] if row else "?",
            )
            return {
                "busy": int(row[0]) if row and row[0] is not None else None,
                "wal_frames": int(row[1]) if row and row[1] is not None else None,
                "wal_checkpointed": int(row[2]) if row and row[2] is not None else None,
            }
        except sqlite3.OperationalError as exc:
            # WAL not enabled (e.g. :memory: in tests) — no-op.
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "wal_checkpoint skipped: %s", exc
            )
            return {"skipped": True, "reason": str(exc)}
        except Exception as exc:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "wal_checkpoint failed", exc_info=True
            )
            return {"error": str(exc)}
    finally:
        conn.close()


def get_user_plan(user_id: int) -> Optional[Dict[str, Any]]:
    """Return the user's effective plan as a dict.

    Checks ``users.plan_id`` first (direct FK), then falls back to the
    most recent active subscription. Returns sensible defaults when no
    plan is attached.

    This is the single source of truth used by both billing_service
    (for discount rates) and quota_service (for rate limits).
    """
    # Fallback used when the user has no plan_id and no active
    # subscription (or the referenced plan row has been deleted).
    # Kept in lockstep with the free plan seed in DEFAULT_PLANS above
    # so users without a persisted plan are treated identically to
    # free-plan users — in particular they are subject to the same
    # RPM / TPM rate limits, not "unlimited".
    _FREE_FALLBACK: Dict[str, Any] = {
        "id": None,
        "code": "free",
        "name": "免费版",
        "discount_rate": 1.0,
        "rate_limit_rpm": 20,
        "rate_limit_tpm": 50000,
        "monthly_credits": 10000,
        "monthly_price": 0,
        "max_api_keys": 1,
        "max_concurrent": 2,
    }

    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Check users.plan_id (direct FK)
        cursor.execute("SELECT plan_id FROM users WHERE id = ?", (user_id,))
        user_row = cursor.fetchone()
        plan_id = user_row["plan_id"] if user_row else None

        # 2. Fallback to most recent active subscription
        if not plan_id:
            cursor.execute(
                """
                SELECT plan_id FROM subscriptions
                WHERE user_id = ? AND status = 'active'
                ORDER BY id DESC LIMIT 1
            """,
                (user_id,),
            )
            sub_row = cursor.fetchone()
            plan_id = sub_row["plan_id"] if sub_row else None

        if not plan_id:
            return dict(_FREE_FALLBACK)

        cursor.execute(
            """
            SELECT id, code, name, discount_rate, rate_limit_rpm, rate_limit_tpm,
                   monthly_credits, monthly_price, max_api_keys, max_concurrent
            FROM plans WHERE id = ?
        """,
            (plan_id,),
        )
        plan = cursor.fetchone()
        if not plan:
            return dict(_FREE_FALLBACK)
        result = dict(plan)
        result["discount_rate"] = float(result.get("discount_rate") or 1.0)
        if result["discount_rate"] <= 0:
            result["discount_rate"] = 1.0
        return result
    finally:
        conn.close()


def update_wallet(
    user_id: int,
    delta: float,
    tx_type: str,
    related_type: str = None,
    related_id: int = None,
    note: str = None,
    expires_at: Optional[str] = "_auto",
) -> dict:
    """Apply a +/- delta to a user's wallet in a single transaction.

    Negative deltas that would drive the balance below zero raise ValueError
    (caller may catch to record the failure in usage_logs).

    ``expires_at`` is only threaded through when ``delta > 0`` — debits
    don't represent credits the user holds, so expiry doesn't apply.
    ``"_auto"`` (default) stamps the operator-configured horizon via
    :func:`_credits_expire_at`; pass ``None`` explicitly to opt out.
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("INSERT INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,))
            cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
        new_balance = float(row["balance"]) + float(delta)
        if new_balance < 0:
            conn.rollback()
            try:
                from backend.services.alert_service import AlertService

                AlertService.send_alert_sync(
                    "WARNING",
                    f"Wallet negative balance attempt for user {user_id}",
                    {
                        "user_id": user_id,
                        "current_balance": float(row["balance"]),
                        "delta": float(delta),
                        "would_be": new_balance,
                        "tx_type": tx_type,
                    },
                )
            except Exception:
                pass
            raise ValueError("余额不足")
        if delta > 0:
            cursor.execute(
                """
                UPDATE wallets
                SET balance = ?, total_recharged = total_recharged + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """,
                (new_balance, delta, user_id),
            )
        else:
            cursor.execute(
                """
                UPDATE wallets
                SET balance = ?, total_consumed = total_consumed + ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """,
                (new_balance, -delta, user_id),
            )

        # Expiry only applies to credit-side entries. "_auto" resolves
        # to the operator-configured horizon; None disables; a literal
        # timestamp string is passed through verbatim.
        #
        # Defence-in-depth: stream-reconcile refunds return unused
        # reservations to the wallet. They must NOT refresh the credit
        # TTL even if a caller forgot to pass ``expires_at=None`` —
        # otherwise users could "launder" expiring credits by cycling
        # pre-reserve → refund. ``grant_credits`` is unaffected because
        # it represents genuine new credit, not a return of reserved
        # funds.
        tx_expires_at: Optional[str] = None
        if delta > 0:
            if expires_at == "_auto":
                if tx_type == "refund" and related_type == "stream_reconcile":
                    tx_expires_at = None
                else:
                    tx_expires_at = _credits_expire_at()
            else:
                tx_expires_at = expires_at

        cursor.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, type, amount, balance_after,
                 related_type, related_id, note,
                 expires_at, expiry_debited)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
            (
                user_id,
                tx_type,
                delta,
                new_balance,
                related_type,
                related_id,
                note,
                tx_expires_at,
            ),
        )
        conn.commit()
        return {
            "balance": new_balance,
            "total_recharged": float(row["total_recharged"]) + (delta if delta > 0 else 0),
            "total_consumed": float(row["total_consumed"]) + (-delta if delta < 0 else 0),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def charge_for_usage_atomic(
    user_id: int,
    usage_log_id: int,
    cost: float,
    error_message: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """Atomically charge a user's wallet for a usage_logs row.

    Performs the idempotency check, wallet debit, wallet_transaction insert,
    and usage_logs write-back inside a single ``BEGIN IMMEDIATE`` transaction
    on one connection. This eliminates the double-spend and inconsistent-state
    race conditions that existed when these steps ran on separate connections.

    Parameters
    ----------
    user_id:
        The user who owns the usage log row.
    usage_log_id:
        Primary key of the usage_logs row to charge.
    cost:
        Pre-computed credit cost (must be >= 0). Pass 0 to mark the row as
        processed without debiting the wallet (idempotency still works on
        retry because cost_credits is set to 0 explicitly).
    error_message:
        If not None, write this string into usage_logs.error_message instead
        of charging. Used by the caller to record failure reasons.

    Returns
    -------
    str
        ``"ok"`` — charge succeeded (or cost was 0 and row was marked).
        ``"already_charged"`` — usage_logs.cost_credits was already > 0.
        ``"not_found"`` — usage_log_id does not exist.
        ``"user_mismatch"`` — the row's user_id doesn't match.
        ``"insufficient"`` — wallet balance too low.
        ``"error:<msg>"`` — unexpected database error.
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        # --- Re-read usage_logs row under the RESERVED lock ---------------
        cursor.execute(
            """
            SELECT id, user_id, cost_credits, error_message
            FROM usage_logs WHERE id = ?
            """,
            (usage_log_id,),
        )
        log_row = cursor.fetchone()
        if not log_row:
            conn.rollback()
            return "not_found"

        if int(log_row["user_id"]) != int(user_id):
            conn.rollback()
            return "user_mismatch"

        # --- Idempotency: already charged --------------------------------
        existing_cost = float(log_row["cost_credits"] or 0)
        if existing_cost > 0:
            conn.rollback()
            return "already_charged"

        # --- If caller wants us to just record an error ------------------
        if error_message is not None:
            cursor.execute(
                """
                UPDATE usage_logs
                SET cost_credits = ?, error_message = ?
                WHERE id = ?
                """,
                (0.0, error_message, usage_log_id),
            )
            conn.commit()
            return "ok"

        # --- Zero-cost: mark row as processed, no wallet debit -----------
        if cost <= 0:
            cursor.execute(
                """
                UPDATE usage_logs SET cost_credits = 0 WHERE id = ?
                """,
                (usage_log_id,),
            )
            conn.commit()
            return "ok"

        # --- Read wallet balance on the SAME connection ------------------
        cursor.execute("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
        wallet_row = cursor.fetchone()
        if not wallet_row:
            # Auto-create wallet (mirrors update_wallet behavior)
            cursor.execute(
                "INSERT INTO wallets (user_id, balance) VALUES (?, 0)",
                (user_id,),
            )
            current_balance = 0.0
        else:
            current_balance = float(wallet_row["balance"])

        if current_balance < cost:
            # Insufficient balance: record the failure on the usage row
            conn.rollback()
            # Write the error annotation in a separate short transaction
            cursor.execute(
                """
                UPDATE usage_logs
                SET cost_credits = 0,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    f"insufficient_balance: 余额不足 (need {cost}, have {current_balance})",
                    usage_log_id,
                ),
            )
            conn.commit()
            return "insufficient"

        # --- Debit wallet ------------------------------------------------
        new_balance = current_balance - cost
        cursor.execute(
            """
            UPDATE wallets
            SET balance = ?,
                total_consumed = total_consumed + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (new_balance, cost, user_id),
        )
        cursor.execute(
            """
            INSERT INTO wallet_transactions
                (user_id, type, amount, balance_after, related_type, related_id, note)
            VALUES (?, 'consume', ?, ?, 'usage', ?, ?)
            """,
            (user_id, -cost, new_balance, usage_log_id, note),
        )

        # --- Write back cost on usage_logs -------------------------------
        cursor.execute(
            """
            UPDATE usage_logs SET cost_credits = ? WHERE id = ?
            """,
            (cost, usage_log_id),
        )

        conn.commit()
        return "ok"

    except sqlite3.OperationalError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        return f"error:{exc}"
    finally:
        conn.close()


def get_model_pricing(provider: str, model_id: str) -> Optional[dict]:
    """Fetch the most relevant pricing row for a (provider, model) pair.

    Prefers admin customizations (`is_custom=1`), then standard tier. Returns
    None when no pricing has been configured.
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT input_price_per_1k, output_price_per_1k, tier, is_custom
            FROM model_pricing
            WHERE provider = ? AND model_id = ? AND is_active = 1
            ORDER BY is_custom DESC, tier = 'standard' DESC
            LIMIT 1
        """,
            (provider, model_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "input_price_per_1k": float(row["input_price_per_1k"]),
            "output_price_per_1k": float(row["output_price_per_1k"]),
            "tier": row["tier"],
            "is_custom": bool(row["is_custom"]),
        }
    finally:
        conn.close()


def list_effective_pricing(
    provider: Optional[str] = None,
    *,
    include_inactive: bool = False,
) -> List[Dict[str, Any]]:
    """Return the *effective* pricing for every (provider, model, tier).

    Effective pricing = admin custom row when present, otherwise the
    official default row. This is what the public / model picker
    endpoints surface to the frontend, so the user always sees the
    price the system will actually charge.

    Parameters
    ----------
    provider:
        Filter by provider slug (case-insensitive). ``None`` returns
        every provider.
    include_inactive:
        Include ``is_active = 0`` rows. Used by the admin pricing
        console to show soft-deleted entries.
    """
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        clauses = ["1=1"]
        params: List[Any] = []
        if not include_inactive:
            clauses.append("p.is_active = 1")
        if provider:
            clauses.append("LOWER(p.provider) = LOWER(?)")
            params.append(provider)
        sql = f"""
            SELECT p.id, p.provider, p.model_id, p.input_price_per_1k,
                   p.output_price_per_1k, p.tier, p.is_custom, p.is_active,
                   p.note, p.updated_at
              FROM model_pricing p
              JOIN (
                  SELECT provider, model_id, MAX(is_custom) AS max_custom
                    FROM model_pricing
                   WHERE is_active = 1
                   GROUP BY provider, model_id
              ) m ON m.provider = p.provider
                 AND m.model_id = p.model_id
                 AND m.max_custom = p.is_custom
             WHERE {" AND ".join(clauses)}
             ORDER BY p.provider ASC, p.model_id ASC, p.tier ASC
        """
        cursor.execute(sql, tuple(params))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_pricing_for_model_list(model_ids: List[str]) -> Dict[str, dict]:
    """Bulk-fetch effective pricing for a list of ``provider/model`` strings.

    Returns ``{model_id: {input, output, tier, is_custom, provider}}`` for
    the models that have any pricing row, missing entries are simply
    absent. Used by the public /v1/models endpoint to enrich the response
    without performing N individual queries.
    """
    if not model_ids:
        return {}
    parsed: List[Tuple[str, str]] = []
    for raw in model_ids:
        if not raw or "/" not in raw:
            continue
        provider, _, model = raw.partition("/")
        if provider and model:
            parsed.append((provider.strip(), model.strip()))

    out: Dict[str, dict] = {}
    if not parsed:
        return out

    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Build a parameterized IN clause (provider, model) tuples.
        # SQLite supports row-value IN predicates.
        placeholders = ",".join(["(?, ?)"] * len(parsed))
        params: List[Any] = []
        for p, m in parsed:
            params.extend([p, m])
        sql = f"""
            SELECT provider, model_id, input_price_per_1k, output_price_per_1k,
                   tier, is_custom
              FROM model_pricing
             WHERE is_active = 1
               AND (provider, model_id) IN ({placeholders})
        """
        try:
            cursor.execute(sql, tuple(params))
        except sqlite3.OperationalError:
            # Older SQLite versions (<3.15) don't support row-value IN.
            # Fall back to OR-chained equality.
            parts = []
            fb_params: List[Any] = []
            for p, m in parsed:
                parts.append("(provider = ? AND model_id = ?)")
                fb_params.extend([p, m])
            sql2 = f"""
                SELECT provider, model_id, input_price_per_1k, output_price_per_1k,
                       tier, is_custom
                  FROM model_pricing
                 WHERE is_active = 1
                   AND ({" OR ".join(parts)})
            """
            cursor.execute(sql2, tuple(fb_params))
        rows = cursor.fetchall()
    finally:
        conn.close()

    # Pick the most-preferred (custom > standard) per (provider, model).
    by_key: Dict[Tuple[str, str], sqlite3.Row] = {}
    for r in rows:
        key = (r["provider"], r["model_id"])
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = r
            continue
        if int(r["is_custom"]) > int(prev["is_custom"]):
            by_key[key] = r
        elif (
            int(r["is_custom"]) == int(prev["is_custom"])
            and r["tier"] == "standard"
            and prev["tier"] != "standard"
        ):
            by_key[key] = r

    for (provider, model), r in by_key.items():
        out[f"{provider}/{model}"] = {
            "input_price_per_1k": float(r["input_price_per_1k"]),
            "output_price_per_1k": float(r["output_price_per_1k"]),
            "tier": r["tier"],
            "is_custom": bool(r["is_custom"]),
            "provider": provider,
        }
    return out


def validate_api_key_format(api_key: str) -> bool:
    if not api_key or len(api_key) < 20:
        return False
    return bool(re.match(r"^[A-Za-z0-9_-]+$", api_key))


def get_client_ip(request) -> str:
    def parse_xff_first_ip(value: str) -> str | None:
        first = (value or "").split(",")[0].strip()
        if not first:
            return None
        try:
            return str(ip_address(first))
        except Exception:
            return None

    def ip_is_trusted_proxy(client_ip: str, trusted_proxies: list[str]) -> bool:
        if not trusted_proxies:
            return False
        try:
            ip_obj = ip_address(client_ip)
        except Exception:
            return False
        for raw in trusted_proxies:
            s = str(raw or "").strip()
            if not s:
                continue
            if "/" in s:
                try:
                    if ip_obj in ip_network(s, strict=False):
                        return True
                except Exception:
                    continue
            else:
                if client_ip == s:
                    return True
        return False

    forwarded = request.headers.get("X-Forwarded-For")
    client_ip = request.client.host if request.client else ""
    if forwarded:
        from backend.config import Config

        trusted_proxies = Config.TRUSTED_PROXIES
        trust_xff = False
        if not Config.is_production() and not trusted_proxies:
            trust_xff = True
        else:
            trust_xff = ip_is_trusted_proxy(client_ip, trusted_proxies)

        if trust_xff:
            parsed = parse_xff_first_ip(forwarded)
            if parsed:
                return parsed

    return client_ip or "unknown"


# ---------------------------------------------------------------------------
# User-defined model pools (migration 35)
#
# Each user can register their own pool of OpenAI-compatible upstream
# endpoints (api_base + api_key + model_name) ordered by priority. The
# proxy layer can route requests through these user-owned pools when
# the user has opted in (e.g. for bring-your-own-key flows).
#
# ``api_base`` and ``api_key`` are stored encrypted at rest with
# ``Security.encrypt``; the helpers below decrypt transparently on read.
# ---------------------------------------------------------------------------


_POOL_COLS = (
    "id, user_id, name, provider_type, api_base, api_key_encrypted, "
    "model_name, priority, max_tokens, used_tokens, is_active, "
    "cooldown_until, last_error, created_at, updated_at"
)


def _decrypt_pool_row(row: sqlite3.Row) -> Optional[Dict[str, Any]]:
    """Convert a ``user_model_pools`` row to a dict with decrypted
    ``api_base`` and ``api_key`` fields (renamed from
    ``api_key_encrypted``)."""
    from backend.security import Security

    if row is None:
        return None
    d = dict(row)
    encrypted_base = d.pop("api_base", None)
    encrypted_key = d.pop("api_key_encrypted", None)
    d["api_base"] = Security.decrypt(encrypted_base) if encrypted_base else ""
    d["api_key"] = Security.decrypt(encrypted_key) if encrypted_key else ""
    return d


def create_user_model_pool(
    user_id: int,
    name: str,
    provider_type: str,
    api_base_encrypted: str,
    api_key_encrypted: str,
    model_name: str,
    priority: int = 0,
    max_tokens: int = 0,
) -> int:
    """Insert a new user_model_pools row and return its id.

    ``api_base_encrypted`` and ``api_key_encrypted`` are expected to be
    already encrypted by the caller (route layer). This keeps the
    encryption concern out of the DB layer so the helper can be used
    from contexts where ``Security.encrypt`` is not yet available
    (e.g. migration scripts).
    """
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_model_pools
                (user_id, name, provider_type, api_base, api_key_encrypted,
                 model_name, priority, max_tokens, used_tokens, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1)
            """,
            (
                int(user_id),
                name,
                provider_type or "openai",
                api_base_encrypted,
                api_key_encrypted,
                model_name,
                int(priority),
                int(max_tokens),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_user_model_pools(user_id: int) -> List[Dict[str, Any]]:
    """Return all model pools owned by ``user_id``, decrypted, ordered
    by ``priority ASC, id ASC``."""
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_POOL_COLS} FROM user_model_pools WHERE user_id = ? "
            "ORDER BY priority ASC, id ASC",
            (int(user_id),),
        )
        return [_decrypt_pool_row(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_user_model_pool(pool_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    """Return a single pool row, verifying ``user_id`` ownership."""
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_POOL_COLS} FROM user_model_pools "
            "WHERE id = ? AND user_id = ?",
            (int(pool_id), int(user_id)),
        )
        return _decrypt_pool_row(cursor.fetchone())
    finally:
        conn.close()


def update_user_model_pool(pool_id: int, user_id: int, **fields: Any) -> bool:
    """Update specified fields on a pool row. Returns True if a row was
    updated.

    Only the following fields are allowed to be updated:
    ``name, provider_type, api_base, api_key_encrypted, model_name,
    priority, max_tokens, is_active``. ``api_base`` and
    ``api_key_encrypted`` are stored as-is (caller must encrypt).
    """
    allowed = {
        "name", "provider_type", "api_base", "api_key_encrypted",
        "model_name", "priority", "max_tokens", "is_active",
    }
    updates: List[str] = []
    params: List[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.extend([int(pool_id), int(user_id)])
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE user_model_pools SET {', '.join(updates)} "
            "WHERE id = ? AND user_id = ?",
            tuple(params),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_user_model_pool(pool_id: int, user_id: int) -> bool:
    """Delete a pool row, verifying ownership. Returns True on success."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_model_pools WHERE id = ? AND user_id = ?",
            (int(pool_id), int(user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def reorder_user_model_pools(user_id: int, ordered_ids: List[int]) -> None:
    """Reassign ``priority`` for each pool id in ``ordered_ids`` (index
    0 → priority 0, index 1 → priority 1, …). Pools not in the list are
    left untouched."""
    if not ordered_ids:
        return
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            for idx, pool_id in enumerate(ordered_ids):
                cursor.execute(
                    "UPDATE user_model_pools SET priority = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND user_id = ?",
                    (int(idx), int(pool_id), int(user_id)),
                )
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def create_model_pool_key(
    user_id: int,
    key_hash: str,
    key_prefix: str,
    name: Optional[str] = None,
) -> int:
    """Insert a new user_model_pool_keys row and return its id."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_model_pool_keys
                (user_id, key_hash, key_prefix, name, is_active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (int(user_id), key_hash, key_prefix, name),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def get_model_pool_key_by_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    """Return the key record (including ``user_id``) matching ``key_hash``,
    or ``None``."""
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, key_hash, key_prefix, name, is_active, "
            "created_at, last_used_at FROM user_model_pool_keys "
            "WHERE key_hash = ? LIMIT 1",
            (key_hash,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_model_pool_keys(user_id: int) -> List[Dict[str, Any]]:
    """Return all pool keys for ``user_id``. The full ``key_hash`` is
    intentionally omitted — only the prefix is exposed for listing."""
    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, user_id, key_prefix, name, is_active, "
            "created_at, last_used_at FROM user_model_pool_keys "
            "WHERE user_id = ? ORDER BY id ASC",
            (int(user_id),),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def delete_model_pool_key(key_id: int, user_id: int) -> bool:
    """Delete a pool key, verifying ownership. Returns True on success."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM user_model_pool_keys WHERE id = ? AND user_id = ?",
            (int(key_id), int(user_id)),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def touch_model_pool_key(key_id: int) -> None:
    """Update ``last_used_at`` to ``CURRENT_TIMESTAMP`` for the given key."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_model_pool_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(key_id),),
        )
        conn.commit()
    finally:
        conn.close()


def increment_model_pool_usage(pool_id: int, tokens: int) -> None:
    """Increment ``used_tokens`` by ``tokens`` for the given pool."""
    if int(tokens or 0) <= 0:
        return
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_model_pools SET used_tokens = used_tokens + ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(tokens), int(pool_id)),
        )
        conn.commit()
    finally:
        conn.close()


def set_model_pool_cooldown(
    pool_id: int, seconds: int, error_msg: Optional[str]
) -> None:
    """Set ``cooldown_until`` to ``now + seconds`` and ``last_error`` to
    ``error_msg`` on the given pool."""
    from datetime import datetime as _dt, timezone as _tz

    until = (_dt.now(_tz.utc) + timedelta(seconds=int(seconds))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_model_pools SET cooldown_until = ?, last_error = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (until, error_msg, int(pool_id)),
        )
        conn.commit()
    finally:
        conn.close()


def get_next_model_pool(user_id: int) -> Optional[Dict[str, Any]]:
    """Pick the next eligible pool for ``user_id``.

    Selection order:
      1. ``is_active = 1`` AND (``cooldown_until`` is NULL or in the past)
         AND (``max_tokens = 0`` OR ``used_tokens < max_tokens``)
         → first row ordered by ``priority ASC, id ASC``.
      2. If every active pool has hit its ``max_tokens`` cap, fall back
         to a random active pool (the operator can cool it down via
         :func:`set_model_pool_cooldown` after a failure).
      3. If there are no active pools at all, return ``None``.
    """
    import random as _random

    conn = get_db()
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # 1. Eligible pool with capacity remaining.
        cursor.execute(
            f"SELECT {_POOL_COLS} FROM user_model_pools "
            "WHERE user_id = ? AND is_active = 1 "
            "  AND (cooldown_until IS NULL OR cooldown_until < datetime('now')) "
            "  AND (max_tokens = 0 OR used_tokens < max_tokens) "
            "ORDER BY priority ASC, id ASC LIMIT 1",
            (int(user_id),),
        )
        row = cursor.fetchone()
        if row:
            return _decrypt_pool_row(row)

        # 2. All active pools exhausted — pick a random one.
        cursor.execute(
            f"SELECT {_POOL_COLS} FROM user_model_pools "
            "WHERE user_id = ? AND is_active = 1 "
            "ORDER BY priority ASC, id ASC",
            (int(user_id),),
        )
        rows = cursor.fetchall()
        if rows:
            return _decrypt_pool_row(_random.choice(rows))
        return None
    finally:
        conn.close()
