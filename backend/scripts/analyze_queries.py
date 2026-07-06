#!/usr/bin/env python3
"""Analyze hot queries with EXPLAIN QUERY PLAN.

Usage:
    python backend/scripts/analyze_queries.py
    DATABASE_PATH=/path/to/production.db python backend/scripts/analyze_queries.py

Connects to the database (default ``./minimax_proxy.db``, overridable via
the ``DATABASE_PATH`` environment variable), runs ``EXPLAIN QUERY PLAN``
on every hot query the platform is known to execute, and flags any plan
that contains ``SCAN TABLE`` (full table scan) or ``TEMP B-TREE``
(filesort / on-disk grouping).

The script never mutates data -- it only reads query plans.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Hot queries collected from the codebase (services, routes, admin panels).
# Each tuple is (label, sql, params).  Parameter placeholders use ``?``
# exactly as the production code does.
# ---------------------------------------------------------------------------

HOT_QUERIES: list[tuple[str, str, tuple]] = [
    # -- user_service.get_usage_chart ----------------------------------------
    (
        "user_service.get_usage_chart (daily chart, 30d)",
        """
        SELECT date(request_time) AS date,
               COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost_credits
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY date
        ORDER BY date ASC
        """,
        (1, "-30 days"),
    ),
    # -- user_service.get_usage_by_model -------------------------------------
    (
        "user_service.get_usage_by_model (model breakdown, 30d)",
        """
        SELECT COALESCE(model, 'unknown') AS model,
               COALESCE(provider, 'unknown') AS provider,
               COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost_credits
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY model, provider
        ORDER BY tokens DESC
        """,
        (1, "-30 days"),
    ),
    # -- user_service.get_user_stats ----------------------------------------
    (
        "user_service.get_user_stats (lifetime totals)",
        """
        SELECT
            COUNT(*) as total_requests,
            COALESCE(SUM(total_tokens), 0) as total_tokens,
            COALESCE(AVG(response_time_ms), 0) as avg_response_time,
            SUM(CASE WHEN status_code = 200 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate
        FROM usage_logs
        WHERE user_id = ?
        """,
        (1,),
    ),
    # -- database.get_quota_snapshot: monthly cost ---------------------------
    (
        "quota_snapshot.monthly_cost (SUM cost_credits, 30d)",
        """
        SELECT COALESCE(SUM(cost_credits), 0) FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', '-30 days')
        """,
        (1,),
    ),
    # -- database.get_quota_snapshot: TPM counter ----------------------------
    (
        "quota_snapshot.tpm_used (SUM total_tokens, 1 min)",
        """
        SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', '-1 minute')
        """,
        (1,),
    ),
    # -- database.get_quota_snapshot: tokens_5h fallback ---------------------
    (
        "quota_snapshot.tokens_5h_fallback (SUM total_tokens, 5h)",
        """
        SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', '-5 hours')
        """,
        (1,),
    ),
    # -- database.get_quota_snapshot: tokens_week fallback -------------------
    (
        "quota_snapshot.tokens_week_fallback (SUM total_tokens, 7d)",
        """
        SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', '-7 days')
        """,
        (1,),
    ),
    # -- database.get_quota_snapshot: rpm_count fallback ---------------------
    (
        "quota_snapshot.rpm_count_fallback (COUNT, 1 min)",
        """
        SELECT COUNT(*) FROM usage_logs
        WHERE user_id = ? AND request_time > datetime('now', '-1 minute')
        """,
        (1,),
    ),
    # -- proxy._bill_latest_usage -------------------------------------------
    (
        "proxy._bill_latest_usage (last unbilled row, 5s window)",
        """
        SELECT id FROM usage_logs
        WHERE user_id = ? AND endpoint = ? AND model = ?
          AND status_code = 200
          AND (cost_credits IS NULL OR cost_credits = 0)
          AND request_time > datetime('now', '-5 seconds')
        ORDER BY id DESC LIMIT 1
        """,
        (1, "/v1/chat", "gpt-4o"),
    ),
    # -- billing_service idempotency check -----------------------------------
    (
        "billing.idempotency_check (wallet_tx dedupe)",
        """
        SELECT 1 FROM wallet_transactions
        WHERE user_id = ?
          AND type = 'consume'
          AND related_type = 'usage'
          AND related_id = ?
        LIMIT 1
        """,
        (1, 1),
    ),
    # -- auth_service._monthly_tokens_used -----------------------------------
    (
        "auth_service._monthly_tokens_used",
        """
        SELECT COALESCE(SUM(total_tokens), 0) AS used
        FROM usage_logs
        WHERE user_id = ?
          AND strftime('%Y-%m', request_time) = strftime('%Y-%m', 'now')
        """,
        (1,),
    ),
    # -- auth_service._monthly_usage_credits ---------------------------------
    (
        "auth_service._monthly_usage_credits",
        """
        SELECT COALESCE(SUM(cost_credits), 0) AS used
        FROM usage_logs
        WHERE user_id = ?
          AND strftime('%Y-%m', request_time) = strftime('%Y-%m', 'now')
        """,
        (1,),
    ),
    # -- usage_service.get_user_daily_usage ----------------------------------
    (
        "usage_service.get_user_daily_usage (30d)",
        """
        SELECT date(request_time) AS d,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY d
        ORDER BY d ASC
        """,
        (1, "-30 days"),
    ),
    # -- usage_service.get_user_monthly_usage --------------------------------
    (
        "usage_service.get_user_monthly_usage (12 months)",
        """
        SELECT strftime('%Y-%m', request_time) AS m,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY m
        ORDER BY m ASC
        """,
        (1, "-12 months"),
    ),
    # -- usage_service per-provider breakdown --------------------------------
    (
        "usage_service.by_provider (30d)",
        """
        SELECT COALESCE(provider, 'unknown') AS provider,
               COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY provider
        ORDER BY tokens DESC
        """,
        (1, "-30 days"),
    ),
    # -- usage_service.get_platform_top_models (admin) -----------------------
    (
        "usage_service.platform_top_models (admin, no user filter)",
        """
        SELECT COALESCE(model, 'unknown') AS model,
               COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost
        FROM usage_logs
        WHERE request_time > datetime('now', ?)
        GROUP BY model
        ORDER BY tokens DESC
        LIMIT ?
        """,
        ("-7 days", 20),
    ),
    # -- usage_service.get_platform_top_users (admin) ------------------------
    (
        "usage_service.platform_top_users (admin, no user filter)",
        """
        SELECT l.user_id, u.username,
               COUNT(*) AS requests,
               COALESCE(SUM(l.total_tokens), 0) AS tokens,
               COALESCE(SUM(l.cost_credits), 0) AS cost
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        WHERE l.request_time > datetime('now', ?)
        GROUP BY l.user_id
        ORDER BY tokens DESC
        LIMIT ?
        """,
        ("-7 days", 20),
    ),
    # -- usage_service.get_platform_daily_trend (admin) ----------------------
    (
        "usage_service.platform_daily_trend (admin, no user filter)",
        """
        SELECT date(request_time) AS d,
               COUNT(*) AS requests,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost
        FROM usage_logs
        WHERE request_time > datetime('now', ?)
        GROUP BY d
        ORDER BY d ASC
        """,
        ("-30 days",),
    ),
    # -- admin_stats: active users 24h ---------------------------------------
    (
        "admin_stats.active_users_24h",
        """
        SELECT COUNT(DISTINCT user_id) FROM usage_logs
        WHERE request_time > datetime('now', '-1 day')
        """,
        (),
    ),
    # -- admin_stats: total requests today -----------------------------------
    (
        "admin_stats.total_requests_today",
        """
        SELECT COUNT(*) FROM usage_logs
        WHERE date(request_time) = date('now')
        """,
        (),
    ),
    # -- admin_stats: total tokens today -------------------------------------
    (
        "admin_stats.total_tokens_today",
        """
        SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
        WHERE date(request_time) = date('now')
        """,
        (),
    ),
    # -- admin_stats: total cost today ---------------------------------------
    (
        "admin_stats.total_cost_today",
        """
        SELECT COALESCE(SUM(cost_credits), 0) FROM usage_logs
        WHERE date(request_time) = date('now')
        """,
        (),
    ),
    # -- admin_stats: total revenue ------------------------------------------
    (
        "admin_stats.total_revenue (orders status=paid)",
        """
        SELECT COALESCE(SUM(amount), 0) FROM orders
        WHERE status = 'paid'
        """,
        (),
    ),
    # -- notification_service.list_notifications -----------------------------
    (
        "notification_service.list (unread, limit 50)",
        """
        SELECT * FROM notifications
        WHERE user_id = ? AND is_read = 0
        ORDER BY id DESC LIMIT ?
        """,
        (1, 50),
    ),
    # -- notification_service.unread_count -----------------------------------
    (
        "notification_service.unread_count",
        """
        SELECT COUNT(*) FROM notifications
        WHERE user_id = ? AND is_read = 0
        """,
        (1,),
    ),
    # -- billing_service: low balance notification dedupe --------------------
    (
        "billing.low_balance_notif_dedupe",
        """
        SELECT MAX(created_at) FROM notifications
        WHERE user_id = ? AND type = 'low_balance'
        """,
        (1,),
    ),
    # -- admin_billing: wallet transactions by user --------------------------
    (
        "admin_billing.wallet_tx_by_user",
        """
        SELECT * FROM wallet_transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (1, 50),
    ),
    # -- admin_billing: orders export (filtered by status) -------------------
    (
        "admin_billing.orders_export (status filter)",
        """
        SELECT * FROM orders
        WHERE status = ?
        ORDER BY id ASC
        """,
        ("paid",),
    ),
    # -- usage_service.export_logs_admin (CSV export) ------------------------
    (
        "usage_service.export_logs_admin (filtered, paginated)",
        """
        SELECT l.*, u.username
        FROM usage_logs l
        LEFT JOIN users u ON u.id = l.user_id
        WHERE l.user_id = ? AND l.status_code = ?
          AND l.request_time >= ?
        ORDER BY l.request_time DESC
        LIMIT ?
        """,
        (1, 200, "2025-01-01", 1000),
    ),
]


# Patterns that indicate a problematic query plan
_BAD_PATTERNS = re.compile(r"SCAN TABLE|SCAN (?!.*USING.*INDEX)|TEMP B-TREE", re.IGNORECASE)


def _explain(conn: sqlite3.Connection, sql: str, params: tuple) -> list[str]:
    """Run EXPLAIN QUERY PLAN and return the detail strings."""
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    except sqlite3.OperationalError as exc:
        return [f"<error: {exc}>"]
    # EXPLAIN QUERY PLAN returns rows with columns (id, parent, notused, detail)
    return [row[-1] if isinstance(row[-1], str) else str(row[-1]) for row in rows]


def main() -> None:
    db_path = os.getenv("DATABASE_PATH", "")
    if not db_path:
        db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "minimax_proxy.db",
        )

    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        print("Set DATABASE_PATH env var or run from the repo root.")
        sys.exit(1)

    conn = sqlite3.connect(db_path, timeout=30)
    # Use the same pragmas as production for accurate plans
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-20000")

    flagged = 0
    total = len(HOT_QUERIES)

    print(f"Analyzing {total} hot queries against {db_path}\n")
    print("=" * 72)

    for label, sql, params in HOT_QUERIES:
        plan_lines = _explain(conn, sql, params)
        plan_text = "\n    ".join(plan_lines)

        is_bad = any(_BAD_PATTERNS.search(line) for line in plan_lines)
        status = "SLOW" if is_bad else "OK"

        if is_bad:
            flagged += 1
            print(f"\n[{status}] {label}")
            print(f"    {plan_text}")
            for line in plan_lines:
                if _BAD_PATTERNS.search(line):
                    print(f"    >>> FLAGGED: {line.strip()}")
        else:
            print(f"\n[{status}] {label}")
            print(f"    {plan_text}")

    print("\n" + "=" * 72)
    print(f"Summary: {flagged}/{total} queries flagged")
    if flagged:
        print("Hint: run migration 22 to add covering indexes for flagged queries.")
    else:
        print("All hot queries use index lookups. No action needed.")

    conn.close()


if __name__ == "__main__":
    main()
