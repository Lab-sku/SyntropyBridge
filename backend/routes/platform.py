"""Custom provider and subscription management routes."""

from __future__ import annotations

import logging
from typing import List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.config import Config
from backend.database import get_db_context
from backend.providers.base import ProviderRegistry
from backend.routes.admin_auth import _require_admin, _require_admin_csrf
from backend.routes.billing import require_user_csrf
from backend.services import custom_providers
from backend.services.model_aggregator import (
    aggregate_models,
)
from backend.session import (
    USER_SESSION_COOKIE,
    get_session as get_server_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ============== user auth helper ==============


def _require_user(request: Request) -> int:
    """Resolve the calling user id from either the session cookie or
    a legacy JWT bearer token. Returns the integer user id."""
    # 1. Session cookie path.
    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        sess = get_server_session(user_session_id, user_agent=request.headers.get("User-Agent"))
        if sess and sess.get("role") == "user" and sess.get("user_id"):
            return int(sess["user_id"])
    # 2. Legacy JWT.
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        raise HTTPException(status_code=401, detail="未授权")
    try:
        payload = jwt.decode(token, Config.SECRET_KEY, algorithms=[Config.JWT_ALGORITHM])
    except Exception:
        raise HTTPException(status_code=401, detail="无效的Token")
    sub = payload.get("sub")
    if sub == "admin" or not str(sub).isdigit():
        raise HTTPException(status_code=401, detail="请使用用户Token")
    return int(sub)


# ============== Custom providers ==============


class CustomProviderCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    display_name: Optional[str] = None
    api_base: str
    api_key: Optional[str] = None
    api_keys: Optional[List[str]] = None
    notes: Optional[str] = ""


class CustomProviderUpdate(BaseModel):
    display_name: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    api_keys: Optional[List[str]] = None
    notes: Optional[str] = None
    is_enabled: Optional[bool] = None


@router.get("/custom-providers")
async def list_custom(request: Request):
    _require_admin(request)
    return custom_providers.list_custom_providers()


@router.post("/custom-providers")
async def create_custom(body: CustomProviderCreate, request: Request):
    _require_admin_csrf(request)
    try:
        cfg = custom_providers.create_custom_provider(
            name=body.name,
            api_base=body.api_base,
            api_key=body.api_key or "",
            slug=body.slug,
            display_name=body.display_name,
            api_keys=body.api_keys,
            notes=body.notes or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    custom_providers.get_provider_class(cfg["slug"])
    return {"message": "已添加", "provider": cfg}


@router.put("/custom-providers/{slug}")
async def update_custom(slug: str, body: CustomProviderUpdate, request: Request):
    _require_admin_csrf(request)
    cfg = custom_providers.update_custom_provider(
        slug,
        display_name=body.display_name,
        api_base=body.api_base,
        api_key=body.api_key,
        api_keys=body.api_keys,
        notes=body.notes,
        is_enabled=body.is_enabled,
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="自定义平台不存在")
    custom_providers.get_provider_class(slug)
    return {"message": "已更新", "provider": cfg}


@router.delete("/custom-providers/{slug}")
async def delete_custom(slug: str, request: Request):
    _require_admin_csrf(request)
    if not custom_providers.delete_custom_provider(slug):
        raise HTTPException(status_code=404, detail="自定义平台不存在")
    ProviderRegistry._providers.pop(slug, None)  # type: ignore
    return {"message": "已删除"}


@router.post("/custom-providers/{slug}/test")
async def test_custom(slug: str, request: Request):
    _require_admin_csrf(request)
    return await custom_providers.test_custom_provider(slug)


@router.get("/custom-providers/{slug}/models")
async def custom_models(slug: str, request: Request, refresh: bool = Query(False)):
    _require_admin(request)
    if refresh:
        rows = await custom_providers.fetch_custom_models(slug, force=True)
    else:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT model_id, display_name, context_length, is_active AS enabled FROM models WHERE provider = ? ORDER BY display_name",
                (f"custom:{slug}",),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        if not rows:
            rows = await custom_providers.fetch_custom_models(slug)
    return {"slug": slug, "models": rows}


@router.post("/custom-providers/refresh-all")
async def refresh_all_custom(request: Request):
    """Refresh model lists for every enabled custom provider."""
    _require_admin_csrf(request)
    all_cfg = custom_providers.list_custom_providers(include_disabled=False)
    results = []
    total = 0
    for cfg in all_cfg:
        try:
            rows = await custom_providers.fetch_custom_models(cfg["slug"])
            total += len(rows)
            results.append({"slug": cfg["slug"], "count": len(rows), "ok": True})
        except Exception as e:
            logger.exception("Failed to refresh custom provider %s", cfg["slug"])
            results.append({"slug": cfg["slug"], "ok": False, "error": str(e)})
    return {
        "message": f"已刷新 {len(all_cfg)} 个自定义平台，共 {total} 个模型",
        "results": results,
        "total": total,
    }


# ============== Global aggregation ==============


@router.get("/providers/aggregate")
async def aggregate_all_providers(request: Request, refresh: bool = Query(False)):
    """Aggregate models from every provider: built-in + custom + openrouter."""
    _require_admin(request)
    custom_providers.ensure_all_registered()
    payload = await aggregate_models(force=refresh)
    return payload


# ============== Subscriptions ==============


class SubscriptionRequestBody(BaseModel):
    provider: str
    model_id: Optional[str] = None
    requested_quota_5h: Optional[int] = None
    requested_quota_week: Optional[int] = None
    note: Optional[str] = None


@router.post("/user/subscriptions", dependencies=[Depends(require_user_csrf)])
async def create_subscription_request(body: SubscriptionRequestBody, request: Request):
    """User-facing: ask for access to a specific platform or model."""
    user_id = _require_user(request)
    if body.provider not in ProviderRegistry.all() and not body.provider.startswith("custom:"):
        # Allow custom providers too, but only if they are configured
        if body.provider.startswith("custom:"):
            slug = body.provider.split(":", 1)[1]
            if not custom_providers.get_custom_provider(slug):
                raise HTTPException(status_code=404, detail="平台不存在")
        else:
            raise HTTPException(status_code=400, detail="不支持的平台")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO subscription_requests
                (user_id, provider, model_id, requested_quota_5h, requested_quota_week, note, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                user_id,
                body.provider,
                body.model_id,
                body.requested_quota_5h,
                body.requested_quota_week,
                body.note,
            ),
        )
        rid = cursor.lastrowid
    return {"message": "订阅申请已提交，等待管理员审批", "id": rid}


@router.get("/user/subscriptions")
async def list_my_subscriptions(request: Request):
    user_id = _require_user(request)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, provider, model_id, status, requested_quota_5h, requested_quota_week,
                   admin_note, created_at, reviewed_at
            FROM subscription_requests
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in cursor.fetchall()]


@router.get("/admin/subscriptions")
async def list_all_subscriptions(request: Request, status: Optional[str] = None):
    _require_admin(request)
    with get_db_context() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute(
                """
                SELECT s.*, u.username FROM subscription_requests s
                JOIN users u ON s.user_id = u.id
                WHERE s.status = ?
                ORDER BY s.created_at DESC
                """,
                (status,),
            )
        else:
            cursor.execute(
                """
                SELECT s.*, u.username FROM subscription_requests s
                JOIN users u ON s.user_id = u.id
                ORDER BY s.created_at DESC
                """
            )
        return [dict(r) for r in cursor.fetchall()]


class ReviewSubscriptionBody(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    admin_note: Optional[str] = None
    grant_quota_5h: Optional[int] = None
    grant_quota_week: Optional[int] = None


@router.post("/admin/subscriptions/{req_id}/review")
async def review_subscription(req_id: int, body: ReviewSubscriptionBody, request: Request):
    _require_admin_csrf(request)
    if body.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="状态无效")
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM subscription_requests WHERE id = ?", (req_id,))
        req = cursor.fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="申请不存在")
        if req["status"] != "pending":
            raise HTTPException(status_code=400, detail="申请已被处理")

        cursor.execute(
            """
            UPDATE subscription_requests
            SET status = ?, admin_note = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (body.status, body.admin_note, req_id),
        )

        if body.status == "approved":
            # ``user_model_access`` is the canonical per-user access
            # table (declared in migration #8). Inserting here grants
            # the user permission to call the requested (provider,
            # model) tuple without being rate-limited by the
            # allow-list default-deny path. We use ``INSERT OR
            # IGNORE`` so a re-approval is a no-op rather than a
            # unique-constraint error.
            cursor.execute(
                """
                INSERT OR IGNORE INTO user_model_access
                    (user_id, model_id, access_type, granted_at)
                VALUES (?, ?, 'allow', CURRENT_TIMESTAMP)
                """,
                (
                    int(req["user_id"]),
                    f"{req['provider']}/{req['model_id']}" if req["model_id"] else req["provider"],
                ),
            )
            if body.grant_quota_5h is not None or body.grant_quota_week is not None:
                updates = []
                params: list = []
                if body.grant_quota_5h is not None:
                    updates.append("quota_5h = MAX(quota_5h, ?)")
                    params.append(int(body.grant_quota_5h))
                if body.grant_quota_week is not None:
                    updates.append("quota_week = MAX(quota_week, ?)")
                    params.append(int(body.grant_quota_week))
                if updates:
                    params.append(int(req["user_id"]))
                    cursor.execute(
                        f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                        params,
                    )
    return {"message": "已审批", "id": req_id, "status": body.status}


# ============== User stats / billing ==============


@router.get("/user/billing")
async def user_billing(request: Request, days: int = Query(30, ge=1, le=365)):
    user_id = _require_user(request)
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT provider, model_id, SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   COUNT(*) AS calls
            FROM billing_records
            WHERE user_id = ? AND request_time > datetime('now', ?)
            GROUP BY provider, model_id
            ORDER BY total_tokens DESC
            """,
            (user_id, f"-{days} days"),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            """
            SELECT
              SUM(total_tokens) AS tokens,
              COUNT(*) AS calls
            FROM billing_records
            WHERE user_id = ? AND request_time > datetime('now', ?)
            """,
            (user_id, f"-{days} days"),
        )
        total = dict(cursor.fetchone())
    return {"period_days": days, "total": total, "by_model": rows}


@router.get("/admin/billing/overview")
async def billing_overview(request: Request):
    _require_admin(request)
    # NOTE: an earlier version of this endpoint read from a
    # ``billing_records`` table that was never actually created — the
    # real usage history lives in ``usage_logs``. Reading from the
    # canonical table keeps the dashboard working on a fresh install.
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(total_tokens), 0) AS total_tokens,
              COUNT(*) AS calls
            FROM usage_logs
            WHERE request_time > datetime('now', '-7 days')
            """
        )
        week = dict(cursor.fetchone())
        cursor.execute(
            """
            SELECT model AS provider, SUM(total_tokens) AS tokens, COUNT(*) AS calls
            FROM usage_logs
            WHERE request_time > datetime('now', '-7 days')
            GROUP BY model
            ORDER BY tokens DESC
            """
        )
        by_provider = [dict(r) for r in cursor.fetchall()]
        cursor.execute(
            """
            SELECT u.username, u.id AS user_id,
                   COALESCE(SUM(b.total_tokens), 0) AS tokens,
                   COUNT(b.id) AS calls
            FROM users u
            LEFT JOIN usage_logs b
              ON b.user_id = u.id AND b.request_time > datetime('now', '-7 days')
            GROUP BY u.id
            HAVING calls > 0
            ORDER BY tokens DESC
            LIMIT 20
            """
        )
        top_users = [dict(r) for r in cursor.fetchall()]
    return {"week": week, "by_provider": by_provider, "top_users": top_users}
