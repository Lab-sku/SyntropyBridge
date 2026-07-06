from __future__ import annotations

"""Admin statistics & operations endpoints.

All routes require either:
* the modern session cookie ``mm_admin_session`` (set by
  ``/api/admin/login`` — what the SPA uses), or
* the legacy ``Authorization: Bearer <jwt>`` header (still supported).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from backend.database import get_db_context
from backend.routes.admin_auth import _admin_guard
from backend.services import usage_service
from backend.services.health_service import (
    get_all_providers_health,
    get_provider_health,
)

router = APIRouter()


def _scalar(conn, sql: str, params: tuple = ()):
    cursor = conn.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/admin/stats/overview")
async def stats_overview(_: None = Depends(_admin_guard)):
    with get_db_context() as conn:
        conn.cursor()
        total_users = _scalar(conn, "SELECT COUNT(*) FROM users") or 0
        active_24h = (
            _scalar(
                conn,
                """
            SELECT COUNT(DISTINCT user_id) FROM usage_logs
            WHERE request_time > datetime('now', '-1 day')
            """,
            )
            or 0
        )
        total_requests_today = (
            _scalar(
                conn,
                """
            SELECT COUNT(*) FROM usage_logs
            WHERE date(request_time) = date('now')
            """,
            )
            or 0
        )
        total_tokens_today = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs
            WHERE date(request_time) = date('now')
            """,
            )
            or 0
        )
        total_cost_today = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(cost_credits), 0) FROM usage_logs
            WHERE date(request_time) = date('now')
            """,
            )
            or 0
        )
        total_revenue = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(amount), 0) FROM orders
            WHERE status = 'paid'
            """,
            )
            or 0
        )
        # Promo bonus credits granted via promo codes (type='bonus'
        # with related_type='promo_code'). These are promotional
        # giveaways that reduce the platform's effective net revenue.
        promo_bonus_credits = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions
            WHERE type = 'bonus' AND related_type = 'promo_code'
            """,
            )
            or 0
        )
        # Credits granted via redeem codes (type='redeem'). These are
        # pre-paid giveaways that aren't recouped by order revenue.
        redeem_credits = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions
            WHERE type = 'redeem'
            """,
            )
            or 0
        )
        # Upstream cost estimate: total credits consumed by usage,
        # scaled by 0.7 to reverse the platform's default 30% markup.
        # This gives a rough sense of how much we're paying upstream.
        upstream_cost_estimate = (
            _scalar(
                conn,
                """
            SELECT COALESCE(SUM(cost_credits), 0) * 0.7 FROM usage_logs
            """,
            )
            or 0
        )
    gross_revenue = float(total_revenue)
    promo_bonus = float(promo_bonus_credits)
    redeem_total = float(redeem_credits)
    net_revenue = gross_revenue - promo_bonus - redeem_total
    return {
        "total_users": int(total_users),
        "active_users_24h": int(active_24h),
        "total_requests_today": int(total_requests_today),
        "total_tokens_today": int(total_tokens_today),
        "total_cost_today": float(total_cost_today),
        "total_revenue": gross_revenue,
        # Net-revenue breakdown (Phase-3 audit): lets the dashboard
        # show how much of gross_revenue was eaten by promotional
        # giveaways / redeem codes, plus a rough upstream cost view.
        "gross_revenue": gross_revenue,
        "promo_bonus_credits": promo_bonus,
        "redeem_credits": redeem_total,
        "net_revenue": net_revenue,
        "upstream_cost_estimate": float(upstream_cost_estimate),
    }


@router.get("/admin/stats/trend")
async def stats_trend(
    days: int = Query(30, ge=1, le=365),
    _: None = Depends(_admin_guard),
):
    return usage_service.get_trend(days)


@router.get("/admin/stats/top-models")
async def stats_top_models(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=100),
    _: None = Depends(_admin_guard),
):
    return usage_service.get_top_models(limit=limit, days=days)


@router.get("/admin/stats/top-users")
async def stats_top_users(
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=100),
    _: None = Depends(_admin_guard),
):
    return usage_service.get_top_users(limit=limit, days=days)


@router.get("/admin/stats/by-provider")
async def stats_by_provider(
    days: int = Query(30, ge=1, le=365),
    _: None = Depends(_admin_guard),
):
    """Platform-wide provider breakdown (admin view)."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COALESCE(provider, 'unknown') AS provider,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_credits), 0) AS cost,
                   COUNT(*) AS requests
            FROM usage_logs
            WHERE request_time > datetime('now', ?)
            GROUP BY provider
            ORDER BY tokens DESC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]


@router.get("/admin/stats/revenue")
async def stats_revenue(
    days: int = Query(30, ge=1, le=365),
    _: None = Depends(_admin_guard),
):
    """Daily revenue based on paid orders."""
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date(paid_at) AS d,
                   COALESCE(SUM(amount), 0) AS revenue,
                   COUNT(*) AS orders
            FROM orders
            WHERE status = 'paid'
              AND paid_at > datetime('now', ?)
            GROUP BY d
            ORDER BY d ASC
        """,
            (f"-{days} days",),
        )
        return [dict(row) for row in cursor.fetchall()]


@router.get("/admin/stats/reconciliation-summary")
async def stats_reconciliation_summary(
    days: int = Query(7, ge=1, le=90),
    _: None = Depends(_admin_guard),
):
    """Stripe reconciliation anomaly summary for the dashboard KPI.

    Returns counts of orders / audit events that need human attention:

    - ``pending_review``: orders currently stuck in ``pending_review``
      status (amount mismatch routed by the recon worker). Live count,
      not windowed — these need an admin decision regardless of age.
    - ``orphans``: paid Stripe sessions with no matching local order,
      logged via ``stripe_recon.orphan`` audit actions in the last
      ``days`` days.
    - ``late_payments``: Stripe captured the charge after our local
      expiry window closed, logged via ``stripe_recon.late_payment``
      audit actions in the last ``days`` days.

    The dashboard surfaces the total so the operator can drill into
    /admin/orders?status=pending_review to clear the backlog.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        pending_review = _scalar(
            conn,
            "SELECT COUNT(*) FROM orders WHERE status = 'pending_review'",
        ) or 0
        orphans = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'stripe_recon.orphan'
              AND created_at > datetime('now', ?)
            """,
            (f"-{days} days",),
        ) or 0
        late_payments = _scalar(
            conn,
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'stripe_recon.late_payment'
              AND created_at > datetime('now', ?)
            """,
            (f"-{days} days",),
        ) or 0
    total = int(pending_review) + int(orphans) + int(late_payments)
    return {
        "pending_review": int(pending_review),
        "orphans": int(orphans),
        "late_payments": int(late_payments),
        "total": total,
        "days": int(days),
    }


@router.get("/admin/recent-logs")
async def admin_recent_logs(
    limit: int = Query(50, ge=1, le=500),
    status_code: Optional[int] = Query(None, alias="status"),
    user_id: Optional[int] = Query(None),
    _: None = Depends(_admin_guard),
):
    return usage_service.get_recent_logs(limit=limit, user_id=user_id, status_code=status_code)


@router.get("/admin/health/providers")
async def admin_providers_health(_: None = Depends(_admin_guard)):
    return get_all_providers_health()


@router.get("/admin/health/providers/{name}")
async def admin_provider_health(name: str, _: None = Depends(_admin_guard)):
    health = get_provider_health(name)
    if not health:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return health


@router.get("/admin/usage/export", response_class=PlainTextResponse)
async def admin_usage_export(
    days: int = Query(30, ge=1, le=365),
    _: None = Depends(_admin_guard),
):
    csv_text = usage_service.export_csv(days=days)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=platform_usage_{days}d.csv"},
    )
