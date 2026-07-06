from __future__ import annotations

"""Pytest configuration for backend tests.

We use a temporary SQLite file and redirect every service call to it
by patching ``backend.database.DATABASE_PATH``. The schema is created
explicitly (rather than via ``init_db``) to keep tests self-contained
and free of any plan / pricing seed data that production code adds.
"""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _reset_db_pool_between_tests():
    """Make sure no test inherits a stale DatabasePool from the
    previous one.

    A handful of tests (``test_token_usage_stats``, ``test_regression_flows``,
    ``test_phase6_trusted_proxies_and_rate_limits``, etc.) ``importlib.reload``
    the ``backend.config`` / ``backend.security`` modules and set
    ``os.environ["DATABASE_PATH"]`` to their own temp file. Without a
    blanket reset that env var leaks into the *next* test in the
    run, which then writes to a file that has already been deleted.
    That manifests as ``UNIQUE constraint failed``,
    ``database is locked`` and ``no such column: cost_credits``
    failures that only show up in the full suite (not in isolation).

    We also clear the cached ``DatabasePool`` singleton and the
    idempotency table's last-cleanup timestamp, so the new test really
    does start from a clean slate.
    """
    # Pre-test: clear env vars that some tests set in ways that leak
    # into subsequent ones.
    for env_name in (
        "DATABASE_PATH",
        "MINIMAX_API_KEY",
        "MINIMAX_API_BASE",
        "NVIDIA_API_KEY",
        "NVIDIA_API_BASE",
        "SECRET_KEY",
        "ENCRYPTION_KEY",
        "ALLOW_LEGACY_X_API_KEY",
        "ALLOW_API_KEY_LOGIN",
        "ENV",
        "CORS_ORIGINS",
        "RATE_LIMIT_PER_MINUTE",
        "RATE_LIMIT_PER_HOUR",
        "CHANNEL_COOLDOWN_SECONDS",
        "CHANNEL_FALLBACK_MAX",
        "TRUSTED_PROXIES",
    ):
        os.environ.pop(env_name, None)

    yield

    # Post-test: drop the singleton pool and the in-process caches.
    try:
        from backend.utils import db_pool

        if db_pool._POOL is not None:
            db_pool._POOL.close_all()
            db_pool._POOL = None
    except Exception:
        pass
    try:
        from backend.utils import idempotency

        idempotency._last_cleanup_ts = 0.0
    except Exception:
        pass


# Minimal schema covering every table referenced by usage / quota /
# health services. Kept in sync with backend/database.py — when the
# real schema evolves, update this fixture accordingly.
_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    email VARCHAR(100),
    password_hash VARCHAR(255),
    api_key VARCHAR(64) UNIQUE NOT NULL,
    api_key_hash VARCHAR(64),
    quota_5h INTEGER DEFAULT 3000,
    quota_week INTEGER DEFAULT 5000,
    quota_month INTEGER DEFAULT 0,
    monthly_budget NUMERIC DEFAULT 0,
    plan_id INTEGER,
    plan_expires_at TIMESTAMP,
    is_active INTEGER DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    api_key_id INTEGER,
    endpoint VARCHAR(200),
    model VARCHAR(100),
    provider VARCHAR(50),
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_credits NUMERIC DEFAULT 0,
    request_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    response_time_ms INTEGER DEFAULT 0,
    status_code INTEGER,
    ip_address VARCHAR(45),
    error_message TEXT,
    token_id INTEGER,
    channel_id INTEGER,
    metadata TEXT
);

CREATE TABLE wallets (
    user_id INTEGER PRIMARY KEY,
    balance NUMERIC DEFAULT 0 CHECK (balance >= 0),
    total_recharged NUMERIC DEFAULT 0,
    total_consumed NUMERIC DEFAULT 0,
    frozen NUMERIC DEFAULT 0 CHECK (frozen >= 0),
    auto_recharge_enabled INTEGER DEFAULT 0,
    auto_recharge_threshold NUMERIC,
    auto_recharge_amount NUMERIC,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type VARCHAR(20) NOT NULL,
    amount NUMERIC NOT NULL CHECK (amount != 0),
    balance_after NUMERIC NOT NULL,
    related_type VARCHAR(20),
    related_id INTEGER,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    expiry_debited INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE orders (
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
    payment_session_id VARCHAR(200),
    payment_provider VARCHAR(20),
    payment_reference VARCHAR(200),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE promo_codes (
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

CREATE TABLE promo_code_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_code_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    order_id INTEGER,
    credits_granted NUMERIC,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE redeem_codes (
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

CREATE TABLE redeem_code_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    redeem_code_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    credits_granted NUMERIC,
    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE audit_logs (
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
);

CREATE TABLE api_keys (
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
    allowed_ips TEXT,
    is_active INTEGER DEFAULT 1,
    last_used_at TIMESTAMP,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE provider_health (
    provider VARCHAR(50) PRIMARY KEY,
    status VARCHAR(20) DEFAULT 'unknown',
    last_check TIMESTAMP,
    latency_p50 INTEGER DEFAULT 0,
    latency_p95 INTEGER DEFAULT 0,
    success_rate_1h NUMERIC DEFAULT 1.0,
    requests_1h INTEGER DEFAULT 0,
    errors_1h INTEGER DEFAULT 0
);

CREATE TABLE provider_keys (
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

CREATE TABLE subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_id INTEGER NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    credits_used_this_period INTEGER DEFAULT 0,
    auto_renew INTEGER DEFAULT 1,
    cancelled_at TIMESTAMP,
    pending_plan_id INTEGER
);

CREATE TABLE admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP,
    is_active INTEGER DEFAULT 1,
    totp_secret TEXT,
    totp_enabled INTEGER DEFAULT 0,
    is_super_admin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(50),
    code VARCHAR(50) UNIQUE NOT NULL,
    rate_limit_rpm INTEGER DEFAULT 0,
    rate_limit_tpm INTEGER DEFAULT 0,
    monthly_price NUMERIC DEFAULT 0,
    monthly_credits INTEGER DEFAULT 0,
    discount_rate NUMERIC DEFAULT 1.0,
    max_api_keys INTEGER DEFAULT 1,
    max_concurrent INTEGER DEFAULT 5,
    features TEXT,
    sort_order INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    role VARCHAR(10) NOT NULL,
    content TEXT NOT NULL,
    model VARCHAR(100) DEFAULT '',
    title VARCHAR(100) DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT,
    is_encrypted INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier VARCHAR(100) NOT NULL,
    request_count INTEGER DEFAULT 0,
    window_start TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    limit_type VARCHAR(20) NOT NULL,
    UNIQUE(identifier, limit_type)
);

CREATE TABLE models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id VARCHAR(100) UNIQUE NOT NULL,
    display_name VARCHAR(200),
    provider VARCHAR(50) NOT NULL,
    is_active INTEGER DEFAULT 1,
    context_length INTEGER DEFAULT 0,
    last_synced TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_model_access (
    user_id INTEGER NOT NULL,
    model_id VARCHAR(200) NOT NULL,
    access_type VARCHAR(20) DEFAULT 'allow',
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by INTEGER,
    PRIMARY KEY (user_id, model_id)
);

CREATE TABLE subscription_requests (
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

CREATE INDEX idx_subscription_requests_status ON subscription_requests(status);
CREATE INDEX idx_subscription_requests_user ON subscription_requests(user_id);

CREATE TABLE model_pricing (
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

CREATE TABLE custom_providers (
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

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE quota_resets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reset_type VARCHAR(20),
    reset_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE usage_rollups (
    user_id INTEGER NOT NULL,
    bucket_minute INTEGER NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bucket_minute),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name VARCHAR(100),
    token_prefix VARCHAR(32) NOT NULL,
    token_hash VARCHAR(64) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP,
    is_active INTEGER DEFAULT 1,
    revoked_at TIMESTAMP,
    expires_at TIMESTAMP,
    allowed_models TEXT,
    allowed_ips TEXT,
    rate_limit_per_minute INTEGER,
    rate_limit_per_hour INTEGER,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE channels (
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
);

CREATE TABLE sessions (
    session_id VARCHAR(100) PRIMARY KEY,
    role VARCHAR(20) NOT NULL,
    admin_id INTEGER,
    user_id INTEGER,
    username VARCHAR(100),
    email VARCHAR(100),
    csrf VARCHAR(128) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address VARCHAR(45),
    user_agent VARCHAR(512),
    absolute_expires_at TIMESTAMP
);

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type VARCHAR(30) NOT NULL,
    title VARCHAR(200),
    content TEXT,
    is_read INTEGER DEFAULT 0,
    metadata TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE password_reset_tokens (
    token VARCHAR(64) PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_prt_expires ON password_reset_tokens(expires_at);
CREATE INDEX idx_prt_user ON password_reset_tokens(user_id);

CREATE INDEX idx_usage_logs_user_time ON usage_logs(user_id, request_time);
CREATE INDEX idx_tokens_user_id ON tokens(user_id);
CREATE INDEX idx_tokens_hash ON tokens(token_hash);
CREATE INDEX idx_channels_provider_active ON channels(provider, is_active);
CREATE INDEX idx_channels_cooldown ON channels(cooldown_until);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
CREATE INDEX idx_sessions_absolute_expires ON sessions(absolute_expires_at);
CREATE INDEX idx_usage_rollups_bucket ON usage_rollups(bucket_minute);

-- Migration 22: query optimization indexes
CREATE INDEX idx_usage_logs_user_status_time ON usage_logs(user_id, status_code, request_time);
CREATE INDEX idx_usage_logs_user_model_time ON usage_logs(user_id, model, request_time);
CREATE INDEX idx_usage_logs_user_cost ON usage_logs(user_id, cost_credits);
CREATE INDEX idx_usage_logs_time ON usage_logs(request_time);
CREATE INDEX idx_wallet_tx_user_created ON wallet_transactions(user_id, created_at);
CREATE INDEX idx_wallet_tx_user_type_related ON wallet_transactions(user_id, type, related_type, related_id);
CREATE INDEX idx_orders_user_status ON orders(user_id, status);
CREATE INDEX idx_notifications_user_created ON notifications(user_id, created_at DESC);

-- Migration 29 + Migration 32: per-request token reservations (multi-row, TTL-guarded)
CREATE TABLE token_reservations (
    user_id          TEXT NOT NULL,
    request_id       TEXT NOT NULL DEFAULT '',
    reserved_tokens  INTEGER   NOT NULL DEFAULT 0,
    reserved_until   TIMESTAMP NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, request_id)
);
CREATE INDEX idx_token_reservations_until ON token_reservations(reserved_until);
CREATE INDEX idx_token_reservations_user_until ON token_reservations(user_id, reserved_until);

-- Migration 33: 部分索引(非 UNIQUE),加速 get_active 查询。
-- "唯一 active" 不变量由应用层(renew/upgrade/process_expiry)维护,
-- 不在 DB 层强制,以免阻断 process_expiry 尚未运行时的过渡态。
CREATE INDEX idx_subscriptions_active_user
ON subscriptions(user_id) WHERE status = 'active';

-- Migration 35: user-defined model pools (bring-your-own-key flows).
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
);
CREATE INDEX idx_user_model_pools_user ON user_model_pools(user_id, is_active, priority);

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
);
CREATE INDEX idx_user_model_pool_keys_hash ON user_model_pool_keys(key_hash);

-- Migration 36: notification_cooldowns (formalised from notification_service lazy-create)
CREATE TABLE notification_cooldowns (
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    last_sent_at TIMESTAMP NOT NULL,
    UNIQUE(user_id, type)
);
CREATE INDEX idx_notification_cooldowns_user ON notification_cooldowns(user_id);
"""


@pytest.fixture
def temp_db(monkeypatch):
    """Yield a temporary database path bound to backend.database.

    Patches the module-level constant so every subsequent ``get_db()``
    call (including those from :mod:`backend.services.*`) writes to the
    temp file. The original path is restored automatically when the
    fixture tears down.

    The fixture also tears down the singleton :class:`DatabasePool` (if
    it has been initialised by an earlier test) and other module-level
    caches that hold open file descriptors, so each test really does
    start with a brand-new SQLite file and a brand-new pool of
    connections. Without this, the pool from the previous test would
    still hold connections to the previous temp file and inserts into
    the new file would race with inserts still in flight on the old
    one — which manifests as ``UNIQUE`` violations.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    # Initialise the schema
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    # Patch the path the service modules read from. We patch the
    # underlying module so any module that called ``from backend.database
    # import get_db_context`` etc. (which only imports the *name*, not the
    # value) will still resolve the new path because ``get_db`` looks
    # ``DATABASE_PATH`` up at call time.
    import backend.database

    monkeypatch.setattr(backend.database, "DATABASE_PATH", path)

    # Reset the singleton DatabasePool so that any cached connections
    # pointing at an earlier test's temp file are closed and dropped.
    try:
        from backend.utils.db_pool import get_pool

        pool = get_pool()
        pool.close_all()
    except Exception:
        pass

    # Reset the usage-window cache that the production database module
    # keeps in-process — otherwise it leaks between tests.
    try:
        backend.database._usage_windows_cache.clear()
    except Exception:
        pass

    yield path

    # Tear-down: close any pool connections pointing at the temp file
    # *before* the file itself goes away, otherwise SQLite will hold
    # a phantom descriptor and the next test's pool entry may reopen
    # a missing path on Windows.
    try:
        from backend.utils.db_pool import get_pool

        pool = get_pool()
        pool.close_all()
    except Exception:
        pass

    try:
        backend.database._usage_windows_cache.clear()
    except Exception:
        pass

    try:
        os.unlink(path)
    except OSError:
        pass
