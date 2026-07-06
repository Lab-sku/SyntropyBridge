from __future__ import annotations

"""Usage aggregation service.

All aggregations are pushed down to SQLite so that timezone handling stays
consistent with the rest of the codebase (server local time, ISO strings).
"""

import csv
import io
from typing import Dict, List, Optional

from backend.database import get_db_context


def _query(sql: str, params: tuple = ()) -> List[Dict]:
    """Run a SELECT and return rows as a list of plain dicts."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_user_daily_usage(user_id: int, days: int = 30) -> List[Dict]:
    """Daily aggregation for the past ``days`` days.

    Returns a list of ``{date, tokens, cost, requests}`` ordered by date asc.
    Days without activity are simply omitted.
    """
    if days <= 0:
        return []
    sql = """
        SELECT strftime('%Y-%m-%d', request_time) AS d,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY strftime('%Y-%m-%d', request_time)
        ORDER BY d ASC
    """
    rows = _query(sql, (user_id, f"-{days} days"))
    return [
        {
            "date": r["d"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def get_user_monthly_usage(user_id: int, months: int = 12) -> List[Dict]:
    """Monthly aggregation using ``strftime('%Y-%m', request_time)``."""
    if months <= 0:
        return []
    sql = """
        SELECT strftime('%Y-%m', request_time) AS m,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY m
        ORDER BY m ASC
    """
    rows = _query(sql, (user_id, f"-{months} months"))
    return [
        {
            "month": r["m"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def get_user_model_breakdown(user_id: int, days: int = 30) -> List[Dict]:
    """Per-model usage, ordered by tokens desc."""
    if days <= 0:
        return []
    sql = """
        SELECT COALESCE(model, 'unknown') AS model,
               COALESCE(provider, 'unknown') AS provider,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY model
        ORDER BY tokens DESC
    """
    rows = _query(sql, (user_id, f"-{days} days"))
    return [
        {
            "model": r["model"],
            "provider": r["provider"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def get_user_provider_breakdown(user_id: int, days: int = 30) -> List[Dict]:
    """Per-provider usage, ordered by tokens desc."""
    if days <= 0:
        return []
    sql = """
        SELECT COALESCE(provider, 'unknown') AS provider,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ?
          AND request_time > datetime('now', ?)
        GROUP BY provider
        ORDER BY tokens DESC
    """
    rows = _query(sql, (user_id, f"-{days} days"))
    return [
        {
            "provider": r["provider"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def _window_summary(user_id: int, condition: str) -> Dict:
    """Build a ``{tokens, cost, requests}`` dict for a window.

    ``condition`` is appended verbatim to a WHERE clause (e.g.
    ``"request_time > datetime('now', '-1 days')"``).
    """
    sql = f"""
        SELECT COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE user_id = ? AND {condition}
    """
    rows = _query(sql, (user_id,))
    if not rows:
        return {"tokens": 0, "cost": 0.0, "requests": 0}
    r = rows[0]
    return {
        "tokens": int(r["tokens"] or 0),
        "cost": float(r["cost"] or 0),
        "requests": int(r["requests"] or 0),
    }


def get_user_summary(user_id: int) -> Dict:
    """Return usage in four windows: today, this week, this month, all-time.

    The 'week' is a rolling 7-day window; the 'month' is a rolling 30-day
    window. This is more useful for billing than calendar windows and is
    what every quota check uses.
    """
    return {
        "today": _window_summary(user_id, "date(request_time) = date('now')"),
        "this_week": _window_summary(user_id, "request_time > datetime('now', '-7 days')"),
        "this_month": _window_summary(user_id, "request_time > datetime('now', '-30 days')"),
        "all_time": _window_summary(user_id, "1=1"),
    }


def get_top_models(limit: int = 10, days: int = 30) -> List[Dict]:
    """Top N models platform-wide, ordered by tokens desc."""
    if limit <= 0 or days <= 0:
        return []
    sql = """
        SELECT COALESCE(model, 'unknown') AS model,
               COALESCE(provider, 'unknown') AS provider,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COALESCE(SUM(cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs
        WHERE request_time > datetime('now', ?)
        GROUP BY model
        ORDER BY tokens DESC
        LIMIT ?
    """
    rows = _query(sql, (f"-{days} days", limit))
    return [
        {
            "model": r["model"],
            "provider": r["provider"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def get_top_users(limit: int = 10, days: int = 30) -> List[Dict]:
    """Top N users by tokens consumed, platform-wide."""
    if limit <= 0 or days <= 0:
        return []
    sql = """
        SELECT u.id AS user_id,
               u.username,
               COALESCE(SUM(l.total_tokens), 0) AS tokens,
               COALESCE(SUM(l.cost_credits), 0) AS cost,
               COUNT(*) AS requests
        FROM usage_logs l
        JOIN users u ON u.id = l.user_id
        WHERE l.request_time > datetime('now', ?)
        GROUP BY l.user_id
        ORDER BY tokens DESC
        LIMIT ?
    """
    rows = _query(sql, (f"-{days} days", limit))
    return [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def get_trend(days: int = 30) -> List[Dict]:
    """Platform-wide daily trend."""
    if days <= 0:
        return []
    sql = """
        SELECT strftime('%Y-%m-%d', request_time) AS d,
               COALESCE(SUM(total_tokens), 0) AS tokens,
               COUNT(*) AS requests,
               COALESCE(SUM(cost_credits), 0) AS cost
        FROM usage_logs
        WHERE request_time > datetime('now', ?)
        GROUP BY strftime('%Y-%m-%d', request_time)
        ORDER BY d ASC
    """
    rows = _query(sql, (f"-{days} days",))
    return [
        {
            "date": r["d"],
            "tokens": int(r["tokens"] or 0),
            "requests": int(r["requests"] or 0),
            "cost": float(r["cost"] or 0),
        }
        for r in rows
    ]


def get_recent_logs(
    limit: int = 50,
    user_id: Optional[int] = None,
    status_code: Optional[int] = None,
) -> List[Dict]:
    """Return recent usage log rows, optionally filtered.

    Joins the users table so the consumer gets ``username`` directly.
    """
    clauses = []
    params: List = []
    if user_id is not None:
        clauses.append("l.user_id = ?")
        params.append(user_id)
    if status_code is not None:
        clauses.append("l.status_code = ?")
        params.append(status_code)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    sql = f"""
        SELECT l.*, u.username
        FROM usage_logs l
        LEFT JOIN users u ON u.id = l.user_id
        {where}
        ORDER BY l.request_time DESC
        LIMIT ?
    """
    return _query(sql, tuple(params))


def export_csv(user_id: Optional[int] = None, days: int = 30) -> str:
    """Return a CSV string of usage rows.

    Includes a small set of columns suitable for finance-style exports.
    """
    clauses = ["request_time > datetime('now', ?)"]
    params: List = [f"-{days} days"]
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    where = "WHERE " + " AND ".join(clauses)

    sql = f"""
        SELECT l.request_time,
               l.user_id,
               u.username,
               l.api_key_id,
               l.endpoint,
               l.model,
               l.provider,
               l.prompt_tokens,
               l.completion_tokens,
               l.total_tokens,
               l.cost_credits,
               l.response_time_ms,
               l.status_code,
               l.ip_address
        FROM usage_logs l
        LEFT JOIN users u ON u.id = l.user_id
        {where}
        ORDER BY l.request_time DESC
    """
    rows = _query(sql, tuple(params))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "request_time",
            "user_id",
            "username",
            "api_key_id",
            "endpoint",
            "model",
            "provider",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cost_credits",
            "response_time_ms",
            "status_code",
            "ip_address",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("request_time"),
                r.get("user_id"),
                r.get("username"),
                r.get("api_key_id"),
                r.get("endpoint"),
                r.get("model"),
                r.get("provider"),
                r.get("prompt_tokens"),
                r.get("completion_tokens"),
                r.get("total_tokens"),
                r.get("cost_credits"),
                r.get("response_time_ms"),
                r.get("status_code"),
                r.get("ip_address"),
            ]
        )
    return buf.getvalue()
