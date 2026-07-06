"""Audit log helpers.

The :class:`audit_logs` table stores everything an admin or user does
that has a financial / security impact (approvals, plan changes, manual
wallet adjustments, redeem code consumption, etc.).

This module is intentionally tiny — it's just a thin wrapper around
SQL to keep the call sites readable.

Schema note
-----------
The :class:`audit_logs` column for the JSON payload is called
``metadata`` (mirroring the canonical schema in
``backend/database._migration_1_baseline`` and
``database.add_audit_log``). Calling it ``details`` here would 500
every admin action that hits :func:`log_action`, so we keep the names
in sync.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from backend.database import get_db


def log_action(
    actor_id: Optional[int],
    actor_type: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Persist a single audit_logs row.

    ``details`` is JSON-encoded so callers can pass arbitrary context.
    """
    if not actor_type:
        actor_type = "system"
    if not action:
        raise ValueError("action 不能为空")
    payload = json.dumps(details, ensure_ascii=False) if details else None
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO audit_logs
                (actor_id, actor_type, action, target_type, target_id,
                 metadata, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                int(actor_id) if actor_id is not None else None,
                str(actor_type)[:20],
                str(action)[:50],
                str(target_type)[:20] if target_type else None,
                int(target_id) if target_id is not None else None,
                payload,
                ip_address,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_logs(
    actor_id: Optional[int] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return recent audit log rows, newest first.

    Supports optional filters: ``actor_id``, ``action``, ``target_type``,
    ``target_id``, and ISO-format date range (``date_from`` / ``date_to``).
    """
    clauses = []
    params: List[Any] = []
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(int(actor_id))
    if action:
        clauses.append("action = ?")
        params.append(action)
    if target_type:
        clauses.append("target_type = ?")
        params.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        params.append(int(target_id))
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT id, actor_id, actor_type, action, target_type, target_id,
               metadata, ip_address, created_at
        FROM audit_logs {where}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    params.extend([int(limit), int(offset)])
    conn = get_db()
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    try:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        # Decode the metadata column back to a dict for convenience.
        # Kept under the ``details`` key so the existing route / SPA
        # contract doesn't change.
        for r in rows:
            raw = r.pop("metadata", None) if isinstance(r, dict) else None
            if raw:
                try:
                    r["details"] = json.loads(raw)
                except (TypeError, ValueError):
                    r["details"] = None
            else:
                r["details"] = None
        return rows
    finally:
        conn.close()


def purge_old_audit_logs(days: int = 365) -> int:
    """Delete ``audit_logs`` rows older than ``days`` days.

    Backed by :func:`backend.database.sweep_old_audit_logs` to avoid
    duplicating the SQL. Returns the number of rows deleted. The
    ``audit_logs.created_at`` column already has ``idx_audit_logs_time``
    so the DELETE is index-backed.

    Called from :func:`SubscriptionService.run_daily_jobs` via
    ``Config.AUDIT_RETENTION_DAYS`` to keep the table (and backups) from
    growing without bound. Default 365 days satisfies typical compliance
    lookback needs.
    """
    from backend.database import sweep_old_audit_logs

    try:
        return int(sweep_old_audit_logs(int(days)))
    except Exception:
        # Don't let a transient DB error take down the daily worker.
        # The next run will retry.
        import logging

        logging.getLogger(__name__).exception(
            "purge_old_audit_logs failed for days=%s", days
        )
        return 0
