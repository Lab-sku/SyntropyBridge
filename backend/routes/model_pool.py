"""User-facing model pool management routes.

Lets a user plug in their own upstream credentials (API key + base URL
+ model name) and expose them through a single ``sk-ump_…`` key on the
OpenAI-compatible gateway.  All state-changing endpoints require a
valid user session + CSRF token (mirrors the pattern in
``backend/routes/user.py``).  Admins cannot see another user's pool —
the credentials are encrypted at rest and only the owner can decrypt.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.routes.user import require_user_csrf, require_user_session
from backend.services.model_pool_service import ModelPoolService

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreatePoolRequest(BaseModel):
    name: str
    provider_type: str
    api_base: str
    api_key: str
    model_name: str
    priority: int = 0
    max_tokens: int = 0


class UpdatePoolRequest(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    priority: Optional[int] = None
    max_tokens: Optional[int] = None
    is_active: Optional[bool] = None


class ReorderRequest(BaseModel):
    ordered_ids: List[int]


class GenerateKeyRequest(BaseModel):
    name: Optional[str] = None


# ---------------------------------------------------------------------------
# Pool CRUD
# ---------------------------------------------------------------------------


def _user_id_from_session(session: dict) -> int:
    uid = session.get("user_id")
    if uid is None:
        raise HTTPException(status_code=401, detail="会话缺少用户标识")
    return int(uid)


@router.post("/user/model-pools", dependencies=[Depends(require_user_csrf)])
async def create_model_pool(
    payload: CreatePoolRequest,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    try:
        pool_id = ModelPoolService.create_pool(
            user_id=user_id,
            name=payload.name,
            provider_type=payload.provider_type,
            api_base=payload.api_base,
            api_key=payload.api_key,
            model_name=payload.model_name,
            priority=payload.priority,
            max_tokens=payload.max_tokens,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": pool_id, "message": "创建成功"}


@router.get("/user/model-pools")
async def list_model_pools(session: dict = Depends(require_user_session)):
    user_id = _user_id_from_session(session)
    pools = ModelPoolService.list_pools(user_id)
    return {"count": len(pools), "items": pools}


@router.put(
    "/user/model-pools/{pool_id}",
    dependencies=[Depends(require_user_csrf)],
)
async def update_model_pool(
    pool_id: int,
    payload: UpdatePoolRequest,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    fields: Dict[str, Any] = payload.model_dump(exclude_unset=True)
    try:
        updated = ModelPoolService.update_pool(pool_id, user_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail="模型池不存在或无字段更新")
    return {"message": "更新成功"}


@router.delete(
    "/user/model-pools/{pool_id}",
    dependencies=[Depends(require_user_csrf)],
)
async def delete_model_pool(
    pool_id: int,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    deleted = ModelPoolService.delete_pool(pool_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="模型池不存在")
    return {"message": "删除成功"}


@router.post(
    "/user/model-pools/reorder",
    dependencies=[Depends(require_user_csrf)],
)
async def reorder_model_pools(
    payload: ReorderRequest,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    updated = ModelPoolService.reorder_pools(user_id, payload.ordered_ids)
    return {"updated": updated}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


@router.post("/user/model-pool-keys", dependencies=[Depends(require_user_csrf)])
async def generate_model_pool_key(
    payload: GenerateKeyRequest,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    display_key = ModelPoolService.generate_key(user_id, name=payload.name)
    return {"key": display_key, "message": "请妥善保存，此密钥仅显示一次"}


@router.get("/user/model-pool-keys")
async def list_model_pool_keys(session: dict = Depends(require_user_session)):
    user_id = _user_id_from_session(session)
    keys = ModelPoolService.list_keys(user_id)
    return {"count": len(keys), "items": keys}


@router.delete(
    "/user/model-pool-keys/{key_id}",
    dependencies=[Depends(require_user_csrf)],
)
async def delete_model_pool_key(
    key_id: int,
    session: dict = Depends(require_user_session),
):
    user_id = _user_id_from_session(session)
    deleted = ModelPoolService.delete_key(key_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"message": "删除成功"}
