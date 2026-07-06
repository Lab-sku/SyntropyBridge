"""User-facing billing / wallet / subscription API.

All routes require a user API key (``Authorization: Bearer <api_key>``).
The same auth pattern is used by the legacy proxy router so the wallet
endpoints sit naturally next to it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from types import SimpleNamespace
from typing import Any, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from backend.config import Config
from backend.database import check_rate_limit, get_db_context, get_wallet
from backend.services import order_service
from backend.services.audit import log_action
from backend.services.user_service import UserService
from backend.session import CSRF_COOKIE

logger = logging.getLogger(__name__)

router = APIRouter()

# Synthetic admin id used when the payment gateway auto-approves orders
# via webhook. Using a negative number avoids colliding with real admin ids.
SYSTEM_ADMIN_ID = -1


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


async def get_current_user(
    request: Request,
    authorization: str = Header(""),
    x_api_key: str = Header(default=""),
):
    """Resolve the caller via user API key OR session cookie.

    - API-key path (Authorization: Bearer <key> or X-API-Key header):
      resolves to the user row. Used by OpenAI-compatible clients and
      programmatic callers.
    - Session-cookie path (mm_session or mm_admin_session): resolves
      to the browser user, or to a synthetic admin sentinel
      (id=SYSTEM_ADMIN_ID, is_admin=True) when the caller is an admin
      with no user session. This lets the admin Subscriptions page
      read user-scoped data (wallet, plans, current subscription)
      without tripping the frontend's aggressive 401 → logout handler.

    The session fallback is strictly second-class: an explicit API key
    always wins, so existing API clients see identical behavior.
    """
    raw = ""
    if authorization:
        raw = authorization.replace("Bearer ", "").strip()
    elif x_api_key:
        raw = x_api_key.strip()

    if raw:
        user = UserService.get_user_by_api_key(raw)
        if not user:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        return user

    # --- Session-cookie fallback for browser callers ---
    from backend.session import (
        ADMIN_SESSION_COOKIE,
        USER_SESSION_COOKIE,
        get_session,
    )

    ua = request.headers.get("User-Agent")

    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        s = get_session(user_session_id, user_agent=ua)
        if s and s.get("role") == "user":
            uid = int(s.get("user_id"))
            u = UserService.get_user(uid)
            if u and u.is_active:
                return u

    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        s = get_session(admin_session_id, user_agent=ua)
        if s and s.get("role") == "admin":
            return SimpleNamespace(
                id=SYSTEM_ADMIN_ID,
                is_active=True,
                is_admin=True,
                username=s.get("username"),
                email=None,
            )

    raise HTTPException(status_code=401, detail="未提供 API Key 或有效会话")


async def get_current_user_for_write(
    request: Request,
    authorization: str = Header(""),
    x_api_key: str = Header(default=""),
):
    """Resolve the caller via user API key OR session cookie for WRITE operations.

    Unlike the original design, this now supports session-cookie auth
    for regular users so that browser-based users can perform write
    operations (subscribe, top-up, redeem, etc.) without needing an
    explicit API key.

    Admin sessions still cannot perform write operations directly —
    they must use a separate user API key or log in as a regular user.
    """
    raw = ""
    if authorization:
        raw = authorization.replace("Bearer ", "").strip()
    elif x_api_key:
        raw = x_api_key.strip()

    # --- API Key path ---
    if raw:
        user = UserService.get_user_by_api_key(raw)
        if not user:
            raise HTTPException(status_code=401, detail="无效的 API Key")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被禁用")
        return user

    # --- Session-cookie fallback for browser callers ---
    from backend.session import (
        ADMIN_SESSION_COOKIE,
        USER_SESSION_COOKIE,
        get_session,
    )

    ua = request.headers.get("User-Agent")

    # Regular user session — allowed for write operations
    user_session_id = request.cookies.get(USER_SESSION_COOKIE)
    if user_session_id:
        s = get_session(user_session_id, user_agent=ua)
        if s and s.get("role") == "user":
            uid = int(s.get("user_id"))
            u = UserService.get_user(uid)
            if u and u.is_active:
                return u

    # Admin session — NOT allowed for write operations (must use user API key)
    admin_session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    if admin_session_id:
        s = get_session(admin_session_id, user_agent=ua)
        if s and s.get("role") == "admin":
            raise HTTPException(
                status_code=403,
                detail="管理员会话不能执行写操作，请使用用户 API Key 或以普通用户身份登录"
            )

    raise HTTPException(status_code=401, detail="未提供 API Key 或有效会话")


async def require_user_csrf(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default=""),
) -> None:
    """CSRF gate for write endpoints.

    API-key-authenticated callers (Authorization: Bearer … or
    X-API-Key header) are inherently CSRF-safe — the key lives in a
    header that browsers never send automatically — so they skip this
    check entirely.

    Session-cookie-authenticated callers (the browser path) MUST send
    back the ``X-CSRF-Token`` header with the value of the ``mm_csrf``
    cookie. Both directions are compared with ``hmac.compare_digest``
    (timing-safe) against the cookie and the server-side session csrf
    token, mirroring the existing guard in ``backend/routes/user.py``.

    Wire into a route alongside ``get_current_user_for_write``::

        @router.post(
            "/user/orders",
            dependencies=[Depends(require_user_csrf)],
        )
        async def create_order(user=Depends(get_current_user_for_write)): ...
    """
    if (authorization or "").strip() or (x_api_key or "").strip():
        return  # API-key caller — no CSRF check needed

    header_token = (request.headers.get("X-CSRF-Token") or "").strip()
    cookie_token = (request.cookies.get(CSRF_COOKIE) or "").strip()
    if not header_token or not cookie_token:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    if not hmac.compare_digest(header_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")

    # Belt-and-suspenders: also compare against the server-side session
    # csrf value, so a stolen mm_csrf cookie alone (without a valid
    # session) cannot pass the check.
    from backend.session import USER_SESSION_COOKIE, get_session

    session_id = request.cookies.get(USER_SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
    session = get_session(session_id, user_agent=request.headers.get("User-Agent"))
    session_csrf = (session or {}).get("csrf") or ""
    if not session_csrf or not hmac.compare_digest(header_token, session_csrf):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    amount: float
    payment_method: str = "admin_grant"
    promo_code: Optional[str] = None


class RedeemRequest(BaseModel):
    code: str


class SubscribeRequest(BaseModel):
    plan_id: int
    auto_renew: bool = True
    payment_method: str = "balance"  # balance, alipay, wechat


class PlanChangeRequest(BaseModel):
    new_plan_id: int


class AutoRechargeRequest(BaseModel):
    enabled: bool
    threshold: Optional[float] = None
    amount: Optional[float] = None


class CreateApiKeyRequest(BaseModel):
    name: str
    monthly_token_limit: Optional[int] = None
    monthly_credit_limit: Optional[float] = None
    allowed_models: Optional[List[str]] = None
    allowed_ips: Optional[str] = None
    expires_at: Optional[str] = None


class UpdateApiKeyRequest(BaseModel):
    name: Optional[str] = None
    monthly_token_limit: Optional[int] = None
    monthly_credit_limit: Optional[float] = None
    allowed_models: Optional[List[str]] = None
    allowed_ips: Optional[str] = None
    expires_at: Optional[str] = None
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------


@router.get("/user/wallet")
async def get_my_wallet(user=Depends(get_current_user)):
    w = get_wallet(int(user.id))

    # Attach current subscription info so the frontend can highlight
    # the active plan card without an extra round-trip.
    plan_name = None
    plan_code = None
    plan_expires_at = None
    try:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT p.name, p.code, s.expires_at
                FROM subscriptions s
                LEFT JOIN plans p ON p.id = s.plan_id
                WHERE s.user_id = ? AND s.status = 'active'
                ORDER BY s.id DESC LIMIT 1
                """,
                (int(user.id),),
            )
            row = cursor.fetchone()
            if row:
                plan_name = row["name"]
                plan_code = row["code"]
                plan_expires_at = row["expires_at"]
    except Exception:
        # M2: 不再静默吞错——best-effort 语义保留，但记录告警便于排查。
        logger.warning(
            "failed to load subscription context for wallet user_id=%s",
            int(user.id),
            exc_info=True,
        )

    return {
        "balance": float(w.get("balance") or 0),
        "total_recharged": float(w.get("total_recharged") or 0),
        "total_consumed": float(w.get("total_consumed") or 0),
        "frozen": float(w.get("frozen") or 0),
        "auto_recharge_enabled": bool(w.get("auto_recharge_enabled") or 0),
        "plan_name": plan_name,
        "plan_code": plan_code,
        "plan_expires_at": plan_expires_at,
    }


@router.get("/user/wallet/transactions")
async def list_wallet_transactions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, type, amount, balance_after, related_type, related_id,
                   note, created_at
            FROM wallet_transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """,
            (int(user.id), int(limit), int(offset)),
        )
        return [dict(r) for r in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


@router.get("/user/orders")
async def list_my_orders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    return order_service.list_orders(
        user_id=int(user.id),
        limit=int(limit),
        offset=int(offset),
    )


@router.post("/user/orders", dependencies=[Depends(require_user_csrf)])
async def create_my_order(
    payload: CreateOrderRequest,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    try:
        order = order_service.create_order(
            user_id=int(user.id),
            amount=float(payload.amount),
            payment_method=payload.payment_method or "admin_grant",
            promo_code=payload.promo_code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="order.create",
        target_type="order",
        target_id=int(order.get("id") or 0),
        details={"amount": payload.amount, "promo": payload.promo_code},
        ip_address=request.client.host if request.client else None,
    )
    return order


@router.get("/user/orders/{order_no}")
async def get_my_order(order_no: str, user=Depends(get_current_user)):
    order = order_service.get_order_by_no(order_no, user_id=int(user.id))
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


# ---------------------------------------------------------------------------
# Redeem
# ---------------------------------------------------------------------------


@router.post("/user/redeem", dependencies=[Depends(require_user_csrf)])
async def redeem_my_code(payload: RedeemRequest, request: Request, user=Depends(get_current_user_for_write)):
    # Rate-limit redeem attempts per user to prevent brute-force guessing.
    client_ip = request.client.host if request.client else "unknown"
    allowed, _ = check_rate_limit(f"redeem:{user.id}", "redeem_user:60", 10, 60)
    if not allowed:
        raise HTTPException(status_code=429, detail="兑换尝试过于频繁，请稍后再试")
    allowed_ip, _ = check_rate_limit(f"redeem_ip:{client_ip}", "redeem_ip:60", 20, 60)
    if not allowed_ip:
        raise HTTPException(status_code=429, detail="兑换尝试过于频繁，请稍后再试")
    try:
        result = order_service.redeem_code(payload.code, int(user.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="redeem.apply",
        target_type="redeem_code",
        target_id=None,
        details={"code": payload.code, "result": result},
        ip_address=request.client.host if request.client else None,
    )
    return result


# ---------------------------------------------------------------------------
# Plans & subscription
# ---------------------------------------------------------------------------


@router.get("/user/plans")
async def list_my_plans(user=Depends(get_current_user)):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, code, monthly_price, monthly_credits, discount_rate,
                   max_api_keys, max_concurrent, rate_limit_rpm, rate_limit_tpm,
                   features, sort_order
            FROM plans WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
        """)
        return [dict(r) for r in cursor.fetchall()]


@router.get("/user/subscription")
async def get_my_subscription(user=Depends(get_current_user)):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT s.*, p.name AS plan_name, p.code AS plan_code
            FROM subscriptions s
            LEFT JOIN plans p ON p.id = s.plan_id
            WHERE s.user_id = ?
            ORDER BY s.id DESC
            LIMIT 1
        """,
            (int(user.id),),
        )
        sub = cursor.fetchone()
        cursor.execute("SELECT plan_id, plan_expires_at FROM users WHERE id = ?", (int(user.id),))
        urow = cursor.fetchone()
    if not sub:
        return {
            "active": False,
            "plan_id": (urow["plan_id"] if urow else None),
            "plan_expires_at": (urow["plan_expires_at"] if urow else None),
        }
    return dict(sub)


@router.post("/user/subscription", dependencies=[Depends(require_user_csrf)])
async def subscribe(payload: SubscribeRequest, request: Request, user=Depends(get_current_user_for_write)):
    """Subscribe to a plan.

    - Free plans (monthly_price = 0): activated immediately.
    - Paid plans with payment_method = "balance": debits wallet and activates.
    - Paid plans with payment_method = "alipay"/"wechat": creates a pending
      order for online payment.
    """
    from backend.services.subscription_service import SubscriptionService

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, code, monthly_price, monthly_credits
            FROM plans WHERE id = ? AND is_active = 1
        """,
            (int(payload.plan_id),),
        )
        plan = cursor.fetchone()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在或已下架")

    price = float(plan["monthly_price"] or 0)

    # --- Free plan: activate immediately ---
    if price <= 0:
        try:
            result = SubscriptionService.upgrade(int(user.id), int(plan["id"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log_action(
            actor_id=int(user.id),
            actor_type="user",
            action="subscription.free_activate",
            target_type="plan",
            target_id=int(plan["id"]),
            details={"plan_name": plan["name"]},
            ip_address=request.client.host if request.client else None,
        )
        return {"subscription": result, "plan": dict(plan), "payment_method": "free"}

    # --- Paid plan: balance payment → debit wallet and activate ---
    if payload.payment_method == "balance":
        try:
            result = SubscriptionService.upgrade(int(user.id), int(plan["id"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log_action(
            actor_id=int(user.id),
            actor_type="user",
            action="subscription.balance_activate",
            target_type="plan",
            target_id=int(plan["id"]),
            details={"plan_name": plan["name"], "amount": price},
            ip_address=request.client.host if request.client else None,
        )
        return {"subscription": result, "plan": dict(plan), "payment_method": "balance"}

    # --- Paid plan: online payment (alipay/wechat) → create order ---
    # P1.7: 在线支付通道提前校验可用性，避免创建订单后用户在
    # /billing/orders/{no}/pay 才发现通道不可用（订单变成无意义
    # pending 订单）。balance / free 路径不需要支付通道，跳过。
    from backend.services.payment import list_providers as _list_payment_providers

    providers_status = _list_payment_providers()
    provider_info = providers_status.get(payload.payment_method)
    if not provider_info:
        raise HTTPException(
            status_code=400, detail=f"不支持的支付方式: {payload.payment_method}"
        )
    if not provider_info.get("available"):
        raise HTTPException(
            status_code=503,
            detail=f"支付通道 {payload.payment_method} 暂不可用: "
                   f"{provider_info.get('error') or '未配置'}",
        )

    try:
        order = order_service.create_order(
            user_id=int(user.id),
            amount=price,
            payment_method=payload.payment_method,
            promo_code=None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Mark the order as a subscription intent by attaching the plan id
    # in the note column (lightweight — no schema change required).
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE orders
            SET note = COALESCE(note, '') || ?
            WHERE id = ?
        """,
            (
                json.dumps({"plan_id": int(plan["id"]), "auto_renew": bool(payload.auto_renew)}),
                int(order["id"]),
            ),
        )
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="subscription.request",
        target_type="order",
        target_id=int(order["id"]),
        details={"plan_id": int(plan["id"]), "payment_method": payload.payment_method},
        ip_address=request.client.host if request.client else None,
    )
    return {"order": order, "plan": dict(plan), "payment_method": payload.payment_method}


# ---------------------------------------------------------------------------
# Subscription lifecycle management
# ---------------------------------------------------------------------------


@router.get("/user/subscriptions/current")
async def get_current_subscription(user=Depends(get_current_user)):
    """Return the user's active subscription with plan details."""
    from backend.services.subscription_service import SubscriptionService

    sub = SubscriptionService.get_active(int(user.id))
    if not sub:
        return {"active": False, "subscription": None}
    return {"active": True, "subscription": sub}


def _assert_subscription_ownership(user_id: int, sub_id: int) -> None:
    """Raise 404 if ``sub_id`` doesn't belong to ``user_id``.

    Defense-in-depth: every subscription lifecycle endpoint accepts a
    ``sub_id`` path parameter for REST ergonomics, but the underlying
    service methods operate on ``user_id`` and find the active sub
    themselves. Without this guard, a caller who passes another user's
    ``sub_id`` would silently operate on their own active sub —
    confusing UX and a latent IDOR if a future refactor wires the
    service to trust the path parameter.
    """
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM subscriptions WHERE id = ?",
            (int(sub_id),),
        )
        row = cursor.fetchone()
    if not row or int(row["user_id"]) != int(user_id):
        raise HTTPException(status_code=404, detail="订阅不存在")


@router.post(
    "/user/subscriptions/{sub_id}/upgrade",
    dependencies=[Depends(require_user_csrf)],
)
async def upgrade_subscription(
    sub_id: int,
    payload: PlanChangeRequest,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    """Prorate and upgrade to a higher-tier plan."""
    from backend.services.subscription_service import SubscriptionService

    _assert_subscription_ownership(user.id, sub_id)

    try:
        result = SubscriptionService.upgrade(int(user.id), int(payload.new_plan_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="subscription.upgrade",
        target_type="subscription",
        target_id=sub_id,
        details={"new_plan_id": payload.new_plan_id},
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.post(
    "/user/subscriptions/{sub_id}/downgrade",
    dependencies=[Depends(require_user_csrf)],
)
async def downgrade_subscription(
    sub_id: int,
    payload: PlanChangeRequest,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    """Schedule a downgrade to a lower-tier plan at period end."""
    from backend.services.subscription_service import SubscriptionService

    _assert_subscription_ownership(user.id, sub_id)

    try:
        result = SubscriptionService.downgrade(int(user.id), int(payload.new_plan_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="subscription.downgrade",
        target_type="subscription",
        target_id=sub_id,
        details={"new_plan_id": payload.new_plan_id},
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.post(
    "/user/subscriptions/{sub_id}/cancel",
    dependencies=[Depends(require_user_csrf)],
)
async def cancel_subscription(
    sub_id: int,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    """Cancel auto-renewal (subscription stays active until expiry)."""
    from backend.services.subscription_service import SubscriptionService

    _assert_subscription_ownership(user.id, sub_id)

    result = SubscriptionService.cancel(int(user.id))
    if not result:
        raise HTTPException(
            status_code=400, detail="Auto-renewal is already off or no active subscription"
        )
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="subscription.cancel",
        target_type="subscription",
        target_id=sub_id,
        ip_address=request.client.host if request.client else None,
    )
    return {
        "cancelled": True,
        "message": "Auto-renewal cancelled. Subscription remains active until expiry.",
    }


@router.post(
    "/user/subscriptions/{sub_id}/renew",
    dependencies=[Depends(require_user_csrf)],
)
async def renew_subscription(
    sub_id: int,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    """Manually renew the subscription for the next period."""
    from backend.services.subscription_service import SubscriptionService

    _assert_subscription_ownership(user.id, sub_id)

    try:
        result = SubscriptionService.renew(int(user.id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="subscription.renew",
        target_type="subscription",
        target_id=sub_id,
        ip_address=request.client.host if request.client else None,
    )
    return result


@router.patch("/user/wallet/auto-recharge", dependencies=[Depends(require_user_csrf)])
async def update_auto_recharge(
    payload: AutoRechargeRequest,
    request: Request,
    user=Depends(get_current_user_for_write),
):
    """Update auto-recharge settings on the user's wallet."""
    updates = ["auto_recharge_enabled = ?"]
    params: List[Any] = [1 if payload.enabled else 0]
    if payload.threshold is not None:
        updates.append("auto_recharge_threshold = ?")
        params.append(float(payload.threshold))
    if payload.amount is not None:
        updates.append("auto_recharge_amount = ?")
        params.append(float(payload.amount))
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(int(user.id))

    with get_db_context() as conn:
        cursor = conn.cursor()
        # P3.8: 原实现先 UPDATE 再 SELECT 判断行是否存在、不存在才 INSERT。
        # 该序列在默认 autocommit 模式下每次 execute 都是独立事务，
        # 两个并发请求可能同时 SELECT 到“不存在”、然后同时 INSERT，
        # 第二个 INSERT 因 user_id PRIMARY KEY 唯一约束失败并 500。
        # 改为 INSERT OR IGNORE 先确保行存在（幂等），再 UPDATE，
        # 消除 TOCTOU 窗口。balance / 总额列保持 0 不影响后续 UPDATE。
        cursor.execute(
            "INSERT OR IGNORE INTO wallets (user_id, balance, auto_recharge_enabled) "
            "VALUES (?, 0, ?)",
            (int(user.id), 1 if payload.enabled else 0),
        )
        cursor.execute(
            f"UPDATE wallets SET {', '.join(updates)} WHERE user_id = ?",
            tuple(params),
        )

    log_action(
        actor_id=int(user.id),
        actor_type="user",
        action="wallet.auto_recharge",
        target_type="wallet",
        target_id=int(user.id),
        details={
            "enabled": payload.enabled,
            "threshold": payload.threshold,
            "amount": payload.amount,
        },
        ip_address=request.client.host if request.client else None,
    )

    # Return updated wallet
    w = get_wallet(int(user.id))
    return {
        "auto_recharge_enabled": bool(w.get("auto_recharge_enabled") or 0),
        "auto_recharge_threshold": float(w.get("auto_recharge_threshold") or 0),
        "auto_recharge_amount": float(w.get("auto_recharge_amount") or 0),
    }


# ---------------------------------------------------------------------------
# API keys (sub-keys)
# ---------------------------------------------------------------------------


def _hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _mask_key(secret: str) -> str:
    if len(secret) < 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


@router.get("/user/api-keys")
async def list_my_api_keys(user=Depends(get_current_user)):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, key_prefix, key_mask, monthly_token_limit,
                   monthly_credit_limit, allowed_models, denied_models,
                   allowed_ips, is_active, last_used_at, expires_at, created_at
            FROM api_keys WHERE user_id = ?
            ORDER BY id DESC
        """,
            (int(user.id),),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    for r in rows:
        # Never leak internal hash columns.
        r.pop("key_hash", None)
    return rows


@router.post("/user/api-keys", dependencies=[Depends(require_user_csrf)])
async def create_my_api_key(payload: CreateApiKeyRequest, user=Depends(get_current_user_for_write)):
    if payload.allowed_ips:
        from ipaddress import ip_network
        for cidr in payload.allowed_ips.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                ip_network(cidr, strict=False)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"无效的 IP/CIDR 格式: {cidr}")

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.max_api_keys
              FROM users u
              LEFT JOIN plans p ON p.id = u.plan_id
             WHERE u.id = ?
        """,
            (int(user.id),),
        )
        row = cursor.fetchone()
        max_keys = int((row["max_api_keys"] if row and row["max_api_keys"] else 1))
        cursor.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id = ? AND is_active = 1", (int(user.id),)
        )
        existing = int(cursor.fetchone()[0] or 0)
    if existing >= max_keys:
        raise HTTPException(
            status_code=403,
            detail=f"当前套餐最多创建 {max_keys} 把 API Key",
        )

    secret = "sk-" + secrets.token_urlsafe(32)
    key_hash = _hash_key(secret)
    key_prefix = secret[:8]
    key_mask = _mask_key(secret)
    allowed_json = json.dumps(payload.allowed_models) if payload.allowed_models else None

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO api_keys
                (user_id, name, key_hash, key_prefix, key_mask,
                 monthly_token_limit, monthly_credit_limit, allowed_models,
                 allowed_ips, is_active, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
            (
                int(user.id),
                payload.name.strip()[:100],
                key_hash,
                key_prefix,
                key_mask,
                payload.monthly_token_limit,
                payload.monthly_credit_limit,
                allowed_json,
                payload.allowed_ips,
                payload.expires_at,
            ),
        )
        new_id = int(cursor.lastrowid)
    return {
        "id": new_id,
        "name": payload.name,
        "api_key": secret,
        "key_prefix": key_prefix,
        "key_mask": key_mask,
        "monthly_token_limit": payload.monthly_token_limit,
        "monthly_credit_limit": payload.monthly_credit_limit,
        "allowed_models": payload.allowed_models,
        "allowed_ips": payload.allowed_ips,
        "expires_at": payload.expires_at,
    }


@router.delete("/user/api-keys/{key_id}", dependencies=[Depends(require_user_csrf)])
async def revoke_my_api_key(key_id: int, user=Depends(get_current_user_for_write)):
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM api_keys WHERE id = ? AND user_id = ?", (int(key_id), int(user.id))
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="API Key 不存在")
        cursor.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (int(key_id),))
    return {"message": "API Key 已撤销"}


@router.patch("/user/api-keys/{key_id}", dependencies=[Depends(require_user_csrf)])
async def update_my_api_key(
    key_id: int, payload: UpdateApiKeyRequest, user=Depends(get_current_user_for_write)
):
    updates = []
    params: List[Any] = []
    if payload.name is not None:
        updates.append("name = ?")
        params.append(payload.name.strip()[:100])
    if payload.monthly_token_limit is not None:
        updates.append("monthly_token_limit = ?")
        params.append(int(payload.monthly_token_limit))
    if payload.monthly_credit_limit is not None:
        updates.append("monthly_credit_limit = ?")
        params.append(float(payload.monthly_credit_limit))
    if payload.allowed_models is not None:
        updates.append("allowed_models = ?")
        params.append(json.dumps(payload.allowed_models))
    if payload.allowed_ips is not None:
        from ipaddress import ip_network
        if payload.allowed_ips:
            for cidr in payload.allowed_ips.split(","):
                cidr = cidr.strip()
                if not cidr:
                    continue
                try:
                    ip_network(cidr, strict=False)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"无效的 IP/CIDR 格式: {cidr}")
        updates.append("allowed_ips = ?")
        params.append(payload.allowed_ips if payload.allowed_ips else None)
    if payload.expires_at is not None:
        updates.append("expires_at = ?")
        params.append(payload.expires_at)
    if payload.is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if payload.is_active else 0)
    if not updates:
        raise HTTPException(status_code=400, detail="没有可更新的字段")
    params.extend([int(key_id), int(user.id)])

    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM api_keys WHERE id = ? AND user_id = ?", (int(key_id), int(user.id))
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="API Key 不存在")
        cursor.execute(
            f"UPDATE api_keys SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            tuple(params),
        )
    return {"message": "已更新"}


# ---------------------------------------------------------------------------
# Payment gateway
# ---------------------------------------------------------------------------


class PayOrderRequest(BaseModel):
    provider: str = "stripe"


@router.get("/billing/providers")
async def list_public_payment_providers():
    """Public listing of payment providers the end user may choose.

    Mirrors the admin ``/admin/payment/providers`` endpoint but:

    * excludes disabled / unavailable providers (the user should not
      see options they cannot pick),
    * hides the SDK ``error`` field (operator-internal diagnostic).

    The frontend ``PaymentDialog`` uses this to render the provider
    selector dynamically instead of hardcoding stripe/alipay/wechat,
    so adding a new provider on the backend automatically surfaces
    in the UI.
    """
    from backend.database import get_setting
    from backend.services.payment import list_providers

    providers = list_providers()
    result = []
    for name, info in providers.items():
        enabled_key = f"payment_provider_{name}_enabled"
        is_enabled_raw = get_setting(enabled_key)
        is_enabled = is_enabled_raw == "1" if is_enabled_raw else info["available"]
        if not is_enabled or not info["available"]:
            continue
        result.append({"name": name, "available": True, "enabled": True})
    return result


@router.post("/billing/orders/{order_no}/pay", dependencies=[Depends(require_user_csrf)])
async def pay_order(order_no: str, payload: PayOrderRequest, user=Depends(get_current_user_for_write)):
    """Create a checkout session with the selected payment provider.

    Returns ``{ "checkout_url": "..." }`` — the frontend should redirect
    the user to that URL.
    """
    from backend.services.payment import get_provider, list_providers

    order = order_service.get_order_by_no(order_no, user_id=int(user.id))
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.get("status") != "pending":
        raise HTTPException(status_code=400, detail="订单状态不允许支付")

    provider_name = (payload.provider or "stripe").lower().strip()

    # P1.7: 前置校验支付通道可用性。stub providers（alipay/wechat）
    # 在 list_providers() 里会标记 available=False，这里直接拒绝，
    # 避免落到 create_checkout 才抛 NotImplementedError（用户拿到
    # 模糊的 503 错误）。同样拒绝未配置 SDK 的 stripe/usdt。
    providers_status = list_providers()
    provider_info = providers_status.get(provider_name)
    if not provider_info:
        raise HTTPException(status_code=400, detail=f"不支持的支付方式: {provider_name}")
    if not provider_info.get("available"):
        raise HTTPException(
            status_code=503,
            detail=f"支付通道 {provider_name} 暂不可用: {provider_info.get('error') or '未配置'}",
        )

    try:
        provider = get_provider(provider_name)
    except KeyError:
        raise HTTPException(status_code=400, detail=f"不支持的支付方式: {provider_name}")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Build the return URL from the request origin
    amount_cny = float(order.get("amount") or 0)
    if provider_name == "usdt":
        # Convert CNY → USDT using the operator-set rate. If no rate
        # is configured we fall back to parity so the checkout is
        # still creatable (the operator can reconcile later).
        rate = float(Config.NOWPAYMENTS_CNY_USDT_RATE or 0.0) or 1.0
        amount_minor = int(round(amount_cny * rate * 100))
        currency = "usdt"
    else:
        amount_minor = int(amount_cny * 100)
        currency = Config.STRIPE_CURRENCY
    description = f"Top-up {int(order.get('credits') or 0)} credits"

    # Construct a return URL — frontend handles the success/cancel states
    base_url = "/wallet"

    try:
        session = await provider.create_checkout(
            order_no=order_no,
            amount_cents=amount_minor,
            currency=currency,
            description=description,
            return_url=base_url,
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Payment provider error: {exc}")

    # Persist the session on the order so the webhook / query can look it up
    with get_db_context() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE orders
            SET payment_session_id = ?, payment_provider = ?
            WHERE order_no = ?
        """,
            (session.session_id, provider_name, order_no),
        )

    return {"checkout_url": session.checkout_url, "session_id": session.session_id}


def _find_order_by_stripe_reference(
    _logger,
    *,
    payment_intent: str = "",
    charge_id: str = "",
    order_no: str = "",
):
    """Look up a local order by any of the Stripe-side identifiers.

    Resolution order:
    1. ``metadata.order_no`` (most reliable — set on every Checkout Session)
    2. ``payment_intent`` → ``orders.payment_reference``
    3. ``charge_id``       → ``orders.payment_reference``
    4. ``payment_intent`` → ``orders.payment_session_id`` (rare; only if
       the checkout webhook hasn't fired yet but the session ID equals
       the PI id, which Stripe doesn't normally do)

    Returns the order dict or ``None``.
    """
    if order_no:
        order = order_service.get_order_by_no(order_no)
        if order:
            return order

    refs = [r for r in (payment_intent, charge_id) if r]
    if not refs:
        return None

    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        for ref in refs:
            cursor.execute(
                "SELECT id FROM orders WHERE payment_reference = ? LIMIT 1",
                (ref,),
            )
            row = cursor.fetchone()
            if row:
                return order_service.get_order(int(row[0]))
    _logger.warning(
        "Stripe webhook: no local order matches payment_intent=%s charge=%s",
        payment_intent, charge_id,
    )
    return None


def _handle_stripe_charge_refunded(_logger, data: dict) -> dict:
    """Process a ``charge.refunded`` event.

    Looks up the local order by ``metadata.order_no`` /
    ``payment_intent`` / charge id and invokes ``refund_order`` so the
    user's credits are debited to mirror the Stripe-side refund. If the
    wallet can't cover the full amount, ``refund_order`` performs a
    partial refund and records the shortfall in the order note.

    Idempotent: if the order is already ``refunded``, returns 200 with
    no side effects.
    """
    payment_intent = str(data.get("payment_intent") or "")
    charge_id = str(data.get("id") or "")
    metadata = data.get("metadata", {}) or {}
    order_no = metadata.get("order_no", "")

    order = _find_order_by_stripe_reference(
        _logger,
        payment_intent=payment_intent,
        charge_id=charge_id,
        order_no=order_no,
    )
    if not order:
        return {"received": True, "event_type": "charge.refunded", "warning": "order not found"}

    if order.get("status") == "refunded":
        _logger.info(
            "Stripe charge.refunded: order %s already refunded, skipping",
            order.get("order_no"),
        )
        return {"received": True, "order_no": order.get("order_no"), "status": "already_refunded"}

    if order.get("status") != "paid":
        _logger.warning(
            "Stripe charge.refunded: order %s in non-paid state %s, skipping",
            order.get("order_no"), order.get("status"),
        )
        return {"received": True, "order_no": order.get("order_no"), "status": order.get("status")}

    refund_details = {
        "stripe_event": "charge.refunded",
        "payment_intent": payment_intent,
        "charge_id": charge_id,
        "amount_refunded": data.get("amount_refunded"),
        "currency": data.get("currency"),
    }
    note = json.dumps(refund_details, ensure_ascii=False)

    # P1.3: Stripe partial refund — when ``amount_refunded`` is less
    # than the original charge ``amount``, only refund the proportional
    # share of credits instead of the full order total. Both figures
    # are in the charge currency's minor units (cents), so the ratio
    # is dimensionless and works regardless of STRIPE_CURRENCY.
    partial_credits: Optional[float] = None
    charge_amount = data.get("amount")
    amount_refunded = data.get("amount_refunded")
    if (
        charge_amount is not None
        and amount_refunded is not None
        and float(charge_amount) > 0
        and float(amount_refunded) < float(charge_amount)
    ):
        ratio = float(amount_refunded) / float(charge_amount)
        order_total_credits = float(order.get("credits") or 0) + float(
            order.get("bonus_credits") or 0
        )
        partial_credits = round(order_total_credits * ratio, 4)

    try:
        ok = order_service.refund_order(
            int(order["id"]),
            admin_id=SYSTEM_ADMIN_ID,
            reason=f"stripe_charge_refunded: {note}",
            partial_credits=partial_credits,
            source="stripe_webhook",
        )
    except ValueError as exc:
        # refund_order may raise ValueError on insufficient balance in
        # legacy paths — log and surface so Stripe retries the webhook.
        _logger.exception(
            "Stripe charge.refunded: refund_order failed for order %s: %s",
            order.get("order_no"), exc,
        )
        return {"received": True, "order_no": order.get("order_no"), "error": str(exc)}

    if ok:
        try:
            from backend.services.audit import log_action

            log_action(
                actor_id=SYSTEM_ADMIN_ID,
                actor_type="system",
                action="stripe_charge_refunded",
                target_type="order",
                target_id=int(order["id"]),
                details=refund_details,
                ip_address=None,
            )
        except Exception:
            _logger.debug(
                "audit log write failed for stripe_charge_refunded order %s",
                order.get("order_no"), exc_info=True,
            )
        _logger.info(
            "Stripe charge.refunded: order %s refunded locally",
            order.get("order_no"),
        )
    return {"received": True, "order_no": order.get("order_no"), "refunded": bool(ok)}


def _handle_stripe_charge_disputed(_logger, data: dict) -> dict:
    """Process a ``charge.dispute.created`` event.

    Marks the local order ``disputed`` so the admin dashboard surfaces
    it for manual handling, writes an audit log, and notifies the user.
    Does NOT auto-refund — disputes require human review (the platform
    may need to submit evidence instead).
    """
    dispute_id = str(data.get("id") or "")
    charge_id = str(data.get("charge") or "")
    payment_intent = str(data.get("payment_intent") or "")
    reason = str(data.get("reason") or "")
    amount = data.get("amount")
    currency = data.get("currency")
    status = str(data.get("status") or "")

    order = _find_order_by_stripe_reference(
        _logger,
        payment_intent=payment_intent,
        charge_id=charge_id,
    )
    if not order:
        return {"received": True, "event_type": "charge.dispute.created", "warning": "order not found"}

    dispute_note = json.dumps({
        "stripe_event": "charge.dispute.created",
        "dispute_id": dispute_id,
        "charge_id": charge_id,
        "payment_intent": payment_intent,
        "reason": reason,
        "amount": amount,
        "currency": currency,
        "status": status,
    }, ensure_ascii=False)

    with get_db_context() as conn:
        conn.row_factory = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.execute(
                "UPDATE orders SET status = 'disputed', note = COALESCE(note, '') || ? WHERE id = ?",
                (dispute_note, int(order["id"])),
            )
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    try:
        from backend.services.audit import log_action

        log_action(
            actor_id=SYSTEM_ADMIN_ID,
            actor_type="system",
            action="stripe_charge_disputed",
            target_type="order",
            target_id=int(order["id"]),
            details={
                "dispute_id": dispute_id,
                "charge_id": charge_id,
                "payment_intent": payment_intent,
                "reason": reason,
                "amount": amount,
                "currency": currency,
                "status": status,
                "order_no": order.get("order_no"),
            },
            ip_address=None,
        )
    except Exception:
        _logger.debug(
            "audit log write failed for stripe_charge_disputed order %s",
            order.get("order_no"), exc_info=True,
        )

    try:
        from backend.services.notification_service import NotificationService

        NotificationService.notify(
            int(order.get("user_id", 0)),
            type="payment_disputed",
            title="订单存在支付争议",
            body=(
                f"您的订单 {order.get('order_no')} 收到一笔 Stripe 争议 "
                f"(原因: {reason or '未知'})。请尽快联系客服处理。"
            ),
            metadata={
                "order_no": order.get("order_no"),
                "dispute_id": dispute_id,
                "reason": reason,
                "amount": amount,
                "currency": currency,
            },
        )
    except Exception:
        _logger.exception(
            "failed to emit dispute notification for order %s",
            order.get("order_no"),
        )

    try:
        from backend.services.alert_service import AlertService

        AlertService.send_alert_sync(
            "CRITICAL",
            f"Stripe dispute created for order {order.get('order_no')}",
            {
                "order_no": order.get("order_no"),
                "dispute_id": dispute_id,
                "reason": reason,
                "amount": amount,
                "currency": currency,
            },
        )
    except Exception:
        _logger.exception(
            "failed to send dispute alert for order %s",
            order.get("order_no"),
        )

    _logger.warning(
        "Stripe charge.dispute.created: order %s marked disputed (reason=%s)",
        order.get("order_no"), reason,
    )
    return {"received": True, "order_no": order.get("order_no"), "status": "disputed"}


def _process_stripe_checkout_completed(_logger, result) -> None:
    """Background-task body: approve / route the order referenced by a
    ``checkout.session.completed`` event.

    Extracted from the route handler so the HTTP response can return
    ``200`` immediately after signature verification — Stripe retries
    webhooks that don't 2xx within ~30s, and ``approve_order`` can
    block on DB writes / notifications.
    """
    session_data = result.raw.get("data", {}) or {}
    metadata = session_data.get("metadata", {}) or {}
    order_no = metadata.get("order_no", "")

    if not order_no:
        _logger.warning("Stripe webhook missing order_no in metadata")
        return

    order = order_service.get_order_by_no(order_no)
    if not order:
        _logger.warning("Stripe webhook: order %s not found", order_no)
        return

    if order.get("status") != "pending":
        # Idempotent: already processed (e.g. duplicate delivery).
        return

    expected_amount = float(order.get("amount") or 0)
    paid_amount = float(result.amount_cents or 0) / 100.0 if result.amount_cents else 0

    # USDT orders are stored in CNY (order.amount) but the webhook
    # pays in USDT. Apply the configured CNY→USDT rate so the
    # mismatch comparison is in the same currency on both sides.
    if (
        order.get("payment_provider") == "usdt"
        and expected_amount > 0
    ):
        rate = float(Config.NOWPAYMENTS_CNY_USDT_RATE or 0.0)
        if rate > 0:
            expected_amount = expected_amount * rate

    # P2.3: 金额容差统一使用 Config.STRIPE_RECON_AMOUNT_TOLERANCE，
    # 避免不同入口（webhook / query / 对账）阈值不一致导致同一笔
    # 支付在一个入口被判 mismatch、在另一个入口被静默放行。
    tolerance = float(getattr(Config, "STRIPE_RECON_AMOUNT_TOLERANCE", 0.01) or 0.01)
    if paid_amount > 0 and expected_amount > 0 and abs(paid_amount - expected_amount) > tolerance:
        shortfall = expected_amount - paid_amount
        _logger.critical(
            "Stripe webhook AMOUNT MISMATCH: order %s expected %.2f, paid %.2f (shortfall=%.2f)",
            order_no,
            expected_amount,
            paid_amount,
            shortfall,
        )
        mismatch_note = json.dumps({
            "amount_mismatch": True,
            "expected": expected_amount,
            "paid": paid_amount,
            "shortfall": shortfall,
        })
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE orders SET status = 'pending_review', note = COALESCE(note, '') || ? WHERE id = ?",
                    (mismatch_note, int(order["id"])),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
        try:
            from backend.services.notification_service import NotificationService
            NotificationService.notify(
                int(order.get("user_id", 0)),
                type="payment_amount_mismatch",
                title="Payment Amount Mismatch - CRITICAL",
                body=f"Order {order_no}: expected {expected_amount:.2f}, paid {paid_amount:.2f}. "
                     f"Order set to pending_review for admin reconciliation.",
                metadata={
                    "order_no": order_no,
                    "expected": expected_amount,
                    "paid": paid_amount,
                    "shortfall": shortfall,
                    "severity": "critical",
                },
            )
        except Exception:
            _logger.exception("failed to emit mismatch notification for %s", order_no)
        try:
            from backend.services.alert_service import AlertService

            AlertService.send_alert_sync(
                "CRITICAL",
                f"Stripe payment amount mismatch for order {order_no}",
                {
                    "order_no": order_no,
                    "expected": expected_amount,
                    "paid": paid_amount,
                    "shortfall": shortfall,
                },
            )
        except Exception:
            _logger.exception("failed to send mismatch alert for %s", order_no)
        _logger.info(
            "Stripe webhook: order %s set to pending_review due to amount mismatch",
            order_no,
        )
        return

    ok = order_service.approve_order(int(order["id"]), admin_id=SYSTEM_ADMIN_ID)
    if ok:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE orders
                SET payment_reference = ?
                WHERE id = ?
            """,
                (result.provider_reference, int(order["id"])),
            )
        _logger.info(
            "Stripe webhook: order %s approved (ref=%s)",
            order_no,
            result.provider_reference,
        )


def _process_stripe_event(_logger, event_type: str, data: dict, result) -> None:
    """Background-task entry point: dispatch a verified Stripe event.

    Pulled out of the route handler so the HTTP response can return
    ``200`` immediately after signature verification. Each branch is
    idempotent — duplicate Stripe deliveries are a normal occurrence.
    """
    if event_type == "charge.refunded":
        _handle_stripe_charge_refunded(_logger, data)
        return
    if event_type == "charge.dispute.created":
        _handle_stripe_charge_disputed(_logger, data)
        return
    if event_type == "checkout.session.completed":
        if result.status != "succeeded":
            return
        _process_stripe_checkout_completed(_logger, result)
        return
    # Other event types are acknowledged without processing.


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Stripe webhook callbacks (no authentication).

    On ``checkout.session.completed``, looks up the order by
    ``metadata.order_no`` and auto-approves it. Idempotent: if the
    order is already paid (e.g. a duplicate webhook delivery), the
    background task is a no-op.

    Also handles:
    - ``charge.refunded`` → debit the user's credits via ``refund_order``
      so the platform doesn't eat the cost of already-consumed credits.
    - ``charge.dispute.created`` → mark the order ``disputed`` and
      notify the user + audit log.

    Signature verification runs synchronously so a bad signature still
    returns 400; once verified, the business logic is dispatched to
    ``BackgroundTasks`` so Stripe gets a 200 within its retry window
    even when ``approve_order`` blocks on DB writes or notifications.
    On signature failure the original payload (truncated to 4 KB) is
    written to ``audit_logs`` for forensic review.
    """
    from backend.services.payment import get_provider

    try:
        provider = get_provider("stripe")
    except (KeyError, RuntimeError) as exc:
        logger.error("Stripe provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Stripe not configured")

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        result = await provider.verify_webhook(payload=payload, signature=signature)
    except ValueError as exc:
        logger.warning("Stripe webhook signature verification failed: %s", exc)
        try:
            payload_preview = payload.decode("utf-8", errors="ignore")[:4096]
            log_action(
                actor_id=None,
                actor_type="system",
                action="webhook_signature_failed",
                target_type="webhook",
                target_id=None,
                details={
                    "provider": "stripe",
                    "payload_preview": payload_preview,
                    "error": str(exc),
                },
                ip_address=request.client.host if request.client else None,
            )
        except Exception:
            logger.exception("failed to write webhook_signature_failed audit log")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = result.raw.get("event_type", "")
    data = result.raw.get("data", {}) or {}

    # Dispatch business logic to the background so we can 200 immediately.
    background_tasks.add_task(_process_stripe_event, logger, event_type, data, result)
    return {"received": True, "event_type": event_type}


def _process_usdt_event(_logger, result) -> None:
    """Background-task body: approve / route the order referenced by a
    NOWPayments IPN.

    Extracted from the route handler so the HTTP response can return
    ``200`` immediately after signature verification. Idempotent: a
    duplicate IPN delivery sees the order already in ``paid`` state
    and is a no-op.
    """
    # P0.2: NOWPayments reports ``partially_paid`` as a dedicated
    # ``partial`` status (see usdt_provider._STATUS_MAP). Route the
    # order to ``pending_review`` via ``handle_partial_payment`` so
    # the admin reconciliation console surfaces it and the user gets
    # a notification. Without this branch the helper was dead code.
    if result.status == "partial":
        order_no = (result.raw or {}).get("order_no") or ""
        if not order_no:
            _logger.warning("USDT partial_payment webhook missing order_id")
            return
        order = order_service.get_order_by_no(order_no)
        if not order:
            _logger.warning("USDT partial_payment webhook: order %s not found", order_no)
            return
        if order.get("status") != "pending":
            # Already transitioned (paid / pending_review / etc.) — no-op.
            return
        paid_amount = float(result.amount_cents or 0) / 100.0 if result.amount_cents else 0
        try:
            order_service.handle_partial_payment(int(order["id"]), paid_amount)
        except Exception:
            _logger.exception(
                "handle_partial_payment failed for order %s", order_no
            )
        return

    if result.status != "succeeded":
        # Pending / failed: nothing to do.
        return

    order_no = (result.raw or {}).get("order_no") or ""
    if not order_no:
        _logger.warning("USDT webhook missing order_id")
        return

    order = order_service.get_order_by_no(order_no)
    if not order:
        _logger.warning("USDT webhook: order %s not found", order_no)
        return

    if order.get("status") != "pending":
        return

    expected_amount = float(order.get("amount") or 0)
    paid_amount = float(result.amount_cents or 0) / 100.0 if result.amount_cents else 0

    # Apply CNY→USDT rate so the mismatch check compares USDT to USDT.
    if expected_amount > 0:
        rate = float(Config.NOWPAYMENTS_CNY_USDT_RATE or 0.0)
        if rate > 0:
            expected_amount = expected_amount * rate

    tolerance = float(getattr(Config, "STRIPE_RECON_AMOUNT_TOLERANCE", 0.01) or 0.01)
    if paid_amount > 0 and expected_amount > 0 and abs(paid_amount - expected_amount) > tolerance:
        shortfall = expected_amount - paid_amount
        _logger.critical(
            "USDT webhook AMOUNT MISMATCH: order %s expected %.4f, paid %.4f (shortfall=%.4f)",
            order_no,
            expected_amount,
            paid_amount,
            shortfall,
        )
        mismatch_note = json.dumps({
            "amount_mismatch": True,
            "expected": expected_amount,
            "paid": paid_amount,
            "shortfall": shortfall,
            "currency": "usdt",
        })
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE orders SET status = 'pending_review', note = COALESCE(note, '') || ? WHERE id = ?",
                    (mismatch_note, int(order["id"])),
                )
                cursor.execute("COMMIT")
            except Exception:
                cursor.execute("ROLLBACK")
                raise
        return

    ok = order_service.approve_order(int(order["id"]), admin_id=None)
    if ok:
        with get_db_context() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE orders
                SET payment_reference = ?, payment_provider = 'usdt'
                WHERE id = ?
            """,
                (result.provider_reference, int(order["id"])),
            )
        _logger.info(
            "USDT webhook: order %s approved (ref=%s)",
            order_no,
            result.provider_reference,
        )


@router.post("/webhooks/usdt")
async def usdt_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive NOWPayments IPN callbacks (no authentication on the
    endpoint itself — the IPN HMAC signature carried in
    ``x-nowpayments-sig`` is the authentication).

    On a terminal ``finished`` / ``paid`` event, looks up the order by
    ``order_id`` (we set it to the internal ``order_no`` when creating
    the payment) and auto-approves it. Idempotent: a duplicate IPN
    delivery sees the order already in ``paid`` state and is
    acknowledged without side effects.

    Signature verification runs synchronously so a bad signature still
    returns 400; once verified, the business logic is dispatched to
    ``BackgroundTasks`` so NOWPayments gets a 200 within its retry
    window. On signature failure the original payload (truncated to
    4 KB) is written to ``audit_logs`` for forensic review.
    """
    from backend.services.payment import get_provider

    try:
        provider = get_provider("usdt")
    except (KeyError, RuntimeError) as exc:
        logger.error("USDT provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="USDT not configured")

    payload = await request.body()
    signature = (
        request.headers.get("x-nowpayments-sig")
        or request.headers.get("X-Nowpayments-Sig")
        or ""
    )

    try:
        result = await provider.verify_webhook(payload=payload, signature=signature)
    except ValueError as exc:
        logger.warning("USDT webhook signature verification failed: %s", exc)
        try:
            payload_preview = payload.decode("utf-8", errors="ignore")[:4096]
            log_action(
                actor_id=None,
                actor_type="system",
                action="webhook_signature_failed",
                target_type="webhook",
                target_id=None,
                details={
                    "provider": "usdt",
                    "payload_preview": payload_preview,
                    "error": str(exc),
                },
                ip_address=request.client.host if request.client else None,
            )
        except Exception:
            logger.exception("failed to write webhook_signature_failed audit log")
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Dispatch business logic to the background so we can 200 immediately.
    background_tasks.add_task(_process_usdt_event, logger, result)
    return {"received": True, "status": result.status}


@router.post("/billing/orders/{order_no}/query", dependencies=[Depends(require_user_csrf)])
async def query_order_payment(order_no: str, user=Depends(get_current_user)):
    """Poll the payment provider for the current status of an order.

    If the provider reports ``succeeded`` and the order is still
    pending, auto-approve it. Returns the order with the latest status.
    """
    import logging as _logging

    _logger = _logging.getLogger(__name__)

    from backend.services.payment import get_provider

    order = order_service.get_order_by_no(order_no, user_id=int(user.id))
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    provider_name = order.get("payment_provider") or ""
    session_id = order.get("payment_session_id") or ""

    if not provider_name or not session_id:
        # No payment session attached — return as-is
        return order

    try:
        provider = get_provider(provider_name)
    except (KeyError, RuntimeError):
        return order

    try:
        result = await provider.query_status(session_id=session_id)
    except Exception as exc:
        _logger.warning("Payment query failed for %s: %s", order_no, exc)
        return order

    if result.status == "succeeded" and order.get("status") == "pending":
        expected_amount = float(order.get("amount") or 0)
        paid_amount = float(result.amount_cents or 0) / 100.0 if result.amount_cents else 0
        if (
            order.get("payment_provider") == "usdt"
            and expected_amount > 0
        ):
            rate = float(Config.NOWPAYMENTS_CNY_USDT_RATE or 0.0)
            if rate > 0:
                expected_amount = expected_amount * rate
        # P2.3: 与 Stripe webhook / USDT webhook / 对账共用同一容差。
        tolerance = float(getattr(Config, "STRIPE_RECON_AMOUNT_TOLERANCE", 0.01) or 0.01)
        if paid_amount > 0 and expected_amount > 0 and abs(paid_amount - expected_amount) > tolerance:
            shortfall = expected_amount - paid_amount
            _logger.critical(
                "Payment query AMOUNT MISMATCH: order %s expected %.2f, paid %.2f (shortfall=%.2f)",
                order_no,
                expected_amount,
                paid_amount,
                shortfall,
            )
            mismatch_note = json.dumps({
                "amount_mismatch": True,
                "expected": expected_amount,
                "paid": paid_amount,
                "shortfall": shortfall,
                "source": "payment_query",
            })
            with get_db_context() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                try:
                    cursor.execute(
                        "UPDATE orders SET status = 'pending_review', note = COALESCE(note, '') || ? WHERE id = ?",
                        (mismatch_note, int(order["id"])),
                    )
                    cursor.execute("COMMIT")
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
            try:
                from backend.services.notification_service import NotificationService
                NotificationService.notify(
                    int(order.get("user_id", 0)),
                    type="payment_amount_mismatch",
                    title="Payment Amount Mismatch - CRITICAL",
                    body=f"Order {order_no}: expected {expected_amount:.2f}, paid {paid_amount:.2f}. "
                         f"Order set to pending_review for admin reconciliation.",
                    metadata={
                        "order_no": order_no,
                        "expected": expected_amount,
                        "paid": paid_amount,
                        "shortfall": shortfall,
                        "severity": "critical",
                    },
                )
            except Exception:
                _logger.exception("failed to emit mismatch notification for %s", order_no)
        else:
            ok = order_service.approve_order(int(order["id"]), admin_id=SYSTEM_ADMIN_ID)
            if ok:
                with get_db_context() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        UPDATE orders
                        SET payment_reference = ?
                        WHERE id = ?
                    """,
                        (result.provider_reference, int(order["id"])),
                    )
        order = order_service.get_order_by_no(order_no, user_id=int(user.id))

    return order
