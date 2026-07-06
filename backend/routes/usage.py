from __future__ import annotations

"""User-facing usage / quota endpoints.

Authentication matches :mod:`backend.routes.proxy`: callers pass their
personal API key via the ``Authorization: Bearer`` header.
"""


from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse

from backend.services import usage_service
from backend.services.quota_service import check_user_quota
from backend.services.user_service import UserService

router = APIRouter()


async def get_current_user(authorization: str = Header("")):
    """Resolve the user from ``Authorization: Bearer <api_key>``.

    Mirrors the helper in ``backend.routes.proxy`` but is local so we
    don't import the proxy module (which pulls in heavy providers).
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization 头")
    token = authorization.replace("Bearer ", "").strip()
    user = UserService.get_user_by_api_key(token)
    if not user:
        raise HTTPException(status_code=401, detail="无效的 API Key")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return user


@router.get("/user/usage/summary")
async def usage_summary(user=Depends(get_current_user)):
    return usage_service.get_user_summary(user.id)


@router.get("/user/usage/daily")
async def usage_daily(
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user),
):
    return usage_service.get_user_daily_usage(user.id, days)


@router.get("/user/usage/monthly")
async def usage_monthly(
    months: int = Query(12, ge=1, le=36),
    user=Depends(get_current_user),
):
    return usage_service.get_user_monthly_usage(user.id, months)


@router.get("/user/usage/by-model")
async def usage_by_model(
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user),
):
    return usage_service.get_user_model_breakdown(user.id, days)


@router.get("/user/usage/by-provider")
async def usage_by_provider(
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user),
):
    return usage_service.get_user_provider_breakdown(user.id, days)


@router.get("/user/usage/export", response_class=PlainTextResponse)
async def usage_export(
    days: int = Query(30, ge=1, le=365),
    user=Depends(get_current_user),
):
    csv_text = usage_service.export_csv(user_id=user.id, days=days)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_{user.id}.csv"},
    )


@router.get("/user/quota")
async def user_quota(user=Depends(get_current_user)):
    """Expose the live quota snapshot for the dashboard."""
    result = check_user_quota(user.id)
    return result.to_dict()
